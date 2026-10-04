"""Deterministic Playwright runner: executes a FlowSpec, returns a RunResult.

Zero LLM cost by design — this is the cheap 80%. The agentic investigator
(investigate.py) only runs when a step fails.
"""
from __future__ import annotations

import os
import re
import time
import traceback
from dataclasses import dataclass, field
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import greenlet as _greenlet

from .artifacts import Collectors, StepArtifacts, ax_snapshot, save_json
from .spec import FlowSpec, Step


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class StepResult:
    index: int
    phase: str
    name: str
    op: str | None
    status: str  # passed | failed | skipped
    duration_ms: int
    error: str = ""
    assertions: list[AssertionResult] = field(default_factory=list)
    artifacts: StepArtifacts = field(default_factory=StepArtifacts)
    attempts: int = 1  # attempts actually used (1-indexed count)


@dataclass
class RunResult:
    flow_name: str
    target: str
    status: str  # passed | failed | error
    started: float
    ended: float
    steps: list[StepResult]
    failed_step: int | None
    console_errors: list[dict]
    page_errors: list[dict]
    failed_requests: list[dict]
    bad_responses: list[dict]
    run_dir: str
    error: str = ""
    network_log: list[dict] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int((self.ended - self.started) * 1000)

    @classmethod
    def from_dict(cls, d: dict) -> "RunResult":
        steps = []
        for s in d.get("steps", []):
            steps.append(StepResult(
                index=s["index"], phase=s["phase"], name=s["name"],
                op=s.get("op"), status=s["status"],
                duration_ms=s.get("duration_ms", 0), error=s.get("error", ""),
                assertions=[AssertionResult(a["name"], a["passed"], a.get("detail", ""))
                            for a in s.get("assertions", [])],
                artifacts=StepArtifacts(
                    screenshot=s.get("screenshot"), ax_snapshot=s.get("ax_snapshot")),
                attempts=s.get("attempts", 1)))
        return cls(flow_name=d["flow_name"], target=d.get("target", ""),
                   status=d["status"], started=d.get("started", 0),
                   ended=d.get("ended", 0), steps=steps,
                   failed_step=d.get("failed_step"),
                   console_errors=d.get("console_errors", []),
                   page_errors=d.get("page_errors", []),
                   failed_requests=d.get("failed_requests", []),
                   bad_responses=d.get("bad_responses", []),
                   network_log=d.get("network_log", []),
                   run_dir=d.get("run_dir", ""), error=d.get("error", ""))

    def to_dict(self) -> dict:
        return {
            "flow_name": self.flow_name,
            "target": self.target,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "duration_ms": self.duration_ms,
            "failed_step": self.failed_step,
            "error": self.error,
            "steps": [
                {
                    "index": s.index, "phase": s.phase, "name": s.name,
                    "op": s.op, "status": s.status,
                    "duration_ms": s.duration_ms, "error": s.error,
                    "attempts": s.attempts,
                    "assertions": [
                        {"name": a.name, "passed": a.passed, "detail": a.detail}
                        for a in s.assertions
                    ],
                    "screenshot": s.artifacts.screenshot,
                    "ax_snapshot": s.artifacts.ax_snapshot,
                }
                for s in self.steps
            ],
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
            "failed_requests": self.failed_requests,
            "bad_responses": self.bad_responses,
            "network_log": self.network_log,
            "run_dir": self.run_dir,
        }


def _resolve_url(target: str, url: str) -> str:
    if re.match(r"^https?://", url):
        return url
    return urljoin(target.rstrip("/") + "/", url.lstrip("/"))


def _ensure_origin(page, target: str) -> None:
    """Seed/script ops need a document from the target origin.

    If no navigation has happened yet, load the target first so
    localStorage / page JS run against the right origin.
    """
    if not page.url or page.url == "about:blank":
        page.goto(target, wait_until="domcontentloaded")


