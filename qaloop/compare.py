"""Run comparison: diff two run dirs (issue #91).

Structured diff of two run.json artifacts: top-level status change and
duration delta, plus a per-step diff keyed by step name (added, removed,
status changes, per-step duration deltas with regression flags), and a
human-readable text render. Read-only over run dirs; no browser.
"""
from __future__ import annotations

import glob
import json
import os

# A step is flagged as a duration regression when run B took more than
# this fraction longer than run A (20% slower).
DURATION_REGRESSION_THRESHOLD = 0.20


class CompareError(Exception):
    """Named error for compare failures (the CLI maps it to exit 2)."""


def _load_run_json(run_dir: str) -> dict:
    path = os.path.join(run_dir, "run.json")
    if not os.path.isfile(path):
        raise CompareError(f"no run.json in {run_dir}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise CompareError(f"corrupt run.json in {run_dir}: {e}")
    if not isinstance(data, dict):
        raise CompareError(f"corrupt run.json in {run_dir}: top level is not an object")
    return data


def resolve_run(ref: str, runs_root: str) -> str:
    """Resolve a run reference to a run dir.

    A direct dir path is used as-is; otherwise the reference is matched
    as a run-dir basename prefix inside runs_root. Ambiguous prefixes
    and non-matches raise CompareError.
    """
    if os.path.isdir(ref):
        return os.path.abspath(ref)
    matches = sorted(
        d for d in glob.glob(os.path.join(runs_root, "*"))
        if os.path.isdir(d) and os.path.basename(d).startswith(ref))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CompareError(f"no run dir matching {ref!r} in {runs_root}")
    raise CompareError(f"ambiguous run id {ref!r}: {len(matches)} matches "
                       f"(first: {os.path.basename(matches[0])})")


def _step_map(run: dict) -> dict:
    return {s.get("name", "?"): s for s in run.get("steps", [])}


def _duration_delta(a_ms, b_ms) -> dict:
    da = a_ms or 0
    db = b_ms or 0
    delta = db - da
    regression = da > 0 and db > da * (1 + DURATION_REGRESSION_THRESHOLD)
    return {"duration_a_ms": da, "duration_b_ms": db,
            "duration_delta_ms": delta, "duration_regression": regression}


def compare_runs(a_dir: str, b_dir: str) -> dict:
    """Diff two run dirs. Returns a structured diff dict; identical is
    True only when the status and every step's outcome are unchanged
    (duration drift alone does not break identicalness)."""
    a = _load_run_json(a_dir)
    b = _load_run_json(b_dir)
    status_changed = a.get("status") != b.get("status")
    top = _duration_delta(a.get("duration_ms"), b.get("duration_ms"))
    sm_a, sm_b = _step_map(a), _step_map(b)
    steps = []
    for name in sm_a.keys() | sm_b.keys():
        sa, sb = sm_a.get(name), sm_b.get(name)
        if sa is None:
            change = "added"
        elif sb is None:
            change = "removed"
        elif sa.get("status") != sb.get("status"):
            change = "status_changed"
        else:
            change = "unchanged"
        d = _duration_delta(sa.get("duration_ms") if sa else None,
                            sb.get("duration_ms") if sb else None)
        steps.append({"name": name, "change": change,
                      "status_a": sa.get("status") if sa else None,
                      "status_b": sb.get("status") if sb else None, **d})
    steps.sort(key=lambda s: s["name"])
    identical = (not status_changed
                 and all(s["change"] == "unchanged" for s in steps))
    return {"run_a": os.path.basename(os.path.normpath(a_dir)),
            "run_b": os.path.basename(os.path.normpath(b_dir)),
            "status_a": a.get("status"), "status_b": b.get("status"),
            "status_changed": status_changed,
            "duration_a_ms": top["duration_a_ms"],
            "duration_b_ms": top["duration_b_ms"],
            "duration_delta_ms": top["duration_delta_ms"],
            "steps": steps, "identical": identical}


def render_text(diff: dict) -> str:
    """Human-readable rendering of a compare_runs diff."""
    lines = [f"compare: {diff['run_a']} -> {diff['run_b']}"]
    marker = "CHANGED" if diff["status_changed"] else "same"
    lines.append(f"status: {diff['status_a']} -> {diff['status_b']} ({marker})")
    lines.append(f"duration: {diff['duration_a_ms']}ms -> "
                 f"{diff['duration_b_ms']}ms "
                 f"({diff['duration_delta_ms']:+d}ms)")
    changed = [s for s in diff["steps"] if s["change"] != "unchanged"]
    if not changed:
        lines.append("steps: all unchanged")
    for s in changed:
        if s["change"] == "added":
            lines.append(f"  + step {s['name']!r}: added in B "
                         f"({s['status_b']})")
        elif s["change"] == "removed":
            lines.append(f"  - step {s['name']!r}: removed in B "
                         f"(was {s['status_a']})")
        else:
            lines.append(f"  ! step {s['name']!r}: "
                         f"{s['status_a']} -> {s['status_b']}")
        reg = " REGRESSION" if s["duration_regression"] else ""
        if s["change"] in ("added", "removed") or s["duration_delta_ms"] or reg:
            lines.append(f"    duration: {s['duration_a_ms']}ms -> "
                         f"{s['duration_b_ms']}ms "
                         f"({s['duration_delta_ms']:+d}ms){reg}")
    lines.append("identical" if diff["identical"] else "differ")
    return "\n".join(lines)
