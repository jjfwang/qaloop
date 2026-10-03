"""Run artifacts: directories, screenshots, a11y snapshots, console/network collectors."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from dataclasses import dataclass, field


def new_run_dir(runs_root: str, flow_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", flow_name.lower()).strip("-") or "flow"
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand = os.urandom(3).hex()
    path = os.path.join(runs_root, f"{stamp}-{slug}-{rand}")
    os.makedirs(os.path.join(path, "steps"), exist_ok=True)
    return path


class Collectors:
    """Console / page-error / network-failure collectors attached to a page."""

    def __init__(self) -> None:
        self.console_errors: list[dict] = []
        self.page_errors: list[dict] = []
        self.failed_requests: list[dict] = []
        self.bad_responses: list[dict] = []
        self._mark = 0  # index into console_errors+page_errors for console_clean

    def attach(self, page) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("requestfailed", self._on_requestfailed)
        page.on("response", self._on_response)

    def checkpoint(self) -> None:
        self._mark = len(self.console_errors) + len(self.page_errors)

    def errors_since_checkpoint(self) -> list[dict]:
        return (self.console_errors + self.page_errors)[self._mark:]

    def _on_console(self, msg) -> None:
        if msg.type in ("error",):
            try:
                loc = msg.location
            except Exception:
                loc = {}
            self.console_errors.append({
                "ts": time.time(), "type": msg.type, "text": msg.text[:2000],
                "location": {k: loc.get(k) for k in ("url", "lineNumber", "columnNumber")}
                if isinstance(loc, dict) else str(loc)[:200],
            })

    def _on_pageerror(self, err) -> None:
        self.page_errors.append({"ts": time.time(), "text": str(err)[:2000]})

    def _on_requestfailed(self, request) -> None:
        try:
            failure = request.failure
        except Exception:
            failure = None
        self.failed_requests.append({
            "ts": time.time(), "method": request.method, "url": request.url[:500],
            "failure": str(failure)[:300] if failure else None,
        })

    def _on_response(self, response) -> None:
        try:
            status = response.status
        except Exception:
            return
        if status >= 400:
            self.bad_responses.append({
                "ts": time.time(), "method": response.request.method,
                "url": response.url[:500], "status": status,
            })


def _node_text(node: dict) -> str:
    name = (node.get("name") or {}).get("value", "")
    return str(name).strip().replace("\n", " ")


def compact_ax_tree(ax_tree: dict, max_nodes: int = 600, max_chars: int = 60000) -> str:
    """Render a CDP Accessibility.getFullAXTree result as indented text.

    Text-first observation: ~10x cheaper than screenshots for LLM input.
    """
    nodes = {n["nodeId"]: n for n in ax_tree.get("nodes", [])}
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for n in ax_tree.get("nodes", []):
        pid = n.get("parentId")
        if pid and pid in nodes:
            children.setdefault(pid, []).append(n["nodeId"])
        else:
            roots.append(n["nodeId"])

    lines: list[str] = []
    skip_roles = {"none", "presentation", "generic"}

    def render(nid: str, depth: int) -> None:
        if len(lines) >= max_nodes:
            return
        n = nodes[nid]
        if n.get("ignored"):
            for c in children.get(nid, []):
                render(c, depth)
            return
        role = (n.get("role") or {}).get("value", "?")
        text = _node_text(n)
        if role in skip_roles and not text:
            for c in children.get(nid, []):
                render(c, depth)
            return
        val = (n.get("value") or {}).get("value", "")
        val = str(val).strip().replace("\n", " ")[:60]
        desc = (n.get("description") or {}).get("value", "")
        desc = str(desc).strip().replace("\n", " ")[:60]
        line = "  " * min(depth, 12) + role
        if text:
            line += f' "{text[:80]}"'
        if val:
            line += f" = {val}"
        if desc:
            line += f" ({desc})"
        lines.append(line)
        for c in children.get(nid, []):
            render(c, depth + 1)

    for r in roots:
        render(r, 0)
    out = "\n".join(lines)
    return out[:max_chars]


def ax_snapshot(page, max_nodes: int = 600) -> str:
    """Capture the accessibility tree of the page as compact text via CDP."""
    session = page.context.new_cdp_session(page)
    try:
        tree = session.send("Accessibility.getFullAXTree", {})
        return compact_ax_tree(tree, max_nodes=max_nodes)
    finally:
        try:
            session.detach()
        except Exception:
            pass


def save_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


@dataclass
class StepArtifacts:
    screenshot: str | None = None
    ax_snapshot: str | None = None