def _do_seed(page, params: dict, timeout_ms: int, target: str) -> None:
    if "js" in params:
        _ensure_origin(page, target)
        page.evaluate(params["js"])
        return
    http = params["http"]
    method = http.get("method", "POST").upper()
    data = None
    headers = {"Content-Type": "application/json"}
    if "json" in http:
        import json as _json
        data = _json.dumps(http["json"]).encode()
    req = Request(http["url"], data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout_ms / 1000) as resp:
        resp.read()


def _route_delay(route, delay_s: float) -> None:
    """Hold a mocked response for delay_s without freezing the automation.

    Sync-API route handlers run in a greenlet while the driver event loop
    awaits their completion, so a plain time.sleep here would block every
    Playwright call for the whole delay — no mid-flight assertion (spinner,
    skeleton) could ever observe the loading state. Instead, park this
    greenlet on a loop timer: control returns to the event loop immediately,
    and the greenlet resumes to fulfill once the delay elapses. route.fulfill
    must run on this same greenlet (sync-API greenlet machinery), which is
    why a background thread cannot do the sleeping.
    """
    import asyncio

    g = _greenlet.getcurrent()
    parent = g.parent
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = getattr(route, "_loop", None)
    if loop is not None and parent is not None:
        loop.call_later(delay_s, g.switch)
        parent.switch()
    else:
        # No event loop to yield to (unexpected); degrade to a blocking
        # sleep so the latency simulation still applies.
        time.sleep(delay_s)


def _do_mock(page, params: dict, mock_hits: dict) -> None:
    """Register a Playwright network route returning a canned response.

    Register in setup before the goto that triggers the requests — routes
    only affect requests made after registration.

    mock_hits records each *served* hit keyed by the route's url pattern
    (delegated decision, issue #43: method-mismatch and times-exceeded
    fallthroughs are not mock hits — those requests didn't hit the mock).
    """
    import json as _json

    url = params["url"]
    method = params.get("method")
    status = params.get("status", 200)
    headers = dict(params.get("headers", {}))
    if "json" in params:
        body = _json.dumps(params["json"]).encode()
        headers.setdefault("content-type", "application/json")
    elif "body" in params:
        body = params["body"].encode() if isinstance(params["body"], str) else params["body"]
    else:  # path: serve a fixture file
        with open(params["path"], "rb") as f:
            body = f.read()
    times = params.get("times")
    delay_ms = params.get("delay_ms", 0)
    seen = {"n": 0}

    def handler(route, request):
        if method and request.method != method:
            route.fallback()
            return
        seen["n"] += 1
        if times is not None and seen["n"] > times:
            route.fallback()
            return
        # Count served hits for mock_calls assertions (issue #43): keyed by
        # the route's url pattern, incremented only on the serving branch.
        mock_hits[url] = mock_hits.get(url, 0) + 1
        # Hold the mocked response without freezing the automation: the sleep
        # must not block Playwright's event loop, or no mid-flight assertion
        # could ever observe the loading state. No sleep on the times-exceeded
        # fallback branch.
        if delay_ms:
            _route_delay(route, delay_ms / 1000)
        route.fulfill(status=status, headers=headers, body=body)

    page.route(url, handler)


