"""Job queue: sqlite-backed, single-worker-claim semantics."""
from __future__ import annotations

import json
import os
import sqlite3
import time

QALOOP_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  claimed_at REAL,
  finished_at REAL,
  run_dir TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
"""


def db_path() -> str:
    return os.environ.get("QALOOP_DB", os.path.join(QALOOP_HOME, "qaloop.db"))


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def enqueue(kind: str, payload: dict) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (created_at, kind, payload, status) VALUES (?, ?, ?, 'pending')",
            (time.time(), kind, json.dumps(payload, ensure_ascii=False)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def claim() -> dict | None:
    """Atomically claim the oldest pending job. Returns None when empty."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE jobs SET status = 'claimed', claimed_at = ? WHERE id = ?",
                     (time.time(), row["id"]))
        conn.execute("COMMIT")
        return dict(row)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def complete(job_id: int, status: str, run_dir: str = "", note: str = "") -> None:
    assert status in ("done", "failed")
    conn = connect()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, run_dir = ?, note = ? WHERE id = ?",
            (status, time.time(), run_dir, note[:2000], job_id))
        conn.commit()
    finally:
        conn.close()


def requeue_stale(stale_s: float = 3600) -> int:
    """Return claimed-but-unfinished jobs older than stale_s to pending."""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE jobs SET status = 'pending', claimed_at = NULL "
            "WHERE status = 'claimed' AND claimed_at < ?",
            (time.time() - stale_s,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
            out.append(d)
        return out
    finally:
        conn.close()
