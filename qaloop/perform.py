"""perform: a natural-language browser task agent.

Takes over a browser (fresh or an already-running one via CDP) and performs
a task the way a careful person would: reading the page's accessibility
tree, acting semantically (role/name/text), checking state after each
action, and stopping honestly when blocked.

Human-like here means robust and legible — real clicks, paced typing,
scroll-into-view, waits for state — not bot-evasion or fingerprint
spoofing. qaloop has no interest in pretending to be a different human;
it acts like one careful, transparent operator.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
import urllib.request

from .artifacts import ax_snapshot, new_run_dir, save_json

SYSTEM = """You are qaloop's performer: a browser agent that completes the user's
task the way a careful person would. You see the page as an accessibility
tree (what a screen-reader user perceives) plus the URL and title.

Reply with exactly one JSON action per turn and nothing else:

{"action": "navigate", "url": "https://..."}            go to a page
{"action": "click", "target": {"role": "button", "name": "Save"}}
{"action": "dblclick", "target": {"role": "link", "name": "Docs"}}
{"action": "fill", "target": {"role": "textbox", "name": "Email"}, "text": "a@b.c"}
{"action": "press", "key": "Enter"}                      or with "target"
{"action": "select", "target": {"role": "combobox", "name": "City"}, "value": "sg"}
{"action": "check", "target": {"role": "checkbox", "name": "Remember me"}}
{"action": "uncheck", "target": {...}}
{"action": "hover", "target": {"role": "menuitem", "name": "File"}}
{"action": "scroll", "direction": "down"}                or "up", optional "target"
{"action": "wait", "target": {"text": "Welcome"}, "state": "visible"}
    state: visible | hidden | attached (default visible); "text" waits for
    that text inside the target, otherwise waits for the element state.
{"action": "screenshot"}                                look at the page visually
{"action": "console"}                                   recent console/page errors
{"action": "done", "summary": "what was accomplished"}
{"action": "blocked", "reason": "why you cannot proceed"}

A target is {"role": ..., "name": ...} (preferred), {"text": "..."}, or as a
last resort {"css": "..."}. Prefer the semantic forms.

Rules:
- Read before you act. After each action, verify the outcome in the next
  observation; never assume a click worked.
- Fill forms the way a person does: one field at a time, then submit, then
  check for confirmation.
- Never submit a payment, delete or destroy data, or publish/post externally.
  If the task (or a page along the way) asks for any of these, stop with
  "blocked" and say why. A confirmation dialog for something destructive is
  a stop sign, not a speed bump.
- If an element matches several things, disambiguate with a better name —
  don't click the first one blindly.
- Keep it tight: you have at most {max_actions} actions. When the task's
  goal is visibly achieved, finish with "done".
- If the page is broken, the goal is impossible, or you're going in circles,
  say "blocked" honestly instead of guessing.
