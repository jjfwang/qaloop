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
    _ensure_retry_columns(conn)
    return conn


def _ensure_retry_columns(conn: sqlite3.Connection) -> None:
    """In-place migration: add max_retries/attempts to DBs created before #16."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "max_retries" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 0")
    if "attempts" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def enqueue(kind: str, payload: dict, max_retries: int = 0) -> int:
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError(f"max_retries must be an int >= 0, got {max_retries!r}")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (created_at, kind, payload, status, max_retries)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (time.time(), kind, json.dumps(payload, ensure_ascii=False), max_retries))
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


def requeue_failed(job_id: int, note: str = "") -> None:
    """Return a failed job to pending for a retry: increments attempts (claims
    consumed), clears the claim, appends the note. Requeued jobs are claimed
    ahead of later-enqueued pending jobs (claim takes the oldest id first) —
    the queue absorbs transients rather than parking them behind the line."""
    conn = connect()
    try:
        row = conn.execute("SELECT attempts, note FROM jobs WHERE id = ?", (job_id,)).fetchone()
        prev = ((row["note"] or "").strip() if row else "")
        combined = (prev + " | " + note) if prev else note
        conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, status = 'pending',"
            " claimed_at = NULL, note = ? WHERE id = ?",
            (combined[:2000], job_id))
        conn.commit()
    finally:
        conn.close()


def record_failure(job_id: int, note: str = "") -> str:
    """Apply the job-level retry policy for one failed attempt. Increments
    attempts, then requeues while retries remain (retries_used < max_retries,
    where retries_used = attempts - 1), else marks the job failed.
    Returns 'requeued' or 'failed'."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT attempts, max_retries, note FROM jobs WHERE id = ?", (job_id,)).fetchone()
        attempts = (row["attempts"] or 0) + 1
        max_retries = row["max_retries"] or 0
        prev = (row["note"] or "").strip()
        if (attempts - 1) < max_retries:
            # Requeue ahead of the line: claim takes the oldest pending id,
            # so this job retries before later-enqueued jobs. The queue
            # absorbs transients rather than parking them behind the line.
            retry_note = f"{note} (retry {attempts} of {max_retries})"
            combined = ((prev + " | " + retry_note) if prev else retry_note)[:2000]
            conn.execute(
                "UPDATE jobs SET attempts = ?, status = 'pending',"
                " claimed_at = NULL, note = ? WHERE id = ?",
                (attempts, combined, job_id))
            conn.commit()
            return "requeued"
        conn.execute(
            "UPDATE jobs SET attempts = ?, status = 'failed', finished_at = ?,"
            " note = ? WHERE id = ?",
            (attempts, time.time(), note[:2000], job_id))
        conn.commit()
        return "failed"
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
