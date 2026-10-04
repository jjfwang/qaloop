"""qaloop CLI."""
from __future__ import annotations

import argparse
import os
import re
import sys

QALOOP_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, QALOOP_HOME)

from qaloop.artifacts import new_run_dir, prune_old_runs  # noqa: E402
from qaloop.report import print_summary, write_report  # noqa: E402
from qaloop.runner import run_flow  # noqa: E402
from qaloop.spec import SpecError, load_spec  # noqa: E402


def default_runs_root() -> str:
    return os.environ.get("QALOOP_RUNS", os.path.join(QALOOP_HOME, "runs"))


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.flow, strict_env=False)
    except SpecError as e:
        print(f"INVALID: {e}")
        return 1
    n = len(spec.setup) + len(spec.steps) + len(spec.teardown)
    print(f"VALID: {spec.name} — {n} steps "
          f"(setup {len(spec.setup)}, steps {len(spec.steps)}, teardown {len(spec.teardown)}), "
          f"target {spec.target}")
    return 0


def _flow_services_info(flow_path: str):
    """Light-parse a flow: returns (flow_name, services) without env substitution."""
    from qaloop.spec import load_services_light
    return load_services_light(flow_path)


def _service_ctx(flow_path: str, services: list, run_dir: str):
    """Build the services context manager for a flow run."""
    from qaloop.env import flow_services
    base_dir = os.path.dirname(os.path.abspath(flow_path))
    return flow_services(services, base_dir, run_dir)


def cmd_verify(args: argparse.Namespace) -> int:
    from qaloop import ledger
    from qaloop.env import resolve_keep_runs
    runs_root = default_runs_root()
    os.makedirs(runs_root, exist_ok=True)
    try:
        keep_runs = resolve_keep_runs(args.keep_runs)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2
    try:
        flow_name, services = _flow_services_info(args.flow)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
    run_dir = new_run_dir(runs_root, flow_name)
    # Enter the services context explicitly so boot failures (and only boot
    # failures) are reported as such; the flow run itself manages its errors.
    ctx = _service_ctx(args.flow, services, run_dir)
    try:
        ctx.__enter__()
    except (TimeoutError, RuntimeError, ValueError) as e:
        print(f"SERVICE BOOT FAILED: {type(e).__name__}: {e}")
        return 3
    try:
        return _cmd_verify_run(args, run_dir, ledger, runs_root, keep_runs)
    finally:
        ctx.__exit__(None, None, None)


def _cmd_verify_run(args: argparse.Namespace, run_dir: str, ledger,
                    runs_root: str, keep_runs: int) -> int:
    try:
        spec = load_spec(args.flow)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
    target = args.target or spec.target
    print(f"run dir: {run_dir}")
    print(f"target:  {target}")
    result = run_flow(spec, run_dir=run_dir, target=target,
                      headless=not args.headed,
                      executable_path=args.executable_path)
    diagnosis = None
    if result.status != "passed" and args.investigate and spec.investigate_on_failure:
        from qaloop.investigate import investigate as run_investigation
        print("step failed — summoning investigator...")
        diagnosis = run_investigation(
            result=result, spec=spec, target=target,
            max_actions=args.max_investigation_actions or spec.max_investigation_actions,
            headless=not args.headed, executable_path=args.executable_path,
            run_dir=run_dir)
        if diagnosis:
            ledger.append({"kind": "investigation", "run_dir": run_dir,
                           "flow": spec.name,
                           "model": diagnosis.get("model"),
                           "tokens_in": diagnosis.get("tokens_in"),
                           "tokens_out": diagnosis.get("tokens_out"),
                           "cost_usd_est": diagnosis.get("cost_usd_est")})
    paths = write_report(result, spec, run_dir, diagnosis=diagnosis)
    print_summary(result)
    print(f"report: {paths['report_md']}")
    ledger.append({"kind": "scripted", "run_dir": run_dir, "flow": spec.name,
                   "status": result.status, "duration_ms": result.duration_ms,
                   "cost_usd_est": 0.0})
    pruned = prune_old_runs(runs_root, keep_runs, current_run_dir=run_dir)
    if pruned:
        print(f"pruned {len(pruned)} old run dir(s) (--keep-runs {keep_runs})")
    return 0 if result.status == "passed" else 1