def _do_action(page, step: Step, target: str, mock_hits: dict) -> None:
    op, p = step.op, step.params
    t = step.timeout_ms or 15000
    if op == "goto":
        page.goto(_resolve_url(target, p), timeout=t, wait_until="domcontentloaded")
    elif op == "click":
        page.click(p, timeout=t)
    elif op == "dblclick":
        page.dblclick(p, timeout=t)
    elif op == "fill":
        page.fill(p["target"], p["text"], timeout=t)
    elif op == "press":
        page.press(p["target"], p["key"], timeout=t)
    elif op == "check":
        page.check(p, timeout=t)
    elif op == "uncheck":
        page.uncheck(p, timeout=t)
    elif op == "select":
        page.select_option(p["target"], p["value"], timeout=t)
    elif op == "wait":
        params = p or {}
        sel = params.get("target")
        # Decision (#20): the default state follows the form — targetless
        # waits watch the page, so default "load"; target-ful waits watch a
        # selector, so default "visible". The in (...) guard below stays as a
        # safety net for unvalidated flows (spec validation already rejects
        # anything but load|domcontentloaded|networkidle here).
        state = params.get("state", "load" if not sel else "visible")
        wt = params.get("timeout_ms", t)
        text = params.get("text")
        if sel:
            if text:
                page.locator(sel, has_text=text).wait_for(state=state, timeout=wt)
            else:
                page.wait_for_selector(sel, state=state, timeout=wt)
        else:
            page.wait_for_load_state(state if state in ("load", "domcontentloaded", "networkidle") else "load",
                                     timeout=wt)
    elif op == "wait_ms":
        page.wait_for_timeout(p)
    elif op == "reload":
        page.reload(timeout=t, wait_until="domcontentloaded")
    elif op == "back":
        page.go_back(timeout=t, wait_until="domcontentloaded")
    elif op == "seed":
        _do_seed(page, p, t, target)
    elif op == "script":
        _ensure_origin(page, target)
        page.evaluate(p["js"])
    elif op == "mock":
        _do_mock(page, p, mock_hits)
    else:
        raise ValueError(f"unknown op {op}")


def _poll_value_match(read, match, deadline_s):
    """Re-read a page value until match(actual) succeeds or the deadline passes.

    Async-rendered state races the old one-shot read, so assertions poll here.
    read() is called again on every iteration; match(actual) may raise re.error
    for a bad pattern, which propagates to the caller exactly as the one-shot
    read did. Returns (ok, final_actual) so callers can name the last observed
    value in failure detail without re-reading after the deadline.
    """
    actual = ""
    while True:
        actual = read()
        if match(actual):
            return True, actual
        if time.monotonic() >= deadline_s:
            return False, actual
        time.sleep(0.15)


def _poll_text_match(page, sel, match, deadline_s):
    """Re-read text_content until match(actual) succeeds or the deadline passes.

    Thin wrapper over _poll_value_match, kept so the text-assertion call sites
    and their behavior are unchanged.
    """
    return _poll_value_match(lambda: page.text_content(sel) or "",
                             match, deadline_s)


def _status_cmp(entry_status: int, filt: dict) -> bool:
    """Apply a validated network_calls status filter to one entry's status.

    filt holds exactly one of equals|gte|lte (spec.py enforces shape).
    """
    if "equals" in filt:
        return entry_status == filt["equals"]
    if "gte" in filt:
        return entry_status >= filt["gte"]
    return entry_status <= filt["lte"]


