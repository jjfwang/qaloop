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


class BatchSelectionError(Exception):
    """Raised when a batch (directory) verify run selects no flows."""


def _read_flow_tags(path: str) -> list[str]:
    """Light-parse a flow file's tags, without env substitution (issue #89).

    Used for --tag selection, before services boot or the full spec
    load. Raises SpecError when the file cannot be parsed or the tags
    block is invalid.
    """
    import yaml
    from qaloop.spec import _validate_tags
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        raise SpecError(f"spec {path}: cannot parse YAML ({e})")
    where = f"spec {path}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: top level must be a mapping")
    return _validate_tags(raw.get("tags"), where)


def _select_flows(flow_dir: str, tags: list[str] | None) -> list[str]:
    """Select the flow files in a directory for a batch verify run.

    Non-recursive: *.yaml/*.yml files only, sorted by name. When tags
    are given, keep flows carrying at least one of them (OR semantics,
    exact case-sensitive match). A flow whose tags cannot be read is
    kept — it surfaces as an INVALID-SPEC failure at run time rather
    than a silent skip. Raises BatchSelectionError when nothing is
    selected: a batch must never silently pass.
    """
    wanted = tags or []
    selected = []
    for name in sorted(os.listdir(flow_dir)):
        if not name.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(flow_dir, name)
        if not os.path.isfile(path):
            continue
        if wanted:
            try:
                flow_tags = _read_flow_tags(path)
            except SpecError:
                selected.append(path)
                continue
            if not any(t in flow_tags for t in wanted):
                continue
        selected.append(path)
    if not selected:
        detail = (f"no flows in {flow_dir} carry tag(s) {wanted}"
                  if wanted else f"no flow YAML files in {flow_dir}")
        raise BatchSelectionError(detail)
    return selected


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
    flow_arg = args.flow
    if os.path.isdir(flow_arg):
        # Issue #89: a directory positional verifies every flow YAML in it.
        if args.investigate:
            print("note: --investigate is not applied in batch mode")
        try:
            return _cmd_verify_batch(args, flow_arg, ledger, runs_root,
                                     keep_runs)
        except BatchSelectionError as e:
            print(f"ERROR: {e}")
            return 2
    if args.tag:
        # Issue #89: single file + --tag runs iff the flow carries the tag.
        try:
            flow_tags = _read_flow_tags(flow_arg)
        except SpecError as e:
            print(f"INVALID SPEC: {e}")
            return 2
        missing = [t for t in args.tag if t not in flow_tags]
        if missing:
            print(f"SKIP: {flow_arg} does not carry tag(s) {missing} "
                  f"(flow tags: {flow_tags or 'none'})")
            return 2
    try:
        flow_name, services = _flow_services_info(flow_arg)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
    run_dir = new_run_dir(runs_root, flow_name)
    # Enter the services context explicitly so boot failures (and only boot
    # failures) are reported as such; the flow run itself manages its errors.
    ctx = _service_ctx(flow_arg, services, run_dir)
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
                    runs_root: str, keep_runs: int, prune: bool = True) -> int:
    """Single-flow verify path. The batch path (issue #89) reuses
    _run_single_flow for the same behavior."""
    from qaloop.env import resolve_upload_dir
    # Flag wins over env (the resolve_keep_runs convention); uploads are
    # fail-closed in the runner when neither is declared (issue #76).
    upload_dir = resolve_upload_dir(args.upload_dir)
    rec = _run_single_flow(args, args.flow, run_dir, ledger, upload_dir,
                           batch_mode=False, runs_root=runs_root,
                           keep_runs=keep_runs, prune=prune)
    if rec["error"] and "INVALID SPEC" in rec["error"]:
        return 2
    return 0 if rec["status"] == "passed" else 1