def cmd_baselines(args: argparse.Namespace) -> int:
    """Run a flow in baseline-update mode: screenshot_matches assertions
    save their baselines instead of comparing."""
    runs_root = default_runs_root()
    os.makedirs(runs_root, exist_ok=True)
    try:
        flow_name, services = _flow_services_info(args.flow)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
    run_dir = new_run_dir(runs_root, flow_name + "-baselines")
    ctx = _service_ctx(args.flow, services, run_dir)
    try:
        ctx.__enter__()
    except (TimeoutError, RuntimeError, ValueError) as e:
        print(f"SERVICE BOOT FAILED: {type(e).__name__}: {e}")
        return 3
    try:
        return _cmd_baselines_run(args, run_dir)
    finally:
        ctx.__exit__(None, None, None)


def _cmd_baselines_run(args: argparse.Namespace, run_dir: str) -> int:
    try:
        spec = load_spec(args.flow)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
    target = args.target or spec.target
    print(f"run dir: {run_dir}")
    print(f"target:  {target}")
    print("baseline update mode — screenshots saved, not compared")
    result = run_flow(spec, run_dir=run_dir, target=target,
                      headless=not args.headed,
                      executable_path=args.executable_path,
                      baseline_update=True)
    print_summary(result)
    saved = [f"{a.name}: {a.detail}"
             for s in result.steps for a in s.assertions
             if a.name == "screenshot_matches"]
    for line in saved:
        print(" ", line)
    return 0 if result.status == "passed" else 1


def cmd_perform(args: argparse.Namespace) -> int:
    from qaloop import ledger
    from qaloop.perform import perform
    if args.max_cost_usd < 0:
        print("ERROR: --max-cost-usd cannot be negative")
        return 2
    try:
        result = perform(
            task=args.task, target=args.target,
            max_actions=args.max_actions, headless=not args.headed,
            executable_path=args.executable_path, cdp_url=args.cdp_url,
            allow_publish=args.allow_publish, upload_dir=args.upload_dir,
            max_cost_usd=args.max_cost_usd)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2
    print(f"status: {result['status']}")
    print(f"summary: {result['summary']}")
    print(f"actions: {result['action_count']}  "
          f"tokens {result['tokens_in']}/{result['tokens_out']}  "
          f"est ${result['cost_usd_est']:.4f}")
    if args.max_cost_usd > 0:
        print(f"cost ceiling: ${args.max_cost_usd:g}")
    print(f"report: {os.path.join(result['run_dir'], 'PERFORM.md')}")
    ledger.append({"kind": "perform", "run_dir": result["run_dir"],
                   "task": args.task[:120], "status": result["status"],
                   "actions": result["action_count"],
                   "tokens_in": result["tokens_in"],
                   "tokens_out": result["tokens_out"],
                   "cost_usd_est": result["cost_usd_est"],
                   "max_cost_usd": args.max_cost_usd,
                   "ceiling_reached": result.get("ceiling_hit", False)})
    return 0 if result["status"] == "completed" else 1


def _resolve_diff(diff_arg: str, repo: str | None) -> str:
    """Accept a diff file path or a git range; return unified diff text."""
    if os.path.isfile(diff_arg):
        with open(diff_arg, encoding="utf-8", errors="replace") as f:
            return f.read()
    if ".." in diff_arg or re.fullmatch(r"[0-9a-fA-F]{4,40}", diff_arg or ""):
        import subprocess
        out = subprocess.run(
            ["git", "-C", repo or ".", "diff", diff_arg],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"git diff {diff_arg}: {out.stderr.strip()[:200]}")
        return out.stdout
    raise RuntimeError(f"--diff must be a diff file or a git range, got {diff_arg!r}")


