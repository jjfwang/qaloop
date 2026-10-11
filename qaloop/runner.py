"""Deterministic Playwright runner: executes a FlowSpec, returns a RunResult.

Zero LLM cost by design — this is the cheap 80%. The agentic investigator
(investigate.py) only runs when a step fails.
"""
from __future__ import annotations

import os
import re
import time
import traceback
from dataclasses import dataclass, field, replace
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
    dialogs: list[dict] = field(default_factory=list)

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
                   dialogs=d.get("dialogs", []),
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
            "dialogs": self.dialogs,
            "run_dir": self.run_dir,
        }


def _resolve_url(target: str, url: str) -> str:
    if re.match(r"^https?://", url):
        return url
    return urljoin(target.rstrip("/") + "/", url.lstrip("/"))


def _resolve_scope(page, step: Step, timeout_ms: int | None = None):
    """Return the Page or Frame that selector lookups resolve against.

    Issue #80: a step may carry `frame: <name | url substring>`; then all
    selector lookups resolve inside that frame instead of the main frame.
    Lookup is by frame name first (the iframe's `name` attribute), then by
    URL substring — a literal substring match on the frame's URL. Frames
    attach asynchronously (after the parent's DOMContentLoaded), so the
    lookup polls until the step timeout expires, mirroring
    wait_for_selector semantics. Raises RuntimeError with a clear detail
    when no frame matches, so the step fails (action errors propagate;
    assertions record the failure).
    """
    frame = step.frame
    if not frame:
        return page
    wait_ms = (timeout_ms if timeout_ms is not None
               else step.timeout_ms or 15000)
    url_rx = re.compile(re.escape(frame))
    deadline = time.monotonic() + wait_ms / 1000
    while True:
        scope = page.frame(name=frame)
        if scope is None:
            scope = page.frame(url=url_rx)
        if scope is not None:
            return scope
        if time.monotonic() >= deadline:
            raise RuntimeError(f"frame not found: {frame!r}")
        # page.frame() is a local lookup on the driver's last-known frame
        # tree, so a cheap round trip is needed for freshly attached or
        # navigated frames to become visible to the lookup.
        page.evaluate("0")
        time.sleep(0.15)


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


