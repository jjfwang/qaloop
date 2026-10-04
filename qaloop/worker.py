"""Worker: claim jobs -> boot env -> run flows -> report -> ledger."""
from __future__ import annotations

import os
import time
import traceback

import yaml

from .artifacts import new_run_dir, prune_old_runs
from .env import ProcTarget, boot_command, boot_static
from .report import print_summary, write_report
from .runner import run_flow
from .spec import load_spec
from . import ledger
from . import queue as q

QALOOP_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _run_one_flow(flow_path: str, target: str, runs_root: str,
                  headless: bool, executable_path: str | None,
                  do_investigate: bool, keep_runs: int = 0) -> tuple[str, str]:
    """Run a single flow. Returns (status, run_dir)."""
    os.environ["TARGET_URL"] = target  # must precede load_spec: specs use ${TARGET_URL}
    spec = load_spec(flow_path)
    run_dir = new_run_dir(runs_root, spec.name)
    result = run_flow(spec, run_dir=run_dir, target=target, headless=headless,
                      executable_path=executable_path)
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


def handle_job(job: dict, *, flows_dir: str, runs_root: str, headless: bool,
               executable_path: str | None, repos_config: dict,
               keep_runs: int = 0) -> None:
    payload = job["payload"]
    if isinstance(payload, str):
        import json as _json
        payload = _json.loads(payload)
    kind = job["kind"]
    do_investigate = bool(payload.get("investigate", True))
    targets: list[ProcTarget] = []
    try:
        if kind == "verify-flow":
            flow = payload["flow"]
            if not os.path.isabs(flow):
                flow = os.path.join(flows_dir, flow)
            target = payload["target"]
            status, run_dir = _run_one_flow(flow, target, runs_root, headless,
                                            executable_path, do_investigate,
                                            keep_runs)
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
                                                    keep_runs)
                finally:
                    for t in flow_targets:
                        t.stop()
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
            q.record_failure(job["id"], f"{kind} -> {status}")
    except Exception as e:  # noqa: BLE001 — job failure is recorded, not raised
        traceback.print_exc()
        q.record_failure(job["id"], f"worker error: {e}"[:2000])
    finally:
        for t in targets:
            t.stop()


def run_worker(*, flows_dir: str, runs_root: str, once: bool = False,
               poll_s: float = 5, headless: bool = True,
               executable_path: str | None = None,
               repos_config_path: str | None = None, keep_runs: int = 0) -> None:
    repos_config = load_repos_config(repos_config_path)
    q.requeue_stale()
    print(f"worker up — flows={flows_dir} runs={runs_root}")
    while True:
        job = q.claim()
        if job is None:
            if once:
                print("queue empty — done")
                return
            time.sleep(poll_s)
            continue
        print(f"claimed job #{job['id']} ({job['kind']})")
        handle_job(job, flows_dir=flows_dir, runs_root=runs_root,
                   headless=headless, executable_path=executable_path,
                   repos_config=repos_config, keep_runs=keep_runs)