def cmd_evaluate(args: argparse.Namespace) -> int:
    from qaloop import ledger
    from qaloop.evaluate import evaluate, write_evaluation
    run_dir = args.run
    if not os.path.isdir(run_dir):
        print(f"ERROR: run dir not found: {run_dir}")
        return 2
    try:
        diff = _resolve_diff(args.diff, args.repo)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2
    if not diff.strip():
        print("ERROR: empty diff — nothing to evaluate")
        return 2
    try:
        verdict = evaluate(args.claim, run_dir, diff)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2
    md_path, json_path = write_evaluation(run_dir, args.claim, verdict)
    cost = verdict.get("_cost", {})
    ledger.append({"kind": "evaluate", "run_dir": os.path.abspath(run_dir),
                   "claim": args.claim[:120],
                   "verdict": verdict["verdict"],
                   "tokens_in": cost.get("tokens_in", 0),
                   "tokens_out": cost.get("tokens_out", 0),
                   "cost_usd_est": cost.get("cost_usd_est", 0)})
    print(f"verdict: {verdict['verdict']} (confidence: {verdict.get('confidence')})")
    print(f"rationale: {verdict.get('rationale')}")
    print(f"cost: tokens {cost.get('tokens_in')}/{cost.get('tokens_out')}  "
          f"est ${cost.get('cost_usd_est', 0):.6f}")
    print(f"report: {md_path}")
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    from qaloop import queue as q
    import json as _json
    if args.max_retries < 0:
        print(f"error: --max-retries must be >= 0 (got {args.max_retries})")
        return 2
    payload = _json.loads(args.payload or "{}")
    if args.kind == "verify-flow":
        payload.setdefault("flow", args.flow)
        if args.target:
            payload["target"] = args.target
    job_id = q.enqueue(args.kind, payload, max_retries=args.max_retries)
    print(f"enqueued job #{job_id} ({args.kind}, max_retries={args.max_retries})")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from qaloop.env import resolve_keep_runs
    from qaloop.worker import run_worker
    try:
        keep_runs = resolve_keep_runs(args.keep_runs)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2
    run_worker(flows_dir=args.flows, runs_root=default_runs_root(),
               once=args.once, poll_s=args.poll, headless=not args.headed,
               executable_path=args.executable_path, keep_runs=keep_runs)
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    from qaloop.webhook import serve
    serve(port=args.port, host=args.host)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from qaloop.dashboard import build
    path = build(default_runs_root(), args.out)
    print(f"dashboard: {path}")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    from qaloop import ledger
    import json as _json
    print(_json.dumps(ledger.summarize(), indent=2))
    return 0


