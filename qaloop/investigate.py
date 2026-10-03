"""Agentic investigator: a bounded ReAct browser agent summoned on flow failure.

Cost design (this is the expensive part — used sparingly):
- text-first observations: the accessibility tree via CDP is the primary
  "eyes" (~10x cheaper than screenshot-every-step); screenshots are saved
  for the human report, not fed to the model.
- JSON-in-text tool protocol: works with any chat model, no function-calling
  API needed. Any OpenAI-compatible /chat/completions endpoint works
  (hosted cheap models, OpenRouter, local Ollama, ...).
- hard action budget (default 20); then it must write its diagnosis.
- no model credentials -> manual mode: writes INVESTIGATION_BRIEF.md with
  the full evidence bundle for a human (or another agent) to execute.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from .artifacts import ax_snapshot
from .runner import RunResult
from .spec import FlowSpec

SYSTEM_PROMPT = """\
You are qaloop's investigator: a browser agent that diagnoses WHY a \
deterministic UI flow failed. You are not fixing code; you are producing a \
diagnosis a code agent can act on.

Rules:
- You have a strict action budget (given below). Spend it on the failing \
area, not on re-running the whole flow.
- Observe with ax_snapshot (cheap text). Only request screenshots when you \
need to see visual layout; they are saved for the human report.
- Prefer read-only exploration. You may click/fill/type to reproduce the \
failure, but do not submit payments, delete data, or exfiltrate anything.
- When you have a diagnosis, call done. If the budget runs out you will be \
asked for a final diagnosis.

Every reply must be exactly one JSON object, no other text:
{"thought": "<what you learned / plan>", "tool": "<tool>", "args": {...}}