"""

# Harness-level stop signs: the model is told the rules, and the harness
# enforces them too so a misbehaving model can't click through.
_DESTRUCTIVE = re.compile(
    r"\b(pay|purchase|checkout|buy now|delete|remove|destroy|erase|wipe)\b",
    re.IGNORECASE)
_PUBLISH = re.compile(
    r"\b(publish|post|tweet|share|send invite)\b", re.IGNORECASE)


def _model_config() -> dict:
    return {
        "base_url": os.environ.get("QALOOP_MODEL_BASE_URL",
                                   "https://api.openai.com/v1").rstrip("/"),
        "name": os.environ.get("QALOOP_MODEL_NAME", "gpt-4o-mini"),
        "api_key": os.environ.get("QALOOP_MODEL_API_KEY", ""),
        "price_in": float(os.environ.get("QALOOP_MODEL_PRICE_IN", "0.15")),
        "price_out": float(os.environ.get("QALOOP_MODEL_PRICE_OUT", "0.60")),
    }


def _chat(cfg: dict, messages: list[dict], timeout_s: int = 90) -> tuple[str, dict]:
    body = json.dumps({
        "model": cfg["name"], "messages": messages,
        "temperature": 0.2, "max_tokens": 1200,
    }).encode()
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.load(resp)
    choice = data["choices"][0]["message"]
    return choice.get("content") or "", data.get("usage", {})


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return a JSON action")
    return json.loads(text[start:end + 1])


class Performer:
    """Executes one natural-language task in a browser page."""

    def __init__(self, page, run_dir: str, max_actions: int,
                 allow_publish: bool = False, seed: int = 0):
        self.page = page
        self.run_dir = run_dir
        self.max_actions = max_actions
        self.allow_publish = allow_publish
        self.rng = random.Random(seed)
        self.n = 0
        self.transcript: list[dict] = []
        self.console: list[dict] = []
        self.dialogs: list[str] = []
        self.tokens_in = 0
        self.tokens_out = 0
        page.on("console", lambda m: self.console.append(
            {"type": m.type, "text": m.text[:300]}) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.console.append(
            {"type": "pageerror", "text": str(e)[:300]}))
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog):
        # Destructive confirmations are a stop sign: record, dismiss, tell the model.
        self.dialogs.append(f"{dialog.type}: {dialog.message[:300]}")
        dialog.dismiss()

    def _pace(self):
        # A person pauses between actions; keep it modest and deterministic-ish.
        time.sleep(0.25 + self.rng.random() * 0.5)

    def _resolve(self, target: dict):
        p = self.page
        if not isinstance(target, dict):
            raise ValueError("target must be an object")
        if "role" in target:
            loc = p.get_by_role(target["role"], name=target.get("name"))
        elif "text" in target:
            loc = p.get_by_text(target["text"])
        elif "css" in target:
            loc = p.locator(target["css"])
        else:
            raise ValueError("target needs role(+name), text, or css")
        n = loc.count()
        if n == 0:
            raise ValueError(f"no element matches {target}")
        if n > 1:
            raise ValueError(
                f"{n} elements match {target} — disambiguate with a more "
                f"specific name")
        return loc

    def _guard(self, name: str, target: dict | None):
        label = ""
        if target:
            label = str(target.get("name") or target.get("text") or "")
        if name in ("click", "dblclick", "press", "check"):
            if _DESTRUCTIVE.search(label):
                raise PermissionError(
                    f"destructive action blocked by harness: {label!r}")
            if _PUBLISH.search(label) and not self.allow_publish:
                raise PermissionError(
                    f"external publish blocked (re-run with --allow-publish "
                    f"to permit): {label!r}")

    def act(self, action: dict) -> str:
        """Execute one model action; return the observation string."""
        self.n += 1
        name = action.get("action")
        p = self.page
        try:
            if name == "navigate":
                p.goto(action["url"], timeout=25000,
                       wait_until="domcontentloaded")
                out = f"navigated to {p.url}"
            elif name in ("click", "dblclick"):
                target = action["target"]
                self._guard(name, target)
                loc = self._resolve(target).first
                loc.scroll_into_view_if_needed(timeout=5000)
                (loc.dblclick if name == "dblclick" else loc.click)(timeout=10000)
                out = f"{name}ed {target}"
            elif name == "fill":
                loc = self._resolve(action["target"])
                loc.scroll_into_view_if_needed(timeout=5000)
                loc.click(timeout=5000)
                # human-like typing for short text; instant set for long blobs
                text = action["text"]
                if len(text) <= 80:
                    p.keyboard.press("ControlOrMeta+a")
                    p.keyboard.type(text, delay=30 + int(self.rng.random() * 40))
                else:
                    loc.fill(text, timeout=10000)
                out = f"filled {action['target']}"
            elif name == "press":
                if action.get("target"):
                    self._resolve(action["target"]).press(
                        action["key"], timeout=10000)
                else:
                    p.keyboard.press(action["key"])
                out = f"pressed {action['key']}"
            elif name in ("select", "check", "uncheck"):
                loc = self._resolve(action["target"])
                loc.scroll_into_view_if_needed(timeout=5000)
                if name == "select":
                    loc.select_option(action["value"], timeout=10000)
                elif name == "check":
                    loc.check(timeout=10000)
                else:
                    loc.uncheck(timeout=10000)
                out = f"{name} {action['target']}"
            elif name == "hover":
                self._resolve(action["target"]).hover(timeout=10000)
                out = "hovered"
            elif name == "scroll":
                if action.get("target"):
                    self._resolve(action["target"]).scroll_into_view_if_needed(
                        timeout=5000)
                else:
                    d = action.get("direction", "down")
                    p.mouse.wheel(0, 600 if d == "down" else -600)
                out = "scrolled"
            elif name == "wait":
                t = action.get("target")
                state = action.get("state", "visible")
                if t and "text" in t and "role" not in t and "css" not in t:
                    p.get_by_text(t["text"]).first.wait_for(
                        state=state, timeout=15000)
                elif t:
                    self._resolve(t).wait_for(state=state, timeout=15000)
                else:
                    time.sleep(action.get("ms", 1000) / 1000)
                out = "wait satisfied"
            elif name == "screenshot":
                path = os.path.join(self.run_dir, "steps",
                                    f"perform-{self.n:02d}.png")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                p.screenshot(path=path)
                out = f"screenshot saved ({os.path.basename(path)})"
                with open(path, "rb") as f:
                    self._last_shot_b64 = base64.b64encode(f.read()).decode()
            elif name == "console":
                items = self.console[-10:]
                out = json.dumps(items, ensure_ascii=False) or "no console/page errors"
            else:
                raise ValueError(f"unknown action {name!r}")
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001 — action failure is observation
            out = f"FAILED: {type(e).__name__}: {str(e)[:300]}"
        self._pace()
        return out

    def observe(self) -> str:
        p = self.page
        parts = [f"URL: {p.url}", f"Title: {p.title()}"]
        if self.dialogs:
            parts.append("DIALOGS (auto-dismissed): " +
                         " | ".join(self.dialogs[-3:]))
            self.dialogs.clear()
        try:
            parts.append(ax_snapshot(p, max_nodes=400)[:25000])
        except Exception as e:  # noqa: BLE001
            parts.append(f"(ax snapshot failed: {e})")
        return "\n".join(parts)


def perform(*, task: str, target: str | None, max_actions: int = 30,
            headless: bool = True, executable_path: str | None = None,
            cdp_url: str | None = None, allow_publish: bool = False,
            runs_root: str | None = None) -> dict:
    """Run one natural-language task. Returns the result dict (also saved)."""
    from playwright.sync_api import sync_playwright

    cfg = _model_config()
    if not cfg["api_key"]:
        raise RuntimeError(
            "perform needs a model: set QALOOP_MODEL_API_KEY "
            "(any OpenAI-compatible /chat/completions endpoint via "
            "QALOOP_MODEL_BASE_URL / QALOOP_MODEL_NAME)")

    runs_root = runs_root or os.environ.get(
        "QALOOP_RUNS", os.path.join(os.getcwd(), "runs"))
    os.makedirs(runs_root, exist_ok=True)
    run_dir = new_run_dir(runs_root, "perform")
    os.makedirs(os.path.join(run_dir, "steps"), exist_ok=True)

    messages = [
        {"role": "system",
         "content": SYSTEM.replace("{max_actions}", str(max_actions))},
        {"role": "user",
         "content": f"TASK: {task}\nSTARTING URL: {target or '(browser already open)'}\n"
                    f"Begin. Reply with exactly one JSON action."},
    ]
    vision_ok = True
    result = {"task": task, "target": target, "status": "unknown",
              "actions": [], "run_dir": run_dir}

    with sync_playwright() as pw:
        launch_kw: dict = {"headless": headless}
        if executable_path:
            launch_kw["executable_path"] = executable_path
        if cdp_url:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else \
                browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            attached = True
        else:
            browser = pw.chromium.launch(**launch_kw)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            attached = False
        try:
            agent = Performer(page, run_dir, max_actions,
                              allow_publish=allow_publish)
            if target:
                page.goto(target, timeout=25000, wait_until="domcontentloaded")
            status, summary = "incomplete", ""
            shot_b64 = None
            for _ in range(max_actions):
                obs = agent.observe()
                user_msg: dict = {"role": "user", "content": obs}
                if shot_b64 and vision_ok:
                    user_msg = {"role": "user", "content": [
                        {"type": "text", "text": obs},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64," + shot_b64}}]}
                messages.append(user_msg)
                shot_b64 = None
                try:
                    reply, usage = _chat(cfg, messages)
                except Exception as e:
                    # Some endpoints reject image content: retry text-only once.
                    if "image" in str(e).lower() or "400" in str(e):
                        vision_ok = False
                        messages[-1] = {"role": "user", "content": obs}
                        reply, usage = _chat(cfg, messages)
                    else:
                        raise
                agent.tokens_in += usage.get("prompt_tokens", 0)
                agent.tokens_out += usage.get("completion_tokens", 0)
                try:
                    action = _extract_json(reply)
                except ValueError as e:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content":
                                     f"That was not a valid JSON action ({e}). "
                                     f"Reply with exactly one JSON action."})
                    continue
                name = action.get("action")
                record = {"n": agent.n + 1, "action": action}
                if name == "done":
                    summary = action.get("summary", "")
                    status = "completed"
                    record["result"] = "done"
                    agent.transcript.append(record)
                    break
                if name == "blocked":
                    summary = action.get("reason", "")
                    status = "blocked"
                    record["result"] = "blocked"
                    agent.transcript.append(record)
                    break
                try:
                    out = agent.act(action)
                except PermissionError as e:
                    out = f"BLOCKED BY HARNESS: {e}"
                if name == "screenshot":
                    shot_b64 = getattr(agent, "_last_shot_b64", None)
                record["result"] = out
                agent.transcript.append(record)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user",
                                 "content": f"ACTION RESULT: {out}"})
            else:
                status, summary = "incomplete", \
                    f"used all {max_actions} actions without finishing"
            result.update({
                "status": status, "summary": summary,
                "actions": agent.transcript,
                "action_count": len(agent.transcript),
                "tokens_in": agent.tokens_in, "tokens_out": agent.tokens_out,
                "cost_usd_est": round(
                    agent.tokens_in / 1e6 * cfg["price_in"] +
                    agent.tokens_out / 1e6 * cfg["price_out"], 4),
                "final_url": page.url, "final_title": page.title(),
                "attached": attached,
            })
        finally:
            if not cdp_url:
                browser.close()

    save_json(os.path.join(run_dir, "perform.json"), result)
    lines = [f"# perform: {task}", "",
             f"status: **{result['status']}** — {result['summary']}", "",
             f"actions: {result['action_count']} · tokens in/out: "
             f"{result['tokens_in']}/{result['tokens_out']} · "
             f"est cost ${result['cost_usd_est']:.4f}", "",
             f"final: {result['final_title']} <{result['final_url']}>", "",
             "## transcript", ""]
    for r in agent.transcript:
        a = r["action"]
        lines.append(f"{r['n']}. `{a.get('action')}` "
                     f"{json.dumps({k: v for k, v in a.items() if k != 'action'}, ensure_ascii=False)[:160]}")
        lines.append(f"   → {str(r['result'])[:220]}")
    with open(os.path.join(run_dir, "PERFORM.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return result