def cmd_investigate(args: argparse.Namespace) -> int:
    import json as _json
    from qaloop.investigate import investigate as run_investigation
    from qaloop.runner import RunResult
    from qaloop.spec import load_spec
    with open(os.path.join(args.run, "run.json"), encoding="utf-8") as f:
        result = RunResult.from_dict(_json.load(f))
    spec = load_spec(args.flow, strict_env=False)
    diagnosis = run_investigation(
        result=result, spec=spec, target=args.target or result.target,
        max_actions=args.max_actions, headless=not args.headed,
        executable_path=args.executable_path, run_dir=args.run)
    print(_json.dumps(diagnosis, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qaloop",
                                description="Agentic QA loop: deterministic flows + browser investigator")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a flow spec")
    v.add_argument("flow", help="path to flow YAML")
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("verify", help="run a flow against a target")
    r.add_argument("flow", help="path to flow YAML")
    r.add_argument("--target", help="override spec target URL")
    r.add_argument("--headed", action="store_true", help="show the browser")
    r.add_argument("--executable-path", default=None,
                   help="chromium executable (default: playwright's)")
    r.add_argument("--investigate", action="store_true",
                   help="summon the agentic investigator on failure")
    r.add_argument("--max-investigation-actions", type=int, default=None)
    r.add_argument("--keep-runs", type=int, default=None,
                   help="keep at most N newest run dirs (env QALOOP_KEEP_RUNS; "
                        "default 0 = keep everything)")
    r.set_defaults(fn=cmd_verify)

    e = sub.add_parser("enqueue", help="enqueue a verification job")
    e.add_argument("--kind", default="verify-flow",
                   choices=["verify-flow", "verify-repo"])
    e.add_argument("--flow", help="flow YAML (verify-flow)")
    e.add_argument("--target", help="target URL override (verify-flow)")
    e.add_argument("--payload", default="{}",
                   help="extra JSON payload (e.g. '{\"repo\": \"x\"}')")
    e.add_argument("--max-retries", type=int, default=0,
                   help="retry the job up to N extra attempts after the first failure (default 0 = terminal)")
    e.set_defaults(fn=cmd_enqueue)

    w = sub.add_parser("worker", help="run the job worker")
    w.add_argument("--flows", default=os.path.join(QALOOP_HOME, "flows"))
    w.add_argument("--once", action="store_true", help="exit when queue is empty")
    w.add_argument("--poll", type=float, default=5)
    w.add_argument("--headed", action="store_true")
    w.add_argument("--executable-path", default=None)
    w.add_argument("--keep-runs", type=int, default=None,
                   help="keep at most N newest run dirs (env QALOOP_KEEP_RUNS; "
                        "default 0 = keep everything)")
    w.set_defaults(fn=cmd_worker)

    h = sub.add_parser("webhook", help="run the GitHub webhook receiver")
    h.add_argument("--port", type=int, default=8090)
    h.add_argument("--host", default="127.0.0.1")
    h.set_defaults(fn=cmd_webhook)

    d = sub.add_parser("dashboard", help="regenerate the static dashboard")
    d.add_argument("--out", default=os.path.join(QALOOP_HOME, "dashboard"))
    d.set_defaults(fn=cmd_dashboard)

    lg = sub.add_parser("ledger", help="print the cost ledger summary")
    lg.set_defaults(fn=cmd_ledger)

    iv = sub.add_parser("investigate",
                        help="run the investigator on an existing run dir")
    iv.add_argument("--run", required=True, help="run dir containing run.json")
    iv.add_argument("--flow", required=True, help="flow YAML used for the run")
    iv.add_argument("--target", default=None)
    iv.add_argument("--max-actions", type=int, default=20)
    iv.add_argument("--headed", action="store_true")
    iv.add_argument("--executable-path", default=None)
    iv.set_defaults(fn=cmd_investigate)

    bl = sub.add_parser("baselines",
                        help="run a flow saving screenshot baselines (update mode)")
    bl.add_argument("flow", help="flow YAML")
    bl.add_argument("--target", default=None)
    bl.add_argument("--headed", action="store_true")
    bl.add_argument("--executable-path", default=None)
    bl.set_defaults(fn=cmd_baselines)

    pf = sub.add_parser("perform",
                        help="natural-language browser task agent")
    pf.add_argument("--task", required=True, help="what the agent should do")
    pf.add_argument("--target", default=None,
                    help="starting URL (omit when attaching to a live browser)")
    pf.add_argument("--max-actions", type=int, default=30)
    pf.add_argument("--headed", action="store_true")
    pf.add_argument("--cdp-url", default=None,
                    help="take over a live browser, e.g. http://127.0.0.1:9222")
    pf.add_argument("--allow-publish", action="store_true",
                    help="permit external publish/post actions")
    pf.add_argument("--upload-dir", default=None,
                    help="directory file uploads are restricted to "
                         "(default: the run dir)")
    pf.add_argument("--max-cost-usd", type=float, default=0.0,
                    help="stop before the next model call would exceed this "
                         "estimated USD cost (default 0 = unlimited)")
    pf.add_argument("--executable-path", default=None)
    pf.set_defaults(fn=cmd_perform)

    ev = sub.add_parser("evaluate",
                        help="semantic judge: does the change make sense?")
    ev.add_argument("--claim", required=True,
                    help="what the change supposedly does")
    ev.add_argument("--run", required=True,
                    help="qaloop run dir with run.json evidence")
    ev.add_argument("--diff", required=True,
                    help="unified diff file, or a git range like HEAD~1..HEAD")
    ev.add_argument("--repo", default=None,
                    help="repo root for git ranges (default: auto-detect)")
    ev.set_defaults(fn=cmd_evaluate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
