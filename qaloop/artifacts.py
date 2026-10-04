"""Run artifacts: directories, screenshots, a11y snapshots, console/network collectors."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field


def new_run_dir(runs_root: str, flow_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", flow_name.lower()).strip("-") or "flow"
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand = os.urandom(3).hex()
    path = os.path.join(runs_root, f"{stamp}-{slug}-{rand}")
    os.makedirs(os.path.join(path, "steps"), exist_ok=True)
    return path


def prune_old_runs(runs_root: str, keep: int,
                   current_run_dir: str | None = None) -> list[str]:
    """Prune old run dirs so at most `keep` newest remain. Returns deleted dir names.

    keep <= 0 disables pruning (keep everything). `current_run_dir` is never
    pruned (defensive: the run that just wrote its report must survive even
    if its name sorts out of the newest window). Only real directories are
    pruned; deletions are best-effort — one undeletable dir does not fail
    the run.
    """
    if keep <= 0 or not os.path.isdir(runs_root):
        return []
    current_base = (os.path.basename(os.path.abspath(current_run_dir))
                    if current_run_dir else None)
    dirs = sorted(d for d in os.listdir(runs_root)
                  if os.path.isdir(os.path.join(runs_root, d)))
    # Run ids are YYYYMMDDTHHMMSSZ-prefixed, so name order is chronological;
    # the keepers are the N newest. The current run is additionally protected.
    keepers = set(dirs[-keep:])
    deleted: list[str] = []
    for name in dirs:
        if name == current_base or name in keepers:
            continue
        try:
            shutil.rmtree(os.path.join(runs_root, name))
        except Exception:  # noqa: BLE001 — best effort, never fail the run
            continue
        deleted.append(name)
    return deleted


class Collectors:
    """Console / page-error / network-failure collectors attached to a page."""

    def __init__(self) -> None:
        self.console_errors: list[dict] = []
        self.page_errors: list[dict] = []
        self.failed_requests: list[dict] = []
        self.bad_responses: list[dict] = []
        self.network_log: list[dict] = []
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
        # A failed request produces no response event, so it gets its own
        # network_log line: no status, no timing, failure recorded.
        self.network_log.append({
            "ts": time.time(), "method": request.method,
            "url": request.url[:500], "status": None,
            "failure": str(failure)[:300] if failure else None, "ms": None,
        })

    def _on_response(self, response) -> None:
        try:
            status = response.status
        except Exception:
            return
        try:
            timing = response.request.timing
            start, hdr = timing["requestStart"], timing["responseStart"]
            # The "response" event fires when headers arrive, before the body
            # streams, so responseEnd is -1 here; ms is time-to-first-byte
            # (responseStart - requestStart). Route-fulfilled mocks report
            # -1 for everything -> ms None. Playwright uses -1 for timing
            # phases that never happened.
            ms = round(hdr - start, 1) if start >= 0 and hdr >= 0 else None
        except Exception:
            ms = None
        self.network_log.append({
            "ts": time.time(), "method": response.request.method,
            "url": response.url[:500], "status": status, "ms": ms,
        })
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


def screenshot_rms_diff(a_path: str, b_path: str) -> float:
    """Normalized RMS pixel difference between two images, 0.0 (identical)
    to ~1.0. Used by the screenshot_matches assertion."""
    from PIL import Image, ImageChops
    import math
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    h = diff.histogram()
    sq = sum(count * ((i % 256) ** 2) for i, count in enumerate(h))
    n = a.size[0] * a.size[1]
    return math.sqrt(sq / n) / 255.0 if n else 0.0


def write_diff_image(a_path: str, b_path: str, out_path: str) -> str:
    """Write a readable diff-highlight PNG: a copy of `a` with every pixel
    that differs from `b` repainted red.

    Uses the same normalized comparison pipeline as screenshot_rms_diff
    (RGB convert, ImageChops.difference). Deliberate contract difference:
    screenshot_rms_diff silently resizes `b` on size mismatch, but a readable
    highlight cannot be built from unequal frames, so this raises a clean
    ValueError instead. Output has the same dimensions as the inputs.
    Returns out_path.
    """
    from PIL import Image, ImageChops
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    if a.size != b.size:
        raise ValueError(
            f"cannot highlight a diff between unequal frames: {a.size} != {b.size}")
    diff = ImageChops.difference(a, b)
    mask = diff.convert("L").point(lambda v: 255 if v else 0)
    red = Image.new("RGB", a.size, (255, 0, 0))
    out = Image.composite(red, a, mask)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out.save(out_path)
    return out_path


@dataclass
class StepArtifacts:
    screenshot: str | None = None
    ax_snapshot: str | None = None