def _do_action(page, step: Step, target: str, mock_hits: dict,
               upload_dir: str | None = None) -> None:
    op, p = step.op, step.params
    t = step.timeout_ms or 15000
    # Issue #80: selector-resolving ops run against the frame scope when the
    # step carries frame:. Page-level ops (goto, reload, back, wait_ms,
    # wait without target / js / response, seed, script, mock) stay on the
    # main page, as does page.mouse (a Frame has no mouse).
    scope = _resolve_scope(page, step, t)
    if op == "goto":
        page.goto(_resolve_url(target, p), timeout=t, wait_until="domcontentloaded")
    elif op == "click":
        scope.click(p, timeout=t)
    elif op == "dblclick":
        scope.dblclick(p, timeout=t)
    elif op == "fill":
        scope.fill(p["target"], p["text"], timeout=t)
    elif op == "press":
        scope.press(p["target"], p["key"], timeout=t)
    elif op == "type":
        # Issue #78: keystroke-by-keystroke typing with an optional
        # inter-keystroke pause in ms — fires per-character
        # keydown/keypress/input events, unlike fill's instant set.
        # Playwright 1.63's press_sequentially takes delay as a keyword arg.
        scope.locator(p["target"]).press_sequentially(
            p["text"], delay=p.get("delay_ms", 0) or 0, timeout=t)
    elif op == "focus":
        scope.locator(p["target"]).focus(timeout=t)
    elif op == "hover":
        scope.hover(p["target"], timeout=t)
    elif op == "scroll":
        target_sel = p.get("target")
        if target_sel:
            scope.locator(target_sel).scroll_into_view_if_needed(timeout=t)
        else:
            pixels = p.get("pixels", 600)
            direction = p.get("direction", "down")
            offsets = {"up": (0, -pixels), "down": (0, pixels),
                       "left": (-pixels, 0), "right": (pixels, 0)}
            page.mouse.wheel(*offsets[direction])
    elif op == "check":
        scope.check(p, timeout=t)
    elif op == "uncheck":
        scope.uncheck(p, timeout=t)
    elif op == "select":
        scope.select_option(p["target"], p["value"], timeout=t)
    elif op == "upload":
        # Issue #76: file upload via the runner, guarded like perform.
        # Fail-closed: the upload dir must be declared via
        # verify --upload-dir or the QALOOP_UPLOAD_DIR env var.
        if not upload_dir:
            raise RuntimeError(
                "upload needs a declared upload dir: pass --upload-dir or "
                "set QALOOP_UPLOAD_DIR")
        from qaloop.upload import check_upload_path
        checked = check_upload_path(p["path"], upload_dir)
        scope.locator(p["target"]).set_input_files(checked, timeout=t)
    elif op == "drag":
        src = scope.locator(p["target"])
        to = p["to"]
        if isinstance(to, str):
            src.drag_to(scope.locator(to), timeout=t)
        else:
            # Playwright 1.63's drag_to takes only a Locator, so the coords
            # form drives the mouse directly: mousedown on the source, move
            # to the page coordinates, mouseup to drop.
            src.hover(timeout=t)
            page.mouse.down()
            page.mouse.move(to["x"], to["y"], steps=10)
            page.mouse.up()
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
        js_expr = params.get("js")
        resp_substr = params.get("response")
        if js_expr is not None:
            # Polls the JS expression in the page until it evaluates truthy
            # (issue #79). wait_for_function already polls and raises on
            # timeout, so no manual loop is needed.
            page.wait_for_function(js_expr, timeout=wt)
        elif resp_substr is not None:
            # Playwright 1.63's Page has no wait_for_response; wait_for_event
            # "response" has the same headers-received semantics (issue #79):
            # the body may still be streaming, so follow with text_contains
            # when body content matters.
            page.wait_for_event("response",
                                lambda r, _s=resp_substr: _s in r.url,
                                timeout=wt)
        elif sel:
            if text:
                scope.locator(sel, has_text=text).wait_for(state=state, timeout=wt)
            else:
                scope.wait_for_selector(sel, state=state, timeout=wt)
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


