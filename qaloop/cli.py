"""qaloop CLI."""
from __future__ import annotations

import argparse
import os
import sys

QALOOP_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, QALOOP_HOME)

from qaloop.artifacts import new_run_dir  # noqa: E402
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


def cmd_verify(args: argparse.Namespace) -> int:
    from qaloop import ledger
    try:
        spec = load_spec(args.flow)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
    runs_root = default_runs_root()
    os.makedirs(runs_root, exist_ok=True)
    run_dir = new_run_dir(runs_root, spec.name)
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
    return 0 if result.status == "passed" else 1


def cmd_enqueue(args: argparse.Namespace) -> int:
    from qaloop import queue as q
    import json as _json
    payload = _json.loads(args.payload or "{}")
    if args.kind == "verify-flow":
        payload.setdefault("flow", args.flow)
        if args.target:
            payload["target"] = args.target
    job_id = q.enqueue(args.kind, payload)
    print(f"enqueued job #{job_id} ({args.kind})")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from qaloop.worker import run_worker
    run_worker(flows_dir=args.flows, runs_root=default_runs_root(),
               once=args.once, poll_s=args.poll, headless=not args.headed,
               executable_path=args.executable_path)
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
    r.set_defaults(fn=cmd_verify)

    e = sub.add_parser("enqueue", help="enqueue a verification job")
    e.add_argument("--kind", default="verify-flow",
                   choices=["verify-flow", "verify-repo"])
    e.add_argument("--flow", help="flow YAML (verify-flow)")
    e.add_argument("--target", help="target URL override (verify-flow)")
    e.add_argument("--payload", default="{}",
                   help="extra JSON payload (e.g. '{\"repo\": \"x\"}')")
    e.set_defaults(fn=cmd_enqueue)

    w = sub.add_parser("worker", help="run the job worker")
    w.add_argument("--flows", default=os.path.join(QALOOP_HOME, "flows"))
    w.add_argument("--once", action="store_true", help="exit when queue is empty")
    w.add_argument("--poll", type=float, default=5)
    w.add_argument("--headed", action="store_true")
    w.add_argument("--executable-path", default=None)
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
