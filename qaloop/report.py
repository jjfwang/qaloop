"""Report card: RunResult -> REPORT.md (human) + stdout summary."""
from __future__ import annotations

import os

from .artifacts import save_json
from .runner import RunResult
from .spec import FlowSpec


def _rel(run_dir: str, path: str | None) -> str | None:
    if not path:
        return None
    return os.path.relpath(path, run_dir)


def write_report(result: RunResult, spec: FlowSpec, run_dir: str,
                 diagnosis: dict | None = None) -> dict[str, str]:
    """Write run.json (already written by runner), REPORT.md. Returns paths."""
    data = result.to_dict()
    data["spec_path"] = spec.source_path
    if diagnosis:
        data["diagnosis"] = diagnosis
    save_json(os.path.join(run_dir, "run.json"), data)

    badge = {"passed": "PASS", "failed": "FAIL", "error": "ERROR"}[result.status]
    lines = [
        f"# qaloop report — {spec.name} [{badge}]",
        "",
        f"- target: `{result.target}`",
        f"- duration: {result.duration_ms}ms",
        f"- steps: {sum(1 for s in result.steps if s.status == 'passed')}"
        f"/{len(result.steps)} passed",
        f"- run dir: `{run_dir}`",
        "",
        "## Steps",
        "",
        "| # | phase | step | op | status | ms |",
        "|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(result.steps):
        mark = {"passed": "ok", "failed": "FAIL", "skipped": "skip"}[s.status]
        lines.append(
            f"| {i} | {s.phase} | {s.name} | {s.op or '—'} | {mark} | {s.duration_ms} |")
    lines.append("")

    if result.failed_step is not None:
        s = result.steps[result.failed_step]
        lines += [
            "## Failing step",
            "",
            f"**{s.name}** (`{s.phase}[{s.index}]`, op `{s.op}`)",
            "",
            f"Error: `{s.error}`",
            "",
        ]
        for a in s.assertions:
            mark = "ok" if a.passed else "FAIL"
            lines.append(f"- [{mark}] `{a.name}` — {a.detail}")
        lines.append("")
        if s.artifacts.screenshot:
            lines.append(f"Screenshot: `{_rel(run_dir, s.artifacts.screenshot)}`")
        if s.artifacts.ax_snapshot:
            lines.append(f"AX snapshot: `{_rel(run_dir, s.artifacts.ax_snapshot)}`")
        lines.append("")

    def _section(title: str, items: list[dict], fmt) -> None:
        if not items:
            return
        lines.append(f"## {title} ({len(items)})")
        lines.append("")
        for it in items[:20]:
            lines.append(f"- {fmt(it)}")
        if len(items) > 20:
            lines.append(f"- … and {len(items) - 20} more (see run.json)")
        lines.append("")

    _section("Console errors", result.console_errors,
             lambda e: f"`{e['text'][:160]}`")
    _section("Page errors", result.page_errors,
             lambda e: f"`{e['text'][:160]}`")
    _section("Failed requests", result.failed_requests,
             lambda e: f"{e['method']} `{e['url'][:120]}` — {e['failure']}")
    _section("Bad responses (HTTP ≥ 400)", result.bad_responses,
             lambda e: f"{e['method']} `{e['url'][:120]}` → {e['status']}")

    if diagnosis:
        lines += [
            "## Investigator diagnosis",
            "",
            f"**Likely cause:** {diagnosis.get('likely_cause', '—')}",
            "",
            diagnosis.get("diagnosis", ""),
            "",
        ]
        if diagnosis.get("suggested_fix"):
            lines += [f"**Suggested fix:** {diagnosis['suggested_fix']}", ""]

    if result.error:
        lines += [f"Run error: `{result.error}`", ""]

    report_path = os.path.join(run_dir, "REPORT.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return {"run_json": os.path.join(run_dir, "run.json"),
            "report_md": report_path}


def print_summary(result: RunResult) -> None:
    badge = {"passed": "PASS", "failed": "FAIL", "error": "ERROR"}[result.status]
    print(f"[{badge}] {result.flow_name} — {result.duration_ms}ms, "
          f"{sum(1 for s in result.steps if s.status == 'passed')}/{len(result.steps)} steps")
    if result.failed_step is not None:
        s = result.steps[result.failed_step]
        print(f"  failing step #{result.failed_step}: {s.name}")
        print(f"  error: {s.error[:300]}")
    if result.page_errors:
        print(f"  page errors: {len(result.page_errors)}")
    if result.failed_requests or result.bad_responses:
        print(f"  network issues: {len(result.failed_requests)} failed, "
              f"{len(result.bad_responses)} bad responses")
