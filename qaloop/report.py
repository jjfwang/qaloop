"""Report card: RunResult -> REPORT.md (human) + stdout summary."""
from __future__ import annotations

import os
import re
import json
from xml.sax.saxutils import escape

from .artifacts import save_json
from .runner import RunResult
from .spec import FlowSpec

# Characters illegal in XML 1.0 (surrogates, control chars other than tab/LF/CR)
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


def _xml_text(text: str | None) -> str:
    """Escape text for XML and strip characters illegal in XML 1.0."""
    return escape(_ILLEGAL_XML.sub("", text or ""))


def _xml_attr(text: str | None) -> str:
    """Escape text for an XML attribute value (quotes too)."""
    return escape(_ILLEGAL_XML.sub("", text or ""), {'"': "&quot;"})


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
    retry_of = {(s.phase, s.index): s.retry
                for s in spec.setup + spec.steps + spec.teardown}
    for i, s in enumerate(result.steps):
        mark = {"passed": "ok", "failed": "FAIL", "skipped": "skip"}[s.status]
        if s.attempts > 1:
            mark = (f"{mark} · attempt {s.attempts}"
                    f"/{retry_of.get((s.phase, s.index), 0) + 1}")
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
    _section("Dialogs", result.dialogs,
             lambda e: f"{e['type']}: `{e['message'][:160]}`")
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
    junit_xml_path = write_junit_xml(result, spec, os.path.join(run_dir, "junit.xml"))
    network_jsonl_path = os.path.join(run_dir, "network.jsonl")
    try:
        with open(network_jsonl_path, "w", encoding="utf-8") as f:
            for entry in result.network_log:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        network_jsonl_path = None  # best effort: never fail the run
    return {"run_json": os.path.join(run_dir, "run.json"),
            "report_md": report_path,
            "junit_xml": junit_xml_path,
            "network_jsonl": network_jsonl_path}


def write_junit_xml(result: RunResult, spec: FlowSpec, path: str) -> str:
    """Write a JUnit XML report: one testsuite per flow, one testcase per step.

    Failed steps get a <failure> element with the step error text; skipped
    steps get a <skipped/> element. Returns the path written.
    """
    n_failed = sum(1 for s in result.steps if s.status == "failed")
    n_skipped = sum(1 for s in result.steps if s.status == "skipped")
    n_errors = 1 if result.error else 0
    total_s = result.duration_ms / 1000.0
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (f'<testsuite name="{_xml_attr(spec.name)}" tests="{len(result.steps)}" '
         f'failures="{n_failed}" skipped="{n_skipped}" errors="{n_errors}" '
         f'time="{total_s:.3f}">'),
    ]
    for s in result.steps:
        classname = f"{s.phase}.{s.op}" if s.op is not None else s.phase
        tc_time = s.duration_ms / 1000.0
        out.append(f'  <testcase name="{_xml_attr(s.name)}" '
                   f'classname="{_xml_attr(classname)}" time="{tc_time:.3f}">')
        if s.status == "failed":
            err_attr = _xml_attr(s.error)
            err_text = _xml_text(s.error)
            out.append(f'    <failure message="{err_attr}">{err_text}</failure>')
        elif s.status == "skipped":
            out.append("    <skipped/>")
        out.append("  </testcase>")
    out.append("</testsuite>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return path


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