def _poll_text_match(scope, sel, match, deadline_s):
    """Re-read text_content until match(actual) succeeds or the deadline passes.

    Thin wrapper over _poll_value_match, kept so the text-assertion call sites
    and their behavior are unchanged. scope is a Page or a Frame.
    """
    return _poll_value_match(lambda: scope.text_content(sel) or "",
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


def _dialog_matches(dialogs: list[dict], criteria: dict) -> tuple[bool, str]:
    """Match recorded dialogs against a `dialog:` assertion's criteria.

    Pure function (no page): criteria may carry `type` (alert|confirm|prompt|
    beforeunload) and/or `text` (substring of the dialog message). Matches
    any dialog recorded since the step started.
    """
    want_type = criteria.get("type")
    want_text = criteria.get("text")
    for d in dialogs:
        if want_type and d.get("type") != want_type:
            continue
        if want_text and want_text not in (d.get("message") or ""):
            continue
        return True, f"matched {d.get('type')}: {(d.get('message') or '')[:160]!r}"
    if not dialogs:
        return False, "no dialog appeared since the step started"
    seen = ", ".join(
        f"{d.get('type')}: {(d.get('message') or '')[:80]!r}" for d in dialogs[:3])
    return False, f"no dialog matched the criteria (saw: {seen})"


# Issue #84: single fixed `page.evaluate` read for the `perf` assertion.
# Returns {lcp_ms, cls, inp_ms} as numbers or null:
# - LCP: last largest-contentful-paint entry's renderTime || startTime,
#   null when no such entry was recorded. Read via a short-lived
#   PerformanceObserver with buffered:true: headless-shell delivers LCP
#   entries to observers but does not retain them in the queryable
#   getEntriesByType buffer.
# - CLS: sum of layout-shift entry values where !hadRecentInput; 0 when
#   none recorded — that is a real reading, not missing data.
# - INP: max duration of `first-input` entries with an interactionId,
#   null when none (no real interaction happened yet). Headless-shell
#   only records `event` timing entries for an observer registered
#   before the interaction, which an assertion cannot install after the
#   fact; first-input entries are buffered and carry the same
#   interactionId/duration shape, so INP is approximated from the first
#   interaction (documented limitation, see SPEC.md).
_PERF_VITALS_SCRIPT = """(async () => {
  const lcpEntries = await new Promise((resolve) => {
    const seen = [];
    let obs = null;
    try {
      obs = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) seen.push(e);
      });
      obs.observe({type: 'largest-contentful-paint', buffered: true});
    } catch (err) { /* entry type unsupported: lcp stays null */ }
    setTimeout(() => { if (obs) obs.disconnect(); resolve(seen); }, 60);
  });
  let lcp = null;
  for (const e of lcpEntries) lcp = e.renderTime || e.startTime;
  let cls = 0;
  for (const e of performance.getEntriesByType('layout-shift')) {
    if (!e.hadRecentInput) cls += e.value;
  }
  let inp = null;
  for (const e of performance.getEntriesByType('first-input')) {
    if (e.interactionId) inp = inp === null ? e.duration : Math.max(inp, e.duration);
  }
  return {lcp_ms: lcp, cls: cls, inp_ms: inp};
})()"""


# Issue #85: vendored axe-core (qaloop/assets/axe.min.js, bundled
# in-repo for determinism and offline safety — see assets/README.md).
_AXE_MIN_JS = os.path.join(os.path.dirname(__file__), "assets", "axe.min.js")

# Issue #85: one-shot axe.run wrapper. Playwright evaluate awaits the
# returned promise. rules is a list of axe rule ids (or [] to scan with
# axe's default rule set); each violation is reduced to {id,
# description} so the result stays JSON-serializable.
_AXE_RUN_SCRIPT = """(rules) => axe.run(document,
    rules && rules.length ? {runOnly: {type: "rule", values: rules}} : {})
  .then((r) => r.violations.map((v) => ({id: v.id, description: v.description})))"""  # noqa: E501


def _axe_scan(page, rules: list | None) -> list[dict]:
    """Run the vendored axe-core scan once (issue #85).

    Injects the vendored axe.min.js idempotently — only when
    window.axe is undefined — then runs a one-shot axe.run. Unlike
    the perf assertion, this never polls: axe.run is seconds-heavy
    per iteration. Raises on injection or scan errors; the caller
    fails the assertion closed on those.
    """
    if page.evaluate("typeof window.axe === 'undefined'"):
        with open(_AXE_MIN_JS, "r", encoding="utf-8") as f:
            source = f.read()
        page.add_script_tag(content=source)
    return page.evaluate(_AXE_RUN_SCRIPT, rules or [])


def _check_assertions(page, step: Step, collectors: Collectors,
                      baseline_dir: str = "", baseline_update: bool = False,
                      run_dir: str = "", mock_hits: dict | None = None,
                      network_log: list[dict] | None = None
                      ) -> list[AssertionResult]:
    out: list[AssertionResult] = []
    t = min(step.timeout_ms or 15000, 8000)
    for key, val in step.expect_items:
        try:
            # Issue #80: selector assertions resolve inside the step's frame
            # scope when frame: is set. A non-matching frame raises
            # RuntimeError, recorded below as an assertion failure.
            # Page-level assertions (url, title, console, js, dialog, mock /
            # network calls) stay on the main page: a Frame has no URL,
            # title, evaluate, or dialogs.
            scope = _resolve_scope(page, step, t)
            if key == "visible":
                scope.wait_for_selector(val, state="visible", timeout=t)
                out.append(AssertionResult(key, True, val))
            elif key == "hidden":
                scope.wait_for_selector(val, state="hidden", timeout=t)
                out.append(AssertionResult(key, True, val))
            elif key == "text_contains":
                sel, text = val["selector"], val["text"]
                scope.wait_for_selector(sel, state="attached", timeout=t)
                ok, actual = _poll_text_match(
                    scope, sel, lambda a: text in a,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"want {text!r} in {actual[:160]!r}"))
            elif key == "text_matches":
                sel, pattern = val["selector"], val["pattern"]
                scope.wait_for_selector(sel, state="attached", timeout=t)
                ok, actual = _poll_text_match(
                    scope, sel, lambda a: re.search(pattern, a) is not None,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"want /{pattern}/ in {actual[:160]!r}"))
            elif key == "count":
                # Poll locator count() until the comparator matches or the
                # step deadline expires (issue #53); the failure detail names
                # the final observed count, not a re-read after the poll.
                # Comparator precedence is equals > gte > lte, unchanged.
                sel = val["selector"]
                if "equals" in val:
                    match, want = (lambda a: a == val["equals"],
                                   f"={val['equals']}")
                elif "gte" in val:
                    match, want = (lambda a: a >= val["gte"],
                                   f">={val['gte']}")
                else:
                    match, want = (lambda a: a <= val["lte"],
                                   f"<={val['lte']}")
                ok, n = _poll_value_match(
                    lambda: scope.locator(sel).count(), match,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"{sel}: count={n} want {want}"))
            elif key == "attribute":
                # Assert an element's attribute value (issue #81). A missing
                # attribute (get_attribute -> None) never matches — even for
                # contains, a guard is needed because `s in None` raises.
                # Polls like text_contains/count: failure detail names the
                # final observed value, not a re-read after the poll.
                sel, name = val["selector"], val["name"]
                scope.wait_for_selector(sel, state="attached", timeout=t)
                if "equals" in val:
                    match, want = (lambda a: a == val["equals"],
                                   f"={val['equals']!r}")
                else:
                    match, want = (lambda a: a is not None and
                                   val["contains"] in a,
                                   f"contains {val['contains']!r}")
                ok, actual = _poll_value_match(
                    lambda: scope.locator(sel).get_attribute(name), match,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"{sel}: {name}={actual!r} want {want}"))
            elif key == "value":
                # Assert an input's live value after fill/type (issue #81);
                # polled so the framework's value-write has time to settle.
                sel = val["selector"]
                scope.wait_for_selector(sel, state="attached", timeout=t)
                ok, actual = _poll_value_match(
                    lambda: scope.locator(sel).input_value(),
                    lambda a: a == val["equals"],
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"{sel}: value={actual!r} want "
                                           f"{val['equals']!r}"))
            elif key == "focused":
                # Assert document.activeElement is the element (issue #81).
                # Playwright has no is_focused(), so compare identity via
                # locator.evaluate; polled so a preceding focus/click takes
                # effect before the deadline.
                sel = val["selector"]
                scope.wait_for_selector(sel, state="attached", timeout=t)
                ok, actual = _poll_value_match(
                    lambda: scope.locator(sel).evaluate(
                        "el => document.activeElement === el"),
                    lambda a: a is True,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"{sel}: focused={actual}"))
            elif key == "cookie":
                # Assert a cookie's value (issue #82). Read from the browser
                # context: cookies set async (e.g. by a login request) need
                # polling, so this polls like attribute/value. A missing
                # cookie (name not in context.cookies()) never matches — even
                # for contains. Name-only (no comparator) asserts presence:
                # "the login cookie is set". Failure detail names the final
                # observed value, not the desired one.
                name = val["name"]
                def read_cookie():
                    for c in page.context.cookies():
                        if c.get("name") == name:
                            return c.get("value")
                    return None
                if "equals" in val:
                    match, want = (lambda a: a == val["equals"],
                                   f"={val['equals']!r}")
                elif "contains" in val:
                    match, want = (lambda a: a is not None and
                                   val["contains"] in a,
                                   f"contains {val['contains']!r}")
                else:
                    match, want = (lambda a: a is not None, "present")
                ok, actual = _poll_value_match(
                    read_cookie, match, time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"cookie {name!r}={actual!r} want {want}"))
            elif key == "storage":
                # Assert a web-storage value (issue #82). Page-level: storage
                # is keyed on the main page's origin, not the step's frame
                # scope. The key is passed as a JS arg, never interpolated
                # into the script, so quoting in keys can't break or inject.
                # Polled like the other async-set state; a missing key
                # (getItem -> null) never matches. Key-only asserts presence.
                kind, skey = val["kind"], val["key"]
                script = ("(a) => { const s = a.kind === 'local' "
                          "? localStorage : sessionStorage; "
                          "return s.getItem(a.key); }")
                if "equals" in val:
                    match, want = (lambda a: a == val["equals"],
                                   f"={val['equals']!r}")
                else:
                    match, want = (lambda a: a is not None, "present")
                ok, actual = _poll_value_match(
                    lambda: page.evaluate(script, {"kind": kind, "key": skey}),
                    match, time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"storage {kind}.{skey}="
                                           f"{actual!r} want {want}"))
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
            elif key == "request_body":
                # Issue #83: assert on a recorded request body (opt-in via
                # top-level `record_bodies: true`). One-shot, consistent with
                # network_calls: entries whose url contains the substring;
                # PASS iff at least one such entry carries a recorded body
                # matching contains (substring) or equals (exact). Fail-closed:
                # with no recorded body the assertion fails rather than
                # silently passing.
                url = val["url"]
                matched = [e for e in (network_log or [])
                           if url in e.get("url", "")]
                recorded = [e for e in matched if "body" in e]
                if "contains" in val:
                    want = val["contains"]
                    hit = any(want in (e.get("body") or "") for e in recorded)
                    mode = f"contains {want!r}"
                else:
                    want = val["equals"]
                    hit = any((e.get("body") or "") == want for e in recorded)
                    mode = f"equals {want!r}"
                if hit:
                    ok, detail = True, (f"{url}: {len(matched)} matching "
                                       f"entries, {len(recorded)} with a "
                                       f"recorded body; {mode} matched")
                elif recorded:
                    ok, detail = False, (f"{url}: {len(matched)} matching "
                                        f"entries, {len(recorded)} with a "
                                        f"recorded body; none {mode}")
                else:
                    ok, detail = False, (f"{url}: {len(matched)} matching "
                                        f"entries, none with a recorded body; "
                                        f"set record_bodies: true to record "
                                        f"request bodies")
                out.append(AssertionResult(key, ok, detail))
            elif key == "perf":
                # Assert Web Vitals performance budgets (issue #84). One
                # fixed page.evaluate read per poll iteration returns all
                # metrics; each requested metric is polled via
                # _poll_value_match until a non-null reading is at or below
                # its lte budget. Fail-closed: a null reading (no LCP entry
                # recorded, no interaction yet for INP) never satisfies the
                # budget. Failure detail names the final observed value,
                # not a re-read after the poll.
                def read_vitals():
                    return page.evaluate(_PERF_VITALS_SCRIPT) or {}
                ok_all = True
                parts = []
                deadline = time.monotonic() + t / 1000.0
                for metric, budget in val.items():
                    limit = budget["lte"]
                    ok, actual = _poll_value_match(
                        lambda m=metric: read_vitals().get(m),
                        lambda a, lim=limit: a is not None and a <= lim,
                        deadline)
                    parts.append(f"{metric}={actual!r} want <={limit}")
                    ok_all = ok_all and ok
                out.append(AssertionResult(key, ok_all, "; ".join(parts)))
            elif key == "a11y":
                # Assert axe-core accessibility violations (issue #85).
                # One-shot scan per step (never polled like perf: axe.run
                # is seconds-heavy per iteration). The violation count
                # must be at or below the lte budget; the optional rules
                # list scopes the scan to those axe rule ids. Fail-closed:
                # an injection or scan error fails the assertion with the
                # error named, never passes silently. Failure detail
                # names the violation count, the violating rule ids, and
                # the first two violation descriptions (truncated).
                limit = val["violations"]["lte"]
                try:
                    violations = _axe_scan(page, val.get("rules"))
                except Exception as e:  # noqa: BLE001
                    out.append(AssertionResult(
                        key, False, f"axe scan failed: {e}"))
                else:
                    n = len(violations)
                    ids = ",".join(v["id"] for v in violations)
                    detail = f"violations={n} want <={limit}; ids={ids}"
                    for v in violations[:2]:
                        detail += (f" | {v['id']}: "
                                   f"{v['description'][:160]}")
                    out.append(AssertionResult(key, n <= limit, detail))
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
                # Poll page.evaluate until the substring appears or the step
                # deadline expires (issue #55); detail names the final
                # observed output, not a re-read after the poll.
                ok, actual = _poll_value_match(
                    lambda: str(page.evaluate(val["script"])),
                    lambda a: val["contains"] in a,
                    time.monotonic() + t / 1000.0)
                out.append(AssertionResult(key, ok,
                                           f"want {val['contains']!r} in {actual[:160]!r}"))
            elif key == "dialog":
                ok, detail = _dialog_matches(
                    collectors.dialogs_since_checkpoint(), val)
                out.append(AssertionResult(key, ok, detail))
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
                    scope.wait_for_selector(selector, state="attached", timeout=t)
                    scope.locator(selector).screenshot(path=shot)
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
                loc = (scope.get_by_role(role, name=name) if name
                       else scope.get_by_role(role))
                loc.first.wait_for(state=state, timeout=t)
                out.append(AssertionResult(
                    key, True,
                    f"role={role} name={name!r} state={state}"))
        except Exception as e:  # noqa: BLE001 — assertion failure, not a bug
            out.append(AssertionResult(key, False, f"{type(e).__name__}: {str(e)[:200]}"))
    return out