def _run_single_flow(args, flow_path: str, run_dir: str, ledger, upload_dir,
                     batch_mode: bool, runs_root: str, keep_runs: int,
                     prune: bool = True) -> dict:
    """Run one flow file end to end in run_dir (issue #89).

    The full single-flow verify path: spec load, matrix dispatch,
    run_flow, write_report, ledger entry. Returns a batch-summary record
    dict: file, status, duration_ms, steps_passed, steps_total, subdir,
    error. --investigate is not applied in batch_mode (same carve-out
    as matrix mode).
    """
    rec = {"file": os.path.basename(flow_path), "status": "failed",
           "duration_ms": 0, "steps_passed": 0, "steps_total": 0,
           "subdir": run_dir, "error": None}
    try:
        spec = load_spec(flow_path)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        rec["error"] = f"INVALID SPEC: {e}"
        return rec
    target = args.target or spec.target
    print(f"run dir: {run_dir}")
    print(f"target:  {target}")
    if spec.matrix:
        _, mrec = _cmd_verify_matrix(args, spec, run_dir, ledger, runs_root,
                                     keep_runs, upload_dir, prune=prune)
        rec.update(mrec)
        return rec
    result = run_flow(spec, run_dir=run_dir, target=target,
                      headless=not args.headed,
                      executable_path=args.executable_path,
                      upload_dir=upload_dir,
                      video=getattr(args, "video", False) or None)
    diagnosis = None
    if (not batch_mode and result.status != "passed" and args.investigate
            and spec.investigate_on_failure):
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
    if prune:
        pruned = prune_old_runs(runs_root, keep_runs, current_run_dir=run_dir)
        if pruned:
            print(f"pruned {len(pruned)} old run dir(s) (--keep-runs {keep_runs})")
    rec.update(status=result.status, duration_ms=result.duration_ms,
               steps_passed=sum(1 for s in result.steps
                                if s.status == "passed"),
               steps_total=len(result.steps),
               error=result.error or None)
    return rec


def _batch_run_one(args, flow_path: str, subdir: str, ledger,
                   upload_dir) -> dict:
    """Run one flow of a batch in its subdir (issue #89).

    The full per-flow path: services light-parse + boot/teardown,
    run_flow, write_report, ledger entry. INVALID SPEC and SERVICE BOOT
    FAILED are recorded as failed records — the batch continues with
    the rest (batch resilience).
    """
    stem = os.path.splitext(os.path.basename(flow_path))[0]
    rec = {"file": os.path.basename(flow_path), "status": "failed",
           "duration_ms": 0, "steps_passed": 0, "steps_total": 0,
           "subdir": subdir, "error": None}
    try:
        _, services = _flow_services_info(flow_path)
    except SpecError as e:
        rec["error"] = f"INVALID SPEC: {e}"
        print(f"[{stem}] {rec['error']}")
        return rec
    ctx = _service_ctx(flow_path, services, subdir)
    try:
        ctx.__enter__()
    except (TimeoutError, RuntimeError, ValueError) as e:
        rec["error"] = f"SERVICE BOOT FAILED: {type(e).__name__}: {e}"
        print(f"[{stem}] {rec['error']}")
        return rec
    try:
        print(f"--- flow: {flow_path}")
        rec.update(_run_single_flow(args, flow_path, subdir, ledger,
                                    upload_dir, batch_mode=True,
                                    runs_root=None, keep_runs=0,
                                    prune=False))
        rec["file"] = os.path.basename(flow_path)
        rec["subdir"] = subdir
    finally:
        ctx.__exit__(None, None, None)
    return rec


def _cmd_verify_batch(args, flow_dir: str, ledger, runs_root: str,
                      keep_runs: int) -> int:
    """Verify every flow in a directory (issue #89).

    Parent run dir <runs>/<stamp>-<dirname>-batch with per-flow subdirs
    <parent>/flows/<stem>/, each getting the full single-flow path.
    Exit 0 iff every flow passed; 1 if any failed (INVALID SPEC and
    SERVICE BOOT FAILED are recorded as failed and the batch continues).
    Selection errors (empty selection) exit 2 via BatchSelectionError.
    --keep-runs pruning happens once, after the batch report, with the
    parent dir as current.
    """
    from qaloop.env import resolve_upload_dir
    from qaloop.report import write_batch_summary
    selected = _select_flows(flow_dir, args.tag)  # raises BatchSelectionError
    tags = args.tag or []
    parent = new_run_dir(
        runs_root, os.path.basename(os.path.abspath(flow_dir)) + "-batch")
    flows_root = os.path.join(parent, "flows")
    os.makedirs(flows_root, exist_ok=True)
    upload_dir = resolve_upload_dir(args.upload_dir)
    print(f"batch run dir: {parent}")
    print(f"flows: {len(selected)}"
          + (f" (tag filter: {', '.join(tags)})" if tags else ""))
    records = []
    for flow_path in selected:
        stem = os.path.splitext(os.path.basename(flow_path))[0]
        subdir = os.path.join(flows_root, stem)
        # Mirror new_run_dir: run_flow writes step artifacts to <subdir>/steps.
        os.makedirs(os.path.join(subdir, "steps"), exist_ok=True)
        records.append(_batch_run_one(args, flow_path, subdir, ledger,
                                      upload_dir))
    report_path = write_batch_summary(records, parent)
    passed = sum(1 for r in records if r["status"] == "passed")
    print(f"batch: {passed}/{len(records)} flows passed")
    print(f"report: {report_path}")
    pruned = prune_old_runs(runs_root, keep_runs, current_run_dir=parent)
    if pruned:
        print(f"pruned {len(pruned)} old run dir(s) (--keep-runs {keep_runs})")
    return 0 if passed == len(records) else 1


