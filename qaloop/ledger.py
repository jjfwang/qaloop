"""Cost ledger: append-only JSONL of scripted runs + investigations."""
from __future__ import annotations

import datetime as _dt
import json
import os

QALOOP_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ledger_path() -> str:
    return os.environ.get("QALOOP_LEDGER",
                          os.path.join(QALOOP_HOME, "runs", "ledger.jsonl"))


def append(entry: dict) -> None:
    entry = dict(entry)
    entry.setdefault("ts", _dt.datetime.now(_dt.timezone.utc).isoformat())
    path = ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_all() -> list[dict]:
    path = ledger_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                raise ValueError(
                    f"ledger line {n} is not valid JSON") from None
            if not isinstance(entry, dict):
                raise ValueError(
                    f"ledger line {n} is not a JSON object")
            if "cost_usd_est" in entry:
                value = entry["cost_usd_est"]
                if value is not None:
                    try:
                        float(value)
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"ledger line {n} cost_usd_est is not numeric"
                        ) from None
            out.append(entry)
    return out


def summarize(entries: list[dict] | None = None) -> dict:
    entries = read_all() if entries is None else entries
    total_cost = sum(float(e.get("cost_usd_est") or 0) for e in entries)
    scripted = [e for e in entries if e.get("kind") == "scripted"]
    inv = [e for e in entries if e.get("kind") == "investigation"]
    return {
        "runs": len(entries),
        "scripted_runs": len(scripted),
        "investigations": len(inv),
        "total_cost_usd_est": round(total_cost, 4),
        "investigation_cost_usd_est": round(
            sum(float(e.get("cost_usd_est") or 0) for e in inv), 4),
    }
