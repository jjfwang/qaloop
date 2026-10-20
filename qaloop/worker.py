"""Worker: claim jobs -> boot env -> run flows -> report -> ledger."""
from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import yaml

from .artifacts import new_run_dir, prune_old_runs
from .env import ProcTarget, boot_command, boot_static
from .report import print_summary, write_report
from .runner import run_flow
from .spec import load_spec
from . import ledger
from . import queue as q

QALOOP_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Default per-job timeout for handle_job (issue #94): a hung browser holding
# a claimed job used to surface only at the 1h stale requeue; the timeout
# turns that into a recorded failure in seconds.
DEFAULT_JOB_TIMEOUT_S = 1800.0

# Guard the os.environ["TARGET_URL"] set + load_spec pair in _run_one_flow.
# spec.py expands ${TARGET_URL} from the process environment, so without the
# lock two concurrent workers could read each other's target URL.
_TARGET_URL_LOCK = threading.Lock()


def load_repos_config(path: str | None = None) -> dict:
    path = path or os.path.join(QALOOP_HOME, "repos.yaml")
    if not os.path.exists(path):
        return {"repos": {}}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {"repos": {}}


def _resolve_flow_entry(entry) -> tuple[str, str | None]:
    """A flow entry is a path string, or {flow: path, env: profile}."""
    if isinstance(entry, str):
        return entry, None
    return entry["flow"], entry.get("env")


def _stop_targets(targets) -> None:
    """Stop each target, isolating failures so one raising stop()
    cannot prevent stop attempts on the remaining targets."""
    for t in targets:
        try:
            t.stop()
        except Exception:  # noqa: BLE001 - cleanup must attempt every target
            pass


def boot_env_entries(entries: list[dict], workdir: str) -> tuple[list[ProcTarget], str]:
    """Boot env entries. Returns (targets, primary_url)."""
    targets: list[ProcTarget] = []
    primary_url = ""
    try:
        for entry in entries:
            if "static" in entry:
                s = entry["static"]
                d = s["dir"]
                if not os.path.isabs(d):
                    d = os.path.join(workdir, d)
                t = boot_static(d, port=s.get("port"))
                targets.append(t)
                primary_url = primary_url or t.url
            elif "command" in entry:
                c = entry["command"]
                cwd = c.get("cwd") or workdir
                t = boot_command(c["command"], cwd=cwd, env=c.get("env"),
                                 wait=c.get("wait"), timeout_s=c.get("timeout_s", 90),
                                 name=c.get("name", "cmd"))
                targets.append(t)
                primary_url = primary_url or t.url
            else:
                raise ValueError(f"unknown env entry {entry}")
        return targets, primary_url
    except Exception:
        for t in targets:
            t.stop()
        raise


def boot_repo_env(preset: dict, profile: str | None = None) -> tuple[list[ProcTarget], str]:
    """Boot a repo preset's env (default, or a named env_profiles entry)."""
    workdir = preset.get("workdir", QALOOP_HOME)
    if profile:
        profiles = preset.get("env_profiles") or {}
        if profile not in profiles:
            raise ValueError(f"no env profile {profile!r} (have: {sorted(profiles)})")
        entries = profiles[profile]
    else:
        entries = preset.get("env", [])
    return boot_env_entries(entries, workdir)


def _notify_failure(job_id: int, kind: str, run_dir: str, summary: str) -> None:
    """POST a failure notification to QALOOP_NOTIFY_URL; never raises.

    Notification failures are swallowed and logged: a dead or missing
    webhook must never fail the job handling itself.
    """
    url = os.environ.get("QALOOP_NOTIFY_URL", "").strip()
    if not url:
        return
    import json as _json
    import urllib.request
    body = _json.dumps({
        "job_id": job_id,
        "kind": kind,
        "run_dir": run_dir,
        "summary": summary,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:  # noqa: BLE001 — notification is best-effort
        print(f"notify failed (job {job_id}): {e}")


def _run_one_flow(flow_path: str, target: str, runs_root: str,
                  headless: bool, executable_path: str | None,
                  do_investigate: bool, keep_runs: int = 0,
                  upload_dir: str | None = None) -> tuple[str, str]:
    """Run a single flow. Returns (status, run_dir)."""
    with _TARGET_URL_LOCK:
        # must precede load_spec: specs use ${TARGET_URL}; the lock keeps a
        # concurrent worker thread from seeing this thread's target URL
        os.environ["TARGET_URL"] = target
        spec = load_spec(flow_path)
    run_dir = new_run_dir(runs_root, spec.name)
    # Pass upload_dir only when declared: run_flow defaults it to None
    # (fail-closed), and existing run_flow stubs keep their signature.
    flow_kwargs = {"upload_dir": upload_dir} if upload_dir is not None else {}
    result = run_flow(spec, run_dir=run_dir, target=target, headless=headless,
                      executable_path=executable_path, **flow_kwargs)
    diagnosis = None
    if result.status != "passed" and do_investigate and spec.investigate_on_failure:
        from .investigate import investigate as run_investigation
        diagnosis = run_investigation(
            result=result, spec=spec, target=target,
            max_actions=spec.max_investigation_actions,
            headless=headless, executable_path=executable_path, run_dir=run_dir)
        if diagnosis:
            ledger.append({"kind": "investigation", "run_dir": run_dir,
                           "flow": spec.name, "model": diagnosis.get("model"),
                           "tokens_in": diagnosis.get("tokens_in"),
                           "tokens_out": diagnosis.get("tokens_out"),
                           "cost_usd_est": diagnosis.get("cost_usd_est")})
    write_report(result, spec, run_dir, diagnosis=diagnosis)
    prune_old_runs(runs_root, keep_runs, current_run_dir=run_dir)
    print_summary(result)
    ledger.append({"kind": "scripted", "run_dir": run_dir, "flow": spec.name,
                   "status": result.status, "duration_ms": result.duration_ms,
                   "cost_usd_est": 0.0})
    return result.status, run_dir


def _resolve_job_timeout(payload: dict, job_timeout_s: float) -> float:
    """Resolve the per-job timeout in seconds: a positive numeric payload
    'timeout_s' overrides the CLI default; anything else (missing,
    non-positive, non-numeric) falls back to the default defensively."""
    raw = payload.get("timeout_s")
    if raw is None:
        return job_timeout_s
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return job_timeout_s
    return value if value > 0 else job_timeout_s


def _handle_job_body(job: dict, *, flows_dir: str, runs_root: str, headless: bool,
                     executable_path: str | None, repos_config: dict,
                     keep_runs: int = 0,
                     upload_dir: str | None = None) -> None:
    payload = job["payload"]
    if isinstance(payload, str):
        import json as _json
        payload = _json.loads(payload)
    kind = job["kind"]
    do_investigate = bool(payload.get("investigate", True))
    # Pass upload_dir only when declared (see _run_one_flow).
    flow_kwargs = {"upload_dir": upload_dir} if upload_dir is not None else {}
    targets: list[ProcTarget] = []
    try:
        if kind == "verify-flow":
            flow = payload["flow"]
            if not os.path.isabs(flow):
                flow = os.path.join(flows_dir, flow)
            target = payload["target"]
            status, run_dir = _run_one_flow(flow, target, runs_root, headless,
                                            executable_path, do_investigate,
                                            keep_runs, **flow_kwargs)
        elif kind == "verify-repo":
            repo = payload["repo"]
            preset = (repos_config.get("repos") or {}).get(repo)
            if not preset:
                raise ValueError(f"no repo preset for {repo!r} (see repos.yaml)")
            flow_entries = payload.get("flows") or preset.get("flows") or []
            if not flow_entries:
                raise ValueError(f"no flows for repo {repo!r}")
            worst, last_dir = "passed", ""
            for entry in flow_entries:
                flow, profile = _resolve_flow_entry(entry)
                if not os.path.isabs(flow):
                    # repos.yaml paths are relative to the qaloop home;
                    # fall back to flows_dir for bare names
                    home_rel = os.path.join(QALOOP_HOME, flow)
                    flow = home_rel if os.path.exists(home_rel) else os.path.join(flows_dir, flow)
                flow_targets, primary_url = boot_repo_env(preset, profile)
                targets.extend(flow_targets)
                try:
                    target = payload.get("target") or primary_url
                    status, last_dir = _run_one_flow(flow, target, runs_root, headless,
                                                    executable_path, do_investigate,
                                                    keep_runs, **flow_kwargs)
                finally:
                    _stop_targets(flow_targets)
                    targets = [t for t in targets if t not in flow_targets]
                if status != "passed":
                    worst = status
                    if not payload.get("continue_on_failure"):
                        break
            status, run_dir = worst, last_dir
        else:
            raise ValueError(f"unknown job kind {kind!r}")
        if status == "passed":
            q.complete(job["id"], "done", run_dir=run_dir, note=f"{kind} -> {status}")
        else:
            attempts = (job.get("attempts") or 0) + 1
            outcome = q.record_failure(job["id"], f"{kind} -> {status}")
            if outcome == "failed":
                # Terminal failure: notify once; retries ("requeued") stay silent.
                _notify_failure(job["id"], kind, run_dir,
                                f"{kind} -> {status} (attempt {attempts})")
    except Exception as e:  # noqa: BLE001 — job failure is recorded, not raised
        traceback.print_exc()
        attempts = (job.get("attempts") or 0) + 1
        outcome = q.record_failure(job["id"], f"worker error: {e}"[:2000])
        if outcome == "failed":
            _notify_failure(job["id"], kind, "",
                            f"worker error: {e} (attempt {attempts})"[:2000])
    finally:
        _stop_targets(targets)


def handle_job(job: dict, *, flows_dir: str, runs_root: str, headless: bool,
               executable_path: str | None, repos_config: dict,
               keep_runs: int = 0,
               job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
               upload_dir: str | None = None) -> None:
    """Handle one job with a timeout (issue #94): the job body runs in a
    daemon thread and is joined for timeout_s. On expiry the job is recorded
    failed via q.record_failure (so the retry budget applies exactly like any
    other failure — requeued within budget, and a terminal timeout takes the
    existing _notify_failure path from #92).

    The hung thread stays orphaned: Playwright sync objects are not
    thread-safe, so the timer side never touches the browser — run_flow's
    'finally: browser.close()' reclaims it when the hung call eventually
    returns. Documented tradeoff, not a bug (see README)."""
    payload = job["payload"]
    if isinstance(payload, str):
        import json as _json
        payload = _json.loads(payload)
    timeout_s = _resolve_job_timeout(payload, job_timeout_s)
    body = threading.Thread(
        target=_handle_job_body,
        kwargs=dict(job=job, flows_dir=flows_dir, runs_root=runs_root,
                    headless=headless, executable_path=executable_path,
                    repos_config=repos_config, keep_runs=keep_runs,
                    upload_dir=upload_dir),
        daemon=True)
    body.start()
    body.join(timeout_s)
    if not body.is_alive():
        return
    # Hung job: surface the timeout in seconds instead of waiting for the
    # 1h stale requeue. The orphaned thread keeps its own claim context and
    # finishes on its own terms when the hung call returns.
    attempts = (job.get("attempts") or 0) + 1
    note = f"job timeout: exceeded {timeout_s:g}s"
    outcome = q.record_failure(job["id"], note)
    print(f"job #{job['id']} timed out after {timeout_s:g}s ({outcome})")
    if outcome == "failed":
        _notify_failure(job["id"], job["kind"], "",
                        f"job timeout: exceeded {timeout_s:g}s (attempt {attempts})")


def run_worker(*, flows_dir: str, runs_root: str, once: bool = False,
               poll_s: float = 5, headless: bool = True,
               executable_path: str | None = None,
               repos_config_path: str | None = None, keep_runs: int = 0,
               concurrency: int = 1,
               job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
               upload_dir: str | None = None) -> None:
    """Claim jobs and handle them, one at a time (concurrency=1) or in
    parallel via a thread pool. Each job gets its own browser: handle_job ->
    run_flow launches and closes one per invocation (Playwright sync objects
    are not thread-safe, so browsers are never shared). q.claim() is atomic
    across threads, so parallel claims never double-claim. Each job is also
    wrapped in handle_job's timeout (issue #94): both the serial path and the
    --concurrency pool path get it for free, and a timed-out future still
    completes so --once exit logic is unchanged."""
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1 (got {concurrency!r})")
    if job_timeout_s <= 0:
        raise ValueError(f"job_timeout_s must be > 0 (got {job_timeout_s!r})")
    repos_config = load_repos_config(repos_config_path)
    q.requeue_stale()
    print(f"worker up — flows={flows_dir} runs={runs_root} concurrency={concurrency}"
          f" job_timeout_s={job_timeout_s:g}")
    handle_kwargs = dict(flows_dir=flows_dir, runs_root=runs_root,
                         headless=headless, executable_path=executable_path,
                         repos_config=repos_config, keep_runs=keep_runs,
                         job_timeout_s=job_timeout_s, upload_dir=upload_dir)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending: set = set()
        while True:
            job = q.claim()
            if job is not None:
                print(f"claimed job #{job['id']} ({job['kind']})")
                fut = executor.submit(handle_job, job, **handle_kwargs)
                if concurrency == 1:
                    # serial: wait before the next claim (exactly the old loop)
                    fut.result()
                else:
                    pending.add(fut)
            else:
                # queue empty: retire finished futures; --once exits only
                # when no claimable job AND nothing in flight
                pending = {f for f in pending if not f.done()}
                if not pending and once:
                    print("queue empty — done")
                    return
                time.sleep(poll_s)