def _check_assertions(page, step: Step, collectors: Collectors,
                      baseline_dir: str = "", baseline_update: bool = False,
                      run_dir: str = "", mock_hits: dict | None = None,
                      network_log: list[dict] | None = None
                      ) -> list[AssertionResult]:
    out: list[AssertionResult] = []
    t = min(step.timeout_ms or 15000, 8000)
    for key, val in step.expect_items:
        try:
            if key == "visible":
                page.wait_for_selector(val, state="visible", timeout=t)
                out.append(AssertionResult(key, True, val))
            elif key == "hidden":
                page.wait_for_selector(val, state="hidden", timeout=t)
                out.append(AssertionResult(key, True, val))
            elif key == "text_contains":
                sel, text = val["selector"], val["text"]
                page.wait_for_selector(sel, state="attached", timeout=t)
                ok, actual = _poll_text_match(
                    page, sel, lambda a: text in a,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"want {text!r} in {actual[:160]!r}"))
            elif key == "text_matches":
                sel, pattern = val["selector"], val["pattern"]
                page.wait_for_selector(sel, state="attached", timeout=t)
                ok, actual = _poll_text_match(
                    page, sel, lambda a: re.search(pattern, a) is not None,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"want /{pattern}/ in {actual[:160]!r}"))
            elif key == "count":
                sel = val["selector"]
                n = page.locator(sel).count()
                if "equals" in val:
                    ok, detail = n == val["equals"], f"count={n} want ={val['equals']}"
                elif "gte" in val:
                    ok, detail = n >= val["gte"], f"count={n} want >={val['gte']}"
                else:
                    ok, detail = n <= val["lte"], f"count={n} want <={val['lte']}"
                out.append(AssertionResult(key, ok, f"{sel}: {detail}"))
            elif key == "mock_calls":
                # Assert how often a mocked route was served (issue #43).
                # observed comes from the run's mock_hits dict, keyed by the
                # mock's url pattern; an unregistered/never-hit url is 0.
                url = val["url"]
                n = (mock_hits or {}).get(url, 0)
                if "equals" in val:
                    ok, detail = n == val["equals"], f"mock_calls={n} want ={val['equals']}"
                elif "gte" in val:
                    ok, detail = n >= val["gte"], f"mock_calls={n} want >={val['gte']}"
                else:
                    ok, detail = n <= val["lte"], f"mock_calls={n} want <={val['lte']}"
                out.append(AssertionResult(key, ok, f"{url}: {detail}"))
            elif key == "network_calls":
                # Assert how often the page made real network requests (issues
                # #47, #49). Counted against the run's network_log by substring
                # match on the entry url; an optional status filter narrows the
                # entries by response status BEFORE counting and never matches
                # failed requests (status None); without one, failed requests
                # count as calls.
                url = val["url"]
                filt = val.get("status")
                status_desc = ""
                if filt is not None:
                    op, sv = next(iter(filt.items()))
                    status_desc = (" status=" if op == "equals" else
                                   " status>=" if op == "gte" else " status<=")
                    status_desc += str(sv)
                if filt is None:
                    n = sum(1 for e in (network_log or [])
                            if url in e.get("url", ""))
                else:
                    n = sum(1 for e in (network_log or [])
                            if url in e.get("url", "")
                            and e.get("status") is not None
                            and _status_cmp(e["status"], filt))
                if "equals" in val:
                    ok, detail = n == val["equals"], f"network_calls={n} want ={val['equals']}{status_desc}"
                elif "gte" in val:
                    ok, detail = n >= val["gte"], f"network_calls={n} want >={val['gte']}{status_desc}"
                else:
                    ok, detail = n <= val["lte"], f"network_calls={n} want <={val['lte']}{status_desc}"
                out.append(AssertionResult(key, ok, f"{url}: {detail}"))
            elif key == "url_contains":
                # Poll page.url until the substring appears or the step
                # deadline expires (issue #51); detail names the final
                # observed URL, not a re-read after the poll.
                ok, actual = _poll_value_match(lambda: page.url,
                                               lambda a: val in a,
                                               time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok, f"url={actual[:160]!r}"))
            elif key == "title_contains":
                # Poll page.title() until the substring appears or the step
                # deadline expires (issue #51); detail names the final
                # observed title, not a re-read after the poll.
                ok, actual = _poll_value_match(page.title,
                                               lambda a: val in a,
                                               time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok, f"title={actual[:120]!r}"))
            elif key == "noop":
                out.append(AssertionResult(key, True, "intentional no-op"))
            elif key == "console_clean":
                errs = collectors.errors_since_checkpoint()
                ok = not errs
                detail = "clean" if ok else f"{len(errs)} error(s): " + "; ".join(
                    e.get("text", "")[:120] for e in errs[:3])
                out.append(AssertionResult(key, ok, detail))
            elif key == "js":
                actual = str(page.evaluate(val["script"]))
                ok = val["contains"] in actual
                out.append(AssertionResult(key, ok,
                                           f"want {val['contains']!r} in {actual[:160]!r}"))
            elif key == "screenshot_matches":
                from .artifacts import screenshot_rms_diff, write_diff_image
                baseline = val["baseline"]
                if not os.path.isabs(baseline):
                    baseline = os.path.join(baseline_dir, baseline)
                max_diff = val.get("max_diff", 0.02)
                shot = os.path.join(run_dir, "steps",
                                    f"assert-{step.phase}-{step.index:02d}.png")
                os.makedirs(os.path.dirname(shot), exist_ok=True)
                selector = val.get("selector")
                if selector:
                    page.wait_for_selector(selector, state="attached", timeout=t)
                    page.locator(selector).screenshot(path=shot)
                else:
                    page.screenshot(path=shot)
                if baseline_update or not os.path.exists(baseline):
                    if baseline_update:
                        os.makedirs(os.path.dirname(baseline), exist_ok=True)
                        import shutil
                        shutil.copyfile(shot, baseline)
                        out.append(AssertionResult(
                            key, True, f"baseline saved to {baseline}"))
                    else:
                        out.append(AssertionResult(
                            key, False,
                            f"baseline missing: {baseline} "
                            f"(run `qaloop baselines <flow>` to create it)"))
                else:
                    diff = screenshot_rms_diff(shot, baseline)
                    ok = diff <= max_diff
                    detail = f"rms_diff={diff:.4f} max_diff={max_diff}"
                    if not ok:
                        diff_name = (f"assert-{step.phase}-{step.index:02d}"
                                     f"-diff.png")
                        diff_path = os.path.join(run_dir, "steps", diff_name)
                        try:
                            write_diff_image(shot, baseline, diff_path)
                            detail += f" diff=steps/{diff_name}"
                        except Exception as e:  # noqa: BLE001 — never fail the run
                            detail += (" (diff image unavailable: "
                                       f"{type(e).__name__})")
                    out.append(AssertionResult(key, ok, detail))
            elif key == "ax":
                role, name = val["role"], val.get("name")
                state = val.get("state", "visible")
                loc = page.get_by_role(role, name=name) if name else page.get_by_role(role)
                loc.first.wait_for(state=state, timeout=t)
                out.append(AssertionResult(
                    key, True,
                    f"role={role} name={name!r} state={state}"))
        except Exception as e:  # noqa: BLE001 — assertion failure, not a bug
            out.append(AssertionResult(key, False, f"{type(e).__name__}: {str(e)[:200]}"))
    return out