Tools:
- ax_snapshot {} -> accessibility tree of the current page (text)
- goto {"url": ...} -> navigate
- click {"target": "<css/text=/role= selector>"} -> click
- fill {"target": ..., "text": ...} -> fill an input
- press {"target": ..., "key": ...} -> keyboard press
- screenshot {"note": ...} -> save a PNG for the human report
- console {} -> recent console/page errors
- network {} -> failed requests and HTTP>=400 responses
- done {"diagnosis": "...", "likely_cause": "...", "suggested_fix": "..."} \
-> finish with your diagnosis
"""


def _model_config() -> dict:
    return {
        "base_url": os.environ.get("QALOOP_MODEL_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "name": os.environ.get("QALOOP_MODEL_NAME", "gpt-4o-mini"),
        "api_key": os.environ.get("QALOOP_MODEL_API_KEY", ""),
        "price_in": float(os.environ.get("QALOOP_MODEL_PRICE_IN", "0.15")),
        "price_out": float(os.environ.get("QALOOP_MODEL_PRICE_OUT", "0.60")),
    }


def _chat(cfg: dict, messages: list[dict], timeout_s: int = 90) -> tuple[str, dict]:
    body = json.dumps({
        "model": cfg["name"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1500,
    }).encode()
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.load(resp)
    choice = data["choices"][0]["message"]
    usage = data.get("usage", {})
    return choice.get("content") or "", usage


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def write_manual_brief(run_dir: str, spec: FlowSpec, result: RunResult,
                       target: str) -> str:
    """No model configured: write the evidence bundle + bounded brief."""
    s = result.steps[result.failed_step] if result.failed_step is not None else None
    lines = [
        "# Investigation brief (manual mode)",
        "",
        "No model is configured (`QALOOP_MODEL_API_KEY` unset), so this",
        "investigation is handed to a human or another agent.",
        "",
        f"Flow: `{spec.name}` — target `{target}`",
        "",
    ]
    if s:
        lines += [f"## Failing step: {s.name} (`{s.phase}[{s.index}]`, op `{s.op}`)",
                  "", f"Error: `{s.error}`", ""]
        for a in s.assertions:
            mark = "ok" if a.passed else "FAIL"
            lines.append(f"- [{mark}] `{a.name}` — {a.detail}")
        lines.append("")
        if s.artifacts.screenshot:
            lines.append(f"Screenshot: `{os.path.relpath(s.artifacts.screenshot, run_dir)}`")
        if s.artifacts.ax_snapshot:
            lines.append(f"AX snapshot: `{os.path.relpath(s.artifacts.ax_snapshot, run_dir)}`")
        lines.append("")
    for title, items, key in (
            ("Console errors", result.console_errors, "text"),
            ("Page errors", result.page_errors, "text"),
            ("Failed requests", result.failed_requests, "url"),
            ("Bad responses", result.bad_responses, "url")):
        if items:
            lines.append(f"## {title}")
            lines.append("")
            for it in items[:10]:
                lines.append(f"- `{str(it.get(key, ''))[:160]}`")
            lines.append("")
    lines += [
        "## Brief",
        "",
        f"You have 20 browser actions. Start at {target}, reproduce the failing",
        "step above, and return: (1) what the user sees vs. what was expected,",
        "(2) the likely cause with the evidence that supports it (console error,",
        "failed request, DOM state), (3) a suggested fix for the code agent.",
        "Prefer the accessibility tree over screenshots. Do not submit payments",
        "or delete data.",
    ]
    path = os.path.join(run_dir, "INVESTIGATION_BRIEF.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


class Investigator:
    def __init__(self, page, run_dir: str, max_actions: int):
        self.page = page
        self.run_dir = run_dir
        self.max_actions = max_actions
        self.actions = 0
        self.shots = 0
        self.console: list[dict] = []
        self.network: list[dict] = []
        page.on("console", lambda m: self.console.append(
            {"type": m.type, "text": m.text[:500]}) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.console.append(
            {"type": "pageerror", "text": str(e)[:500]}))
        page.on("requestfailed", lambda r: self.network.append(
            {"kind": "failed", "method": r.method, "url": r.url[:300]}))
        page.on("response", lambda r: self.network.append(
            {"kind": "bad", "method": r.request.method,
             "url": r.url[:300], "status": r.status}) if r.status >= 400 else None)

    def tool(self, name: str, args: dict) -> str:
        self.actions += 1
        p = self.page
        if name == "ax_snapshot":
            return ax_snapshot(p, max_nodes=400)[:30000]
        if name == "goto":
            p.goto(args["url"], timeout=20000, wait_until="domcontentloaded")
            return f"navigated to {p.url}"
        if name == "click":
            p.click(args["target"], timeout=10000)
            return "clicked"
        if name == "fill":
            p.fill(args["target"], args["text"], timeout=10000)
            return "filled"
        if name == "press":
            p.press(args["target"], args["key"], timeout=10000)
            return "pressed"
        if name == "screenshot":
            self.shots += 1
            path = os.path.join(self.run_dir, "steps", f"inv-{self.shots:02d}.png")
            p.screenshot(path=path)
            return f"screenshot saved to {os.path.basename(path)} (for the human report)"
        if name == "console":
            items = self.console[-15:]
            return json.dumps(items, ensure_ascii=False) or "no console/page errors"
        if name == "network":
            items = self.network[-15:]
            return json.dumps(items, ensure_ascii=False) or "no failed requests"
        raise ValueError(f"unknown tool {name}")


def investigate(*, result: RunResult, spec: FlowSpec, target: str,
                max_actions: int = 20, headless: bool = True,
                executable_path: str | None = None,
                run_dir: str) -> dict | None:
    """Run the investigator. Returns a diagnosis dict, or None on harness error."""
    cfg = _model_config()
    if not cfg["api_key"]:
        brief = write_manual_brief(run_dir, spec, result, target)
        print(f"no model configured — manual brief written to {brief}")
        return {"mode": "manual", "brief": brief, "model": None,
                "tokens_in": 0, "tokens_out": 0, "cost_usd_est": 0.0}

    from playwright.sync_api import sync_playwright

    s = result.steps[result.failed_step] if result.failed_step is not None else None
    context_bits = [
        f"Flow: {spec.name}",
        f"Target: {target}",
        f"Failing step: {s.name} (op {s.op})" if s else "no step info",
        f"Error: {s.error}" if s else "",
    ]
    if s:
        for a in s.assertions:
            if not a.passed:
                context_bits.append(f"Failed assertion {a.name}: {a.detail}")
    for e in (result.page_errors + result.console_errors)[:5]:
        context_bits.append(f"Console/page error: {e.get('text', '')[:200]}")
    for e in (result.failed_requests + result.bad_responses)[:5]:
        context_bits.append(f"Network: {e.get('method', '')} {e.get('url', '')[:150]}")
    first_user = ("Diagnose this failure. Budget: "
                  f"{max_actions} actions.\n" + "\n".join(context_bits))

    transcript: list[str] = [f"# Investigation transcript — {spec.name}\n"]
    tokens_in = tokens_out = 0
    diagnosis: dict | None = None

    def note(line: str) -> None:
        transcript.append(line)

    with sync_playwright() as pw:
        launch_kw: dict = {"headless": headless}
        if executable_path:
            launch_kw["executable_path"] = executable_path
        browser = pw.chromium.launch(**launch_kw)
        try:
            page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
            inv = Investigator(page, run_dir, max_actions)
            page.goto(target, timeout=20000, wait_until="domcontentloaded")
            opening_ax = inv.tool("ax_snapshot", {})
            note(f"## opening ax snapshot\n```\n{opening_ax[:8000]}\n```\n")

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": first_user},
            ]
            for turn in range(max_actions + 2):
                raw, usage = _chat(cfg, messages)
                tokens_in += usage.get("prompt_tokens", 0)
                tokens_out += usage.get("completion_tokens", 0)
                note(f"## turn {turn} (model)\n```json\n{raw[:2000]}\n```\n")
                try:
                    call = _extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content":
                        f"That was not valid JSON ({e}). Reply with exactly one JSON tool call."})
                    continue
                messages.append({"role": "assistant", "content": raw})
                tool, args = call.get("tool"), call.get("args") or {}
                thought = call.get("thought", "")
                note(f"thought: {thought}\n")
                if tool == "done":
                    diagnosis = {"diagnosis": args.get("diagnosis", ""),
                                 "likely_cause": args.get("likely_cause", ""),
                                 "suggested_fix": args.get("suggested_fix", "")}
                    note(f"## diagnosis\n{json.dumps(diagnosis, ensure_ascii=False, indent=2)}\n")
                    break
                if inv.actions >= max_actions:
                    messages.append({"role": "user", "content":
                        "Budget exhausted. Call done with your best diagnosis now."})
                    continue
                try:
                    obs = inv.tool(tool, args)
                except Exception as e:  # noqa: BLE001
                    obs = f"tool error: {type(e).__name__}: {str(e)[:300]}"
                note(f"### {tool} ->\n```\n{obs[:6000]}\n```\n")
                messages.append({"role": "user", "content":
                                 f"tool {tool} returned:\n{obs[:8000]}"})
            else:
                note("loop ended without done\n")
        finally:
            browser.close()

    if diagnosis is None:
        diagnosis = {"diagnosis": "Investigator exhausted its budget without a conclusion.",
                     "likely_cause": "unknown", "suggested_fix": ""}
    cost = (tokens_in / 1e6) * cfg["price_in"] + (tokens_out / 1e6) * cfg["price_out"]
    diagnosis.update({"mode": "agent", "model": cfg["name"],
                      "tokens_in": tokens_in, "tokens_out": tokens_out,
                      "cost_usd_est": round(cost, 4),
                      "actions_taken": inv.actions if "inv" in dir() else 0})
    with open(os.path.join(run_dir, "INVESTIGATION.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(transcript))
    return diagnosis