def _screenshot_policy(spec: FlowSpec) -> str:
    return spec.artifacts.get("screenshot", "per-step")


# Issue #87: runtime ${name}/$name reference shape (same as spec._VAR_REF).
_VAR_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _interpolate_vars(value, variables: dict, where: str):
    """Substitute ${name}/$name in step params from the run-scoped table.

    Returns a new structure; the parsed spec is never mutated. A
    reference to a name that was never captured is a clear step
    failure naming the variable.
    """
    if isinstance(value, str):
        def repl(m: "re.Match") -> str:
            name = m.group(1) or m.group(2)
            if name not in variables:
                raise RuntimeError(
                    f"{where}: variable ${{{name}}} was never captured "
                    f"(no store step set it before this step)")
            return variables[name]

        return _VAR_REF.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate_vars(v, variables, where)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_vars(v, variables, where) for v in value]
    return value


def _evaluate_stores(page, step: Step, variables: dict, where: str) -> None:
    """Evaluate each store `js` via page.evaluate and capture str(result).

    Runs after a passing step; a failed step captures nothing.
    Re-storing a name overwrites the previous value.
    """
    for name, src in step.store.items():
        try:
            result = page.evaluate(src["js"])
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"{where}: store '{name}' evaluation failed: {e}") from e
        variables[name] = str(result)