def _screenshot_policy(spec: FlowSpec) -> str:
    return spec.artifacts.get("screenshot", "per-step")


def run_flow(spec: FlowSpec, *, run_dir: str, target: str | None = None,
             headless: bool = True, executable_path: str | None = None,
             baseline_update: bool = False,
             ) -> RunResult:
    """Execute the flow. Never raises for flow failures; raises only on harness errors.

    baseline_update: screenshot_matches assertions save baselines instead of
    comparing (used by `qaloop baselines --update`).
    """
    from playwright.sync_api import sync_playwright

    target = target or spec.target
    executable_path = executable_path or os.environ.get("QALOOP_EXECUTABLE_PATH")
    baseline_dir = os.path.dirname(os.path.abspath(spec.source_path or ""))
    started = time.time()
    steps: list[StepResult] = []
    collectors = Collectors()
    # Per-run mock hit counts (issue #43): mock routes served since run
    # start, keyed by url pattern. Reset each run; mocks registered in
    # setup thread through here, and mock_calls assertions read it.
    mock_hits: dict = {}
    status, failed_step, run_error = "passed", None, ""
    shot_policy = _screenshot_policy(spec)
    ax_on_failure = spec.artifacts.get("ax_snapshot", "on-failure") == "on-failure"
    deadline = started + spec.timeouts.get("run_ms", 300000) / 1000

    def shot_path(phase: str, i: int) -> str:
        return os.path.join(run_dir, "steps", f"{phase}-{i:02d}.png")

    def ax_path(phase: str, i: int) -> str:
        return os.path.join(run_dir, "steps", f"{phase}-{i:02d}-ax.txt")

    with sync_playwright() as pw:
        launch_kw: dict = {"headless": headless}
        if executable_path:
            launch_kw["executable_path"] = executable_path
        browser = pw.chromium.launch(**launch_kw)
        try:
            ctx = browser.new_context(viewport=spec.viewport)
            if spec.artifacts.get("trace"):
                ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = ctx.new_page()
            collectors.attach(page)

            phases = [("setup", spec.setup, True), ("steps", spec.steps, False),
                      ("teardown", spec.teardown, False)]
            abort = False
            for phase, steplist, is_setup in phases:
                for step in steplist:
                    if time.time() > deadline:
                        run_error = "run timeout exceeded"
                        status = "error"
                        abort = True
                        break
                    if abort and phase != "teardown":
                        steps.append(StepResult(step.index, phase, step.name, step.op,
                                                "skipped", 0))
                        continue
                    t0 = time.time()
                    # checkpoint once per step (before the attempt loop) so
                    # console_clean keeps its "since the step started" meaning.
                    collectors.checkpoint()
                    sr = StepResult(step.index, phase, step.name, step.op,
                                    "passed", 0)
                    # retry is a steps-phase feature only (spec rejects
                    # retry > 0 in setup/teardown); setup keeps abort-on-first-
                    # fail and teardown keeps best-effort semantics.
                    max_attempts = step.retry + 1 if phase == "steps" else 1
                    for attempt in range(max_attempts):
                        sr.attempts = attempt + 1
                        sr.status = "passed"
                        sr.error = ""
                        try:
                            if step.op:
                                _do_action(page, step, target, mock_hits)
                            sr.assertions = _check_assertions(
                                page, step, collectors,
                                baseline_dir=baseline_dir,
                                baseline_update=baseline_update,
                                run_dir=run_dir,
                                mock_hits=mock_hits,
                                network_log=collectors.network_log)
                            failed_asserts = [a for a in sr.assertions
                                              if not a.passed]
                            if failed_asserts:
                                sr.status = "failed"
                                sr.error = "; ".join(
                                    f"{a.name}: {a.detail}"
                                    for a in failed_asserts[:3])
                            else:
                                break  # first success wins
                        except Exception as e:  # noqa: BLE001
                            sr.status = "failed"
                            sr.error = f"{type(e).__name__}: {str(e)[:400]}"
                        # failed and attempts remain: loop back-to-back, no delay
                    # final status/error/assertions come from the last attempt
                    sr.duration_ms = int((time.time() - t0) * 1000)

                    # artifacts
                    try:
                        if shot_policy == "per-step" or (
                                shot_policy == "on-failure" and sr.status == "failed"):
                            sp = shot_path(phase, step.index)
                            page.screenshot(path=sp)
                            sr.artifacts.screenshot = sp
                        if sr.status == "failed" and ax_on_failure:
                            ap = ax_path(phase, step.index)
                            with open(ap, "w", encoding="utf-8") as f:
                                f.write(ax_snapshot(page))
                            sr.artifacts.ax_snapshot = ap
                    except Exception as e:  # noqa: BLE001
                        sr.error += f" [artifact error: {e}]"

                    steps.append(sr)
                    if sr.status == "failed":
                        if phase == "teardown":
                            continue  # best-effort
                        if is_setup or not step.continue_on_fail:
                            status = "failed"
                            failed_step = len(steps) - 1
                            abort = True
                if abort and phase != "teardown":
                    continue
            if spec.artifacts.get("trace"):
                ctx.tracing.stop(path=os.path.join(run_dir, "trace.zip"))
            ctx.close()
        finally:
            browser.close()

    ended = time.time()
    save_json(f"{run_dir}/run.json",
              RunResult(flow_name=spec.name, target=target, status=status,
                        started=started, ended=ended, steps=steps,
                        failed_step=failed_step,
                        console_errors=collectors.console_errors,
                        page_errors=collectors.page_errors,
                        failed_requests=collectors.failed_requests,
                        bad_responses=collectors.bad_responses,
                        network_log=collectors.network_log,
                        run_dir=run_dir, error=run_error).to_dict())
    return RunResult(flow_name=spec.name, target=target, status=status,
                     started=started, ended=ended, steps=steps,
                     failed_step=failed_step,
                     console_errors=collectors.console_errors,
                     page_errors=collectors.page_errors,
                     failed_requests=collectors.failed_requests,
                     bad_responses=collectors.bad_responses,
                     network_log=collectors.network_log,
                     run_dir=run_dir, error=run_error)