def _combo_slug(combo: dict, index: int) -> str:
    """Filesystem-safe slug for a matrix combination dir (issue #88)."""
    slug = "-".join(f"{k}-{v}" for k, v in combo.items())
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-")[:60]
    return slug or f"combo-{index:02d}"


def _cmd_verify_matrix(args, spec, run_dir: str, ledger, runs_root: str,
                       keep_runs: int, upload_dir, prune: bool = True,
                       baseline_update: bool = False) -> tuple:
    """Run a matrix flow once per combination (issue #88).

    One parent run dir, per-combination subdirs
    <run_dir>/matrix/<NN>-<combo-slug>/, each with its own run.json and
    REPORT.md; a combined summary lands in the parent REPORT.md.
    Exit code 0 iff every combination passed. Per-combination
    investigation is not run in matrix mode (--investigate applies to
    single runs only). Returns (exit_code, batch-summary record) for
    issue #89's batch table. baseline_update (issue #200) is threaded
    to each combination's run_flow: `qaloop baselines` reuses this
    dispatch so screenshot baselines are saved per combination.
    """
    from qaloop.report import write_matrix_summary
    matrix_dir = os.path.join(run_dir, "matrix")
    os.makedirs(matrix_dir, exist_ok=True)
    results = []
    for i, combo in enumerate(spec.matrix):
        subdir = os.path.join(matrix_dir, f"{i:02d}-{_combo_slug(combo, i)}")
        os.makedirs(subdir, exist_ok=True)
        print(f"combination {i + 1}/{len(spec.matrix)}: {combo}")
        result = run_flow(spec, run_dir=subdir,
                          target=args.target or spec.target,
                          headless=not args.headed,
                          executable_path=args.executable_path,
                          upload_dir=upload_dir, initial_vars=combo,
                          baseline_update=baseline_update,
                          video=getattr(args, "video", False) or None)
        write_report(result, spec, subdir)
        results.append((combo, result, subdir))
        print_summary(result)
    report_path = write_matrix_summary(spec, results, run_dir)
    passed = sum(1 for _, r, _ in results if r.status == "passed")
    print(f"matrix: {passed}/{len(results)} combinations passed")
    print(f"report: {report_path}")
    ledger.append({"kind": "scripted-matrix", "run_dir": run_dir,
                   "flow": spec.name,
                   "status": "passed" if passed == len(results) else "failed",
                   "combinations": len(results), "passed": passed,
                   "cost_usd_est": 0.0})
    if prune:
        pruned = prune_old_runs(runs_root, keep_runs, current_run_dir=run_dir)
        if pruned:
            print(f"pruned {len(pruned)} old run dir(s) (--keep-runs {keep_runs})")
    record = {"file": os.path.basename(spec.source_path),
              "status": "passed" if passed == len(results) else "failed",
              "duration_ms": sum(r.duration_ms for _, r, _ in results),
              "steps_passed": passed, "steps_total": len(results),
              "subdir": run_dir, "error": None, "matrix": True}
    return (0 if passed == len(results) else 1), record