def run_flow(spec: FlowSpec, *, run_dir: str, target: str | None = None,
             headless: bool = True, executable_path: str | None = None,
             baseline_update: bool = False,
             upload_dir: str | None = None,
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
    collectors = Collectors(spec.record_bodies)
    # Per-run mock hit counts (issue #43): mock routes served since run
    # start, keyed by url pattern. Reset each run; mocks registered in
    # setup thread through here, and mock_calls assertions read it.
    mock_hits: dict = {}
    # Issue #87: run-scoped flow-variable table, shared across setup,
    # steps, and teardown. Steps capture via `store:`; later steps read
    # via ${name} interpolation of their params.
    variables: dict = {}
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
                    # Per-step dialog policy: accept or dismiss JS dialogs
                    # raised while this step's action runs.
                    collectors.dialog_policy = step.on_dialog
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
                            # Issue #87: interpolate ${name} in the step's
                            # params from the run-scoped variable table
                            # before the action, on a copy — the parsed
                            # spec stays pristine for retries and reports.
                            where = f"{phase}[{step.index}]"
                            eff_step = replace(
                                step,
                                params=_interpolate_vars(step.params,
                                                         variables, where))
                            if step.op:
                                _do_action(page, eff_step, target, mock_hits,
                                           upload_dir=upload_dir)
                            sr.assertions = _check_assertions(
                                page, eff_step, collectors,
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
                                # A passing step captures its store
                                # variables; a failed step captures
                                # nothing.
                                _evaluate_stores(page, step, variables,
                                                 where)
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
                        dialogs=collectors.dialogs,
                        run_dir=run_dir, error=run_error).to_dict())
    return RunResult(flow_name=spec.name, target=target, status=status,
                     started=started, ended=ended, steps=steps,
                     failed_step=failed_step,
                     console_errors=collectors.console_errors,
                     page_errors=collectors.page_errors,
                     failed_requests=collectors.failed_requests,
                     bad_responses=collectors.bad_responses,
                     network_log=collectors.network_log,
                     dialogs=collectors.dialogs,
                     run_dir=run_dir, error=run_error)