def cmd_baselines(args: argparse.Namespace) -> int:
    """Run a flow in baseline-update mode: screenshot_matches assertions
    save their baselines instead of comparing."""
    from qaloop import ledger
    runs_root = default_runs_root()
    os.makedirs(runs_root, exist_ok=True)
    try:
        flow_name, services = _flow_services_info(args.flow)
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
    run_dir = new_run_dir(runs_root, flow_name + "-baselines")
    # Load the spec defensively (issue #200): a matrix flow dispatches
    # per combination, and a malformed flow must still exit 2 via
    # _cmd_baselines_run's own spec handling, never crash here.
    try:
        spec = load_spec(args.flow)
    except Exception:
        spec = None
    ctx = _service_ctx(args.flow, services, run_dir)
    try:
        ctx.__enter__()
    except (TimeoutError, RuntimeError, ValueError) as e:
        print(f"SERVICE BOOT FAILED: {type(e).__name__}: {e}")
        return 3
    try:
        return _cmd_baselines_run(args, run_dir, spec, ledger, runs_root)
    finally:
        ctx.__exit__(None, None, None)


def _cmd_baselines_run(args: argparse.Namespace, run_dir: str, spec=None,
                       ledger=None, runs_root: str = None) -> int:
    from qaloop.env import resolve_upload_dir
    if spec is None:
        try:
            spec = load_spec(args.flow)
        except SpecError as e:
            print(f"INVALID SPEC: {e}")
            return 2
    if getattr(spec, "matrix", None):
        # Issue #200: matrix flows run once per combination, each in
        # baseline-update mode — same dispatch as verify's matrix path.
        print(f"run dir: {run_dir}")
        print(f"target:  {args.target or spec.target}")
        print("baseline update mode — screenshots saved, not compared")
        code, _rec = _cmd_verify_matrix(
            args, spec, run_dir, ledger, runs_root, 0,
            resolve_upload_dir(getattr(args, "upload_dir", None)),
            prune=False, baseline_update=True)
        return code
    target = args.target or spec.target
    print(f"run dir: {run_dir}")
    print(f"target:  {target}")
    print("baseline update mode — screenshots saved, not compared")
    result = run_flow(spec, run_dir=run_dir, target=target,
                      headless=not args.headed,
                      executable_path=args.executable_path,
                      upload_dir=resolve_upload_dir(
                          getattr(args, "upload_dir", None)),
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
    print(f"cost: tokens {cost.get('tokens_in', 0)}/{cost.get('tokens_out', 0)}  "
          f"est ${cost.get('cost_usd_est', 0):.6f}")
    print(f"report: {md_path}")
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    from qaloop import queue as q
    import json as _json
    if args.max_retries < 0:
        print(f"error: --max-retries must be >= 0 (got {args.max_retries})")
        return 2
    try:
        payload = _json.loads(args.payload or "{}")
    except ValueError:
        print("error: --payload is not valid JSON")
        return 2
    if not isinstance(payload, dict):
        print("error: --payload must be a JSON object")
        return 2
    if args.kind == "verify-flow":
        payload.setdefault("flow", args.flow)
        if args.target:
            payload["target"] = args.target
    job_id = q.enqueue(args.kind, payload, max_retries=args.max_retries)
    print(f"enqueued job #{job_id} ({args.kind}, max_retries={args.max_retries})")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from qaloop.env import resolve_keep_runs, resolve_upload_dir
    from qaloop.worker import run_worker
    try:
        keep_runs = resolve_keep_runs(args.keep_runs)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2
    if args.concurrency < 1:
        print(f"ERROR: --concurrency must be >= 1 (got {args.concurrency})")
        return 2
    if args.job_timeout <= 0:
        print(f"ERROR: --job-timeout must be > 0 (got {args.job_timeout})")
        return 2
    upload_dir = resolve_upload_dir(getattr(args, "upload_dir", None))
    run_worker(flows_dir=args.flows, runs_root=default_runs_root(),
               once=args.once, poll_s=args.poll, headless=not args.headed,
               executable_path=args.executable_path, keep_runs=keep_runs,
               concurrency=args.concurrency, job_timeout_s=args.job_timeout,
               upload_dir=upload_dir)
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


def cmd_compare(args: argparse.Namespace) -> int:
    """Diff two runs: status change, per-step outcomes, duration deltas.

    Exit 0 when the runs are identical, 1 when they differ (like diff),
    2 on error (missing/corrupt run dir, ambiguous run id)."""
    from qaloop.compare import (CompareError, compare_runs, render_text,
                                resolve_run)
    runs_root = default_runs_root()
    try:
        a_dir = resolve_run(args.runA, runs_root)
        b_dir = resolve_run(args.runB, runs_root)
        diff = compare_runs(a_dir, b_dir)
    except CompareError as e:
        print(f"ERROR: {e}")
        return 2
    print(render_text(diff))
    return 0 if diff["identical"] else 1


def cmd_ledger(args: argparse.Namespace) -> int:
    from qaloop import ledger
    import json as _json
    try:
        summary = ledger.summarize()
    except ValueError as e:
        print(f"error: {e}")
        return 2
    print(_json.dumps(summary, indent=2))
    return 0


# Issue #126: demo fixtures needed by the documented demo proof
# (GUIDANCE section 7: flows/demo-mock-ux.yaml runs against /demo/mock-demo.html
# served from the repo root, plus the fixture pages referenced across
# flows/tests).
_DOCTOR_DEMO_FIXTURES = (
    "mock-demo.html",
    "demo-login.html",
    "demo-a11y.html",
    "cookie-storage.html",
)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Issue #126: environment self-check. Prints one PASS/FAIL line per
    check plus a machine-readable summary. Exit 0 when everything passes,
    exit 2 on any FAIL so a broken environment never silently passes
    (mirrors the batch-verify convention: exit 2 = broken configuration)."""
    import shutil
    import importlib

    results: list[tuple[str, str, str]] = []  # (name, status, detail)

    # 1. Chromium executable - mirrors run_flow's resolution (runner.py:1026):
    #    QALOOP_EXECUTABLE_PATH wins; otherwise Playwright's default.
    #    The executable_path property is only read - no browser is launched.
    env_exe = os.environ.get("QALOOP_EXECUTABLE_PATH")
    if env_exe:
        if os.path.exists(env_exe):
            results.append(("chromium", "PASS", env_exe))
        else:
            results.append(("chromium", "FAIL",
                            "QALOOP_EXECUTABLE_PATH points at a missing file: "
                            f"{env_exe}"))
    else:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                default_exe = pw.chromium.executable_path
        except Exception as e:  # noqa: BLE001
            results.append(("chromium", "FAIL",
                            "could not resolve the Playwright chromium "
                            f"executable: {e}"))
        else:
            if os.path.exists(default_exe):
                results.append(("chromium", "PASS", default_exe))
            else:
                results.append(("chromium", "FAIL",
                                "Playwright chromium executable missing: "
                                f"{default_exe} "
                                "(fix: python3 -m playwright install chromium)"))

    # 2. ffmpeg - FAIL, not WARN: verify --video is a documented framework
    #    capability and a missing ffmpeg breaks it cryptically.
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        results.append(("ffmpeg", "PASS", ffmpeg))
    else:
        results.append(("ffmpeg", "FAIL",
                        "ffmpeg not found on PATH; verify --video cannot "
                        "record video.webm (fix: install ffmpeg)"))

    # 3. Python packages.
    for pkg in ("yaml", "playwright"):
        try:
            importlib.import_module(pkg)
        except Exception:  # noqa: BLE001
            results.append(("python", "FAIL",
                            f"missing Python package: {pkg}"))
            break
    else:
        results.append(("python", "PASS", "yaml and playwright importable"))

    # 4. demo/ fixtures at the repo root - needed by the documented demo proof.
    demo_dir = os.path.join(QALOOP_HOME, "demo")
    missing = [f for f in _DOCTOR_DEMO_FIXTURES
               if not os.path.isfile(os.path.join(demo_dir, f))]
    if os.path.isdir(demo_dir) and not missing:
        results.append(("demo", "PASS",
                        f"{demo_dir} "
                        f"({len(_DOCTOR_DEMO_FIXTURES)} fixtures present)"))
    else:
        results.append(("demo", "FAIL",
                        f"demo fixture dir broken: {demo_dir} "
                        "(missing: "
                        f"{', '.join('demo/' + f for f in missing) or 'the directory itself'}; "
                        "needed by the documented demo proof)"))

    for name, status, detail in results:
        print(f"{status} {name}: {detail}")
    print("doctor: " + " ".join(f"{name}={status}"
                                for name, status, _ in results))
    return 2 if any(status == "FAIL" for _, status, _ in results) else 0


def cmd_investigate(args: argparse.Namespace) -> int:
    import json as _json
    from qaloop.investigate import investigate as run_investigation
    from qaloop.runner import RunResult
    from qaloop.spec import load_spec
    if not os.path.isdir(args.run):
        print(f"ERROR: run dir not found: {args.run}")
        return 2
    run_json = os.path.join(args.run, "run.json")
    try:
        with open(run_json, encoding="utf-8") as f:
            result = RunResult.from_dict(_json.load(f))
    except FileNotFoundError:
        print(f"ERROR: run.json not found: {run_json}")
        return 2
    except ValueError:
        print(f"ERROR: run.json is not valid JSON: {run_json}")
        return 2
    from qaloop.spec import SpecError
    try:
        spec = load_spec(args.flow, strict_env=False)
    except FileNotFoundError:
        print(f"ERROR: flow not found: {args.flow}")
        return 2
    except SpecError as e:
        print(f"INVALID SPEC: {e}")
        return 2
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

    r = sub.add_parser("verify",
                       help="run a flow (or a directory of flows) against a target")
    r.add_argument("flow", help="path to flow YAML or directory of flow YAMLs")
    r.add_argument("--target", help="override spec target URL")
    r.add_argument("--tag", action="append", default=None,
                   help="only run flows carrying this tag (repeatable; OR "
                        "semantics, exact case-sensitive match)")
    r.add_argument("--headed", action="store_true", help="show the browser")
    r.add_argument("--upload-dir", default=None,
                   help="directory file uploads are restricted to "
                        "(env QALOOP_UPLOAD_DIR; fail-closed if neither "
                        "is declared)")

    r.add_argument("--executable-path", default=None,
                   help="chromium executable (default: playwright's)")
    r.add_argument("--investigate", action="store_true",
                   help="summon the agentic investigator on failure")
    r.add_argument("--max-investigation-actions", type=int, default=None)
    r.add_argument("--video", action="store_true",
                   help="record the run as video.webm in the run dir "
                        "(issue #90; opt-in, default off)")
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
    w.add_argument("--concurrency", type=int, default=1,
                   help="parallel job claims (default 1 = serial)")
    w.add_argument("--job-timeout", type=float, default=1800,
                   help="per-job timeout in seconds (default 1800); a job "
                        "still running after this is recorded failed with a "
                        "timeout note (payload 'timeout_s' overrides)")
    w.add_argument("--headed", action="store_true")
    w.add_argument("--executable-path", default=None)
    w.add_argument("--keep-runs", type=int, default=None,
                   help="keep at most N newest run dirs (env QALOOP_KEEP_RUNS; "
                        "default 0 = keep everything)")
    w.add_argument("--upload-dir", default=None,
                   help="directory file uploads are restricted to "
                        "(env QALOOP_UPLOAD_DIR; fail-closed if neither "
                        "is declared)")
    w.set_defaults(fn=cmd_worker)

    h = sub.add_parser("webhook", help="run the GitHub webhook receiver")
    h.add_argument("--port", type=int, default=8090)
    h.add_argument("--host", default="127.0.0.1")
    h.set_defaults(fn=cmd_webhook)

    d = sub.add_parser("dashboard", help="regenerate the static dashboard")
    d.add_argument("--out", default=os.path.join(QALOOP_HOME, "dashboard"))
    d.set_defaults(fn=cmd_dashboard)

    c = sub.add_parser("compare",
                       help="diff two runs: step outcomes + durations")
    c.add_argument("runA", help="run dir, or a run-id prefix matched "
                   "inside runs/")
    c.add_argument("runB", help="run dir, or a run-id prefix matched "
                   "inside runs/")
    c.set_defaults(fn=cmd_compare)

    lg = sub.add_parser("ledger", help="print the cost ledger summary")
    lg.set_defaults(fn=cmd_ledger)

    dr = sub.add_parser("doctor",
                        help="self-check the environment (chromium, ffmpeg, "
                             "python packages, demo fixtures)")
    dr.set_defaults(fn=cmd_doctor)

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
    bl.add_argument("--upload-dir", default=None,
                    help="directory file uploads are restricted to "
                         "(env QALOOP_UPLOAD_DIR; fail-closed if neither "
                         "is declared)")
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
