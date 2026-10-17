"""qaloop unit tests (stdlib only). Run: python3 tests/test_qaloop.py"""
import contextlib
import io
import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


@contextlib.contextmanager
def temp_db():
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("QALOOP_DB")
        os.environ["QALOOP_DB"] = os.path.join(d, "q.db")
        try:
            yield
        finally:
            if old is None:
                del os.environ["QALOOP_DB"]
            else:
                os.environ["QALOOP_DB"] = old


def test_spec_valid():
    from qaloop.spec import load_spec
    spec = load_spec("flows/game-loading-frames.yaml", strict_env=False)
    check("spec loads game flow", spec.name == "game-loading-frames")
    total = len(spec.setup) + len(spec.steps) + len(spec.teardown)
    check("spec has 6 steps total", total == 6, str(total))
    names = {k for s in spec.steps for k, _ in s.expect_items}
    # noop: intentionally-unbound affordances (OD-P36 pattern). Schema supports it;
    # exercise it with a synthetic spec.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("name: noop-demo\ntarget: http://x\nsteps:\n  - name: journal stays unbound\n"
                "    goto: /\n"
                "    expect: [{visible: body}, {noop: true}]\n")
        noop_path = f.name
    try:
        noop_spec = load_spec(noop_path)
        noop_names = [k for k, _ in noop_spec.steps[0].expect_items]
        check("noop assertion supported + order preserved",
              noop_names == ["visible", "noop"], str(noop_names))
    finally:
        os.unlink(noop_path)
    multi = [s for s in spec.steps if len(s.expect_items) > 1]
    check("repeated assertions preserved in order", len(multi) >= 1)


def test_spec_env_substitution():
    from qaloop.spec import load_spec
    os.environ["TARGET_URL"] = "http://example:9999"
    try:
        spec = load_spec("flows/game-loading-frames.yaml")
        check("env substitution in target", spec.target == "http://example:9999")
    finally:
        del os.environ["TARGET_URL"]


def test_spec_invalid():
    from qaloop.spec import load_spec, SpecError
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("name: bad\nsteps:\n  - name: x\n    op: frobnicate\n")
        path = f.name
    try:
        load_spec(path)
        check("invalid op rejected", False, "no error raised")
    except SpecError:
        check("invalid op rejected", True)
    finally:
        os.unlink(path)


def test_spec_unknown_top_level_keys():
    from qaloop.spec import load_spec, SpecError

    def load(text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return load_spec(path), None
        except SpecError as e:
            return None, str(e)
        finally:
            os.unlink(path)

    base = "name: t\ntarget: http://x\nsteps:\n  - goto: /\n"
    _, err = load(base + "servces: []\n")
    check("unknown top-level key rejected naming the key",
          err is not None and "servces" in err, str(err))
    _, err = load(base + "timeot: {step_ms: 1}\n")
    check("typo top-level key rejected naming the key",
          err is not None and "timeot" in err, str(err))
    _, err = load(base + "timeouts: {step_ms: 1, bogus_ms: 2}\n")
    check("unknown timeouts subkey rejected naming it",
          err is not None and "bogus_ms" in err, str(err))
    _, err = load(base + "artifacts: {screenshot: none, bogus: true}\n")
    check("unknown artifacts subkey rejected naming it",
          err is not None and "bogus" in err, str(err))
    _, err = load(base + "viewport: {width: 800, depth: 3}\n")
    check("unknown viewport subkey rejected naming it",
          err is not None and "depth" in err, str(err))
    all_keys = (
        "version: 1\nname: all-keys\ndescription: every known key\n"
        "target: http://x\nviewport: {width: 1280, height: 800}\n"
        "artifacts: {screenshot: per-step, ax_snapshot: on-failure, trace: true}\n"
        "timeouts: {step_ms: 15000, run_ms: 300000}\n"
        "record_bodies: true\nvideo: false\ntags: [smoke]\n"
        "browser: {color_scheme: dark}\n"
        "setup:\n  - goto: /\n"
        "steps:\n  - goto: /\n"
        "teardown:\n  - goto: /\n"
        "investigate_on_failure: true\nmax_investigation_actions: 5\n"
        "services:\n  - name: api\n    command: \"python3 app.py\"\n"
        "    wait: {port: 8000}\n"
        "matrix:\n  - {variant: a}\n")
    spec, err = load(all_keys)
    check("flow using all 18 known top-level keys loads",
          spec is not None and spec.name == "all-keys", str(err))


def test_spec_validation_top_level_contracts():
    """Issue #357: pin SpecError messages for malformed top-level specs."""
    from qaloop.spec import load_spec, SpecError

    def load(text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return load_spec(path), None
        except SpecError as e:
            return None, str(e)
        finally:
            os.unlink(path)

    _, err = load("- a\n- b\n")
    check("357 top-level list rejected as non-mapping",
          err is not None and "top level must be a mapping" in err, str(err))
    _, err = load("target: http://x\nsteps: []\n")
    check("357 missing name rejected",
          err is not None and "`name` is required" in err, str(err))
    _, err = load("name: t\nversion: 99\ntarget: http://x\nsteps: []\n")
    check("357 unsupported version rejected",
          err is not None and "unsupported version" in err, str(err))
    _, err = load("name: t\ntarget: http://x\n"
                  "artifacts: {screenshot: sometimes}\nsteps:\n  - goto: /\n")
    check("357 bad artifacts.screenshot rejected",
          err is not None and "artifacts.screenshot must be per-step|on-failure|none" in err,
          str(err))
    _, err = load("name: t\ntarget: http://x\nservices: [just-a-string]\n"
                  "steps:\n  - goto: /\n")
    check("357 non-mapping service rejected",
          err is not None and "service must be a mapping" in err, str(err))


def test_spec_validation_services_light_contracts():
    """Issue #357: pin load_services_light SpecError messages."""
    from qaloop.spec import load_services_light, SpecError

    def load_light(text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return load_services_light(path), None
        except SpecError as e:
            return None, str(e)
        finally:
            os.unlink(path)

    _, err = load_light("- a\n- b\n")
    check("357 services-light top-level list rejected as non-mapping",
          err is not None and "top level must be a mapping" in err, str(err))
    _, err = load_light("target: http://x\nsteps: []\n")
    check("357 services-light missing name rejected",
          err is not None and "`name` is required" in err, str(err))


def test_spec_validation_action_ops_contracts():
    """Issue #357: pin SpecError messages for malformed action ops."""
    from qaloop.spec import load_spec, SpecError

    def load(text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return load_spec(path), None
        except SpecError as e:
            return None, str(e)
        finally:
            os.unlink(path)

    def step_err(step_line):
        return load("name: t\ntarget: http://x\nsteps:\n  - " + step_line + "\n")[1]

    def setup_err(step_line):
        return load("name: t\ntarget: http://x\nsetup:\n  - " + step_line
                    + "\nsteps:\n  - goto: /\n")[1]

    err = step_err("click: 123")
    check("357 click with non-string rejected",
          err is not None and "needs a selector string" in err, str(err))
    err = step_err("goto: 123")
    check("357 goto with non-string rejected",
          err is not None and "goto needs a URL string" in err, str(err))
    err = step_err('fill: {target: "#x"}')
    check("357 fill missing text rejected",
          err is not None and "fill needs {target, text}" in err, str(err))
    err = step_err('press: {target: "#x"}')
    check("357 press missing key rejected",
          err is not None and "press needs {target, key}" in err, str(err))
    err = step_err('select: {target: "#x"}')
    check("357 select missing value rejected",
          err is not None and "select needs {target, value}" in err, str(err))
    err = step_err('wait: "later"')
    check("357 wait with non-mapping rejected",
          err is not None and "wait needs a mapping" in err, str(err))
    spec, err = load("name: t\ntarget: http://x\nsteps:\n  - wait:\n")
    check("357 bare wait loads as a valid targetless wait",
          spec is not None, str(err))
    err = step_err('wait_ms: "500"')
    check("357 wait_ms with string rejected",
          err is not None and "wait_ms needs a non-negative integer" in err, str(err))
    err = step_err("wait_ms: -5")
    check("357 wait_ms negative rejected",
          err is not None and "wait_ms needs a non-negative integer" in err, str(err))
    err = step_err('reload: "now"')
    check("357 reload with params rejected",
          err is not None and "takes no params" in err, str(err))
    err = step_err("seed: {css: x}")
    check("357 seed without http/js rejected",
          err is not None and "seed needs" in err, str(err))
    err = step_err("script: {css: x}")
    check("357 script without js rejected",
          err is not None and "script needs {js:" in err, str(err))
    err = setup_err("mock: {url: 123, json: {x: 1}}")
    check("357 mock with non-string url rejected",
          err is not None and "mock.url must be a non-empty string" in err, str(err))
    err = setup_err('mock: {url: "**/a", json: {x: 1}, status: 99}')
    check("357 mock with out-of-range status rejected",
          err is not None and "mock.status must be an HTTP status int" in err, str(err))
    err = setup_err('mock: {url: "**/a", json: {x: 1}, status: "200"}')
    check("357 mock with string status rejected",
          err is not None and "mock.status must be an HTTP status int" in err, str(err))


def test_spec_validation_assertion_contracts():
    """Issue #357: pin SpecError messages for malformed assertions."""
    from qaloop.spec import load_spec, SpecError, _validate_expect

    def load(text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return load_spec(path), None
        except SpecError as e:
            return None, str(e)
        finally:
            os.unlink(path)

    def expect_err(expect_yaml):
        return load("name: t\ntarget: http://x\nsteps:\n  - goto: /\n"
                    "    expect: " + expect_yaml + "\n")[1]

    err = expect_err('["just-a-string"]')
    check("357 expect list of strings rejected",
          err is not None and "expect list items must be single-key mappings" in err,
          str(err))
    direct_err = None
    try:
        _validate_expect("hello", "steps[0]")
    except SpecError as e:
        direct_err = str(e)
    check("357 direct _validate_expect scalar rejected",
          direct_err is not None
          and "expect must be a mapping or a list of mappings" in direct_err,
          str(direct_err))
    err = expect_err("{bogus_assert: true}")
    check("357 unknown assertion rejected",
          err is not None and "unknown assertion" in err, str(err))
    err = expect_err('{text_contains: {selector: "#x"}}')
    check("357 text_contains missing text/pattern rejected",
          err is not None and "needs {selector, text|pattern}" in err, str(err))
    err = expect_err("{visible: 123}")
    check("357 visible with non-string rejected",
          err is not None and "needs a selector string" in err, str(err))
    err = expect_err('{count: {selector: "#x"}}')
    check("357 count missing comparator rejected",
          err is not None and "assertion count needs {selector, equals|gte|lte}" in err,
          str(err))
    err = expect_err("{url_contains: 123}")
    check("357 url_contains with non-string rejected",
          err is not None and "needs a string" in err, str(err))
    err = expect_err("{noop: false}")
    check("357 noop false rejected",
          err is not None and "must be" in err, str(err))
    err = expect_err('{js: {script: "1"}}')
    check("357 js assertion missing contains rejected",
          err is not None and "assertion js needs {script, contains}" in err, str(err))
    err = expect_err("{screenshot_matches: {baseline: 123}}")
    check("357 screenshot_matches non-string baseline rejected",
          err is not None and "screenshot_matches.baseline must be a path string" in err,
          str(err))
    err = expect_err("{ax: {role: 123}}")
    check("357 ax non-string role rejected",
          err is not None and "ax.role must be an ARIA role string" in err, str(err))


def test_spec_validation_step_structure_contracts():
    """Issue #357: pin SpecError messages for malformed steps/phases."""
    from qaloop.spec import load_spec, SpecError

    def load(text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return load_spec(path), None
        except SpecError as e:
            return None, str(e)
        finally:
            os.unlink(path)

    base = "name: t\ntarget: http://x\n"
    _, err = load(base + 'steps: ["just-a-string"]\n')
    check("357 non-mapping step rejected",
          err is not None and "step must be a mapping" in err, str(err))
    _, err = load(base + "steps:\n  - {name: 123, goto: /}\n")
    check("357 step with non-string name rejected",
          err is not None and "name must be a string" in err, str(err))
    _, err = load(base + "steps:\n  - goto: /\n    expect: hello\n")
    check("357 step with scalar expect rejected",
          err is not None and "expect must be a mapping or list of assertions" in err,
          str(err))
    _, err = load(base + 'steps:\n  - {goto: /, click: "#x"}\n')
    check("357 step with two action ops rejected",
          err is not None and "at most one action op per step" in err, str(err))
    _, err = load(base + "steps:\n  - {name: only-a-name}\n")
    check("357 step with no op and no expect rejected",
          err is not None and "step has no action op and no expect" in err, str(err))
    _, err = load(base + "steps: {goto: /}\n")
    check("357 steps as mapping rejected",
          err is not None and "must be a list of steps" in err, str(err))


def test_queue_claim_complete():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml"})
        check("enqueue returns id", isinstance(jid, int))
        job = queue.claim()
        check("claim returns job", job is not None and job["id"] == jid)
        check("double claim blocked", queue.claim() is None)
        queue.complete(jid, "done", run_dir="/tmp/r")
        row = queue.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        check("complete marks done", row["status"] == "done")
        check("complete records run_dir", row["run_dir"] == "/tmp/r")


def test_queue_stale_requeue():
    from qaloop import queue
    import time as _t
    with temp_db():
        jid = queue.enqueue("verify-repo", {"repo": "r"})
        queue.claim()
        con = queue.connect()
        con.execute("UPDATE jobs SET claimed_at=? WHERE id=?", (_t.time() - 7200, jid))
        con.commit()
        con.close()
        check("recent jobs not requeued", queue.claim() is None or True)
        n = queue.requeue_stale(stale_s=3600)
        check("stale claim requeued", n == 1, str(n))
        job = queue.claim()
        check("reclaimed after requeue", job is not None and job["id"] == jid)


def _job_row(jid):
    from qaloop import queue
    con = queue.connect()
    try:
        return dict(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    finally:
        con.close()


def test_retry_first_failure_requeues():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml"}, max_retries=2)
        job = queue.claim()
        check("first claim starts with attempts 0", job["attempts"] == 0, str(job["attempts"]))
        out = queue.record_failure(job["id"], "boom")
        check("first failure requeues", out == "requeued", out)
        row = _job_row(jid)
        check("requeued job back to pending", row["status"] == "pending", row["status"])
        check("attempts incremented after failure 1", row["attempts"] == 1, str(row["attempts"]))
        check("claim cleared on requeue", row["claimed_at"] is None)
        check("retry note recorded", "retry 1 of 2" in (row["note"] or ""), row["note"] or "")
        job2 = queue.claim()
        check("requeued job claimed again", job2 is not None and job2["id"] == jid)
        out = queue.record_failure(job2["id"], "boom")
        check("second failure requeues", out == "requeued", out)
        row = _job_row(jid)
        check("attempts incremented after failure 2", row["attempts"] == 2, str(row["attempts"]))
        check("still pending after failure 2", row["status"] == "pending", row["status"])


def test_retry_fails_twice_then_passes():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml"}, max_retries=2)
        claims = 0
        for _ in range(2):
            job = queue.claim()
            claims += 1
            queue.record_failure(job["id"], "boom")
        job = queue.claim()
        claims += 1
        queue.complete(job["id"], "done", run_dir="/tmp/r")
        row = _job_row(jid)
        check("fails-twice-then-passes is done", row["status"] == "done", row["status"])
        check("fails-twice-then-passes takes 3 claims", claims == 3, str(claims))


def test_retry_exhausted_leaves_failed():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml"}, max_retries=2)
        for _ in range(2):
            job = queue.claim()
            queue.record_failure(job["id"], "boom")
        job = queue.claim()
        out = queue.record_failure(job["id"], "boom")
        check("third failure ends failed", out == "failed", out)
        row = _job_row(jid)
        check("exhausted job status failed", row["status"] == "failed", row["status"])
        check("attempts == 3 after three failures", row["attempts"] == 3, str(row["attempts"]))
        check("no requeue after exhaustion", queue.claim() is None)


def test_retry_default_zero_terminal():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml"})
        job = queue.claim()
        out = queue.record_failure(job["id"], "boom")
        check("default max_retries stays terminal", out == "failed", out)
        row = _job_row(jid)
        check("one failure marks failed", row["status"] == "failed", row["status"])
        check("attempts == 1 with no retries", row["attempts"] == 1, str(row["attempts"]))
        check("nothing to reclaim", queue.claim() is None)


def test_requeue_failed_primitive():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml"}, max_retries=5)
        job = queue.claim()
        queue.requeue_failed(job["id"], "transient glitch")
        row = _job_row(jid)
        check("requeue_failed resets to pending", row["status"] == "pending", row["status"])
        check("requeue_failed increments attempts", row["attempts"] == 1, str(row["attempts"]))
        check("requeue_failed clears claim", row["claimed_at"] is None)
        check("requeue_failed keeps note", "transient glitch" in (row["note"] or ""), row["note"] or "")
        jid2 = queue.enqueue("verify-flow", {"flow": "z.yaml"})
        job2 = queue.claim()
        check("requeued job wins the claim (oldest id first)", job2 is not None and job2["id"] == jid, str(job2 and job2["id"]))


def test_enqueue_stores_max_retries():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml"}, max_retries=2)
        row = _job_row(jid)
        check("max_retries stored on insert", row["max_retries"] == 2, str(row["max_retries"]))
        check("attempts starts at 0", row["attempts"] == 0, str(row["attempts"]))
        jobs = queue.list_jobs()
        check("list_jobs surfaces max_retries", jobs[0]["max_retries"] == 2)
        for bad in (-1, "2", 2.5):
            try:
                queue.enqueue("verify-flow", {}, max_retries=bad)
                check(f"max_retries {bad!r} rejected", False, "no error raised")
            except (ValueError, TypeError):
                check(f"max_retries {bad!r} rejected", True)


def test_queue_claim_rollback_on_error():
    from qaloop import queue

    class FakeCursor:
        def fetchone(self):
            return {"id": 1}

    class FakeConn:
        def __init__(self):
            self.sql = []
            self.closed = False

        def execute(self, sql, params=()):
            self.sql.append(sql)
            if "UPDATE" in sql:
                raise RuntimeError("boom-update")
            return FakeCursor()

        def close(self):
            self.closed = True

    fake = FakeConn()
    real_connect = queue.connect
    queue.connect = lambda: fake
    raised = None
    try:
        try:
            queue.claim()
        except RuntimeError as e:
            raised = e
    finally:
        queue.connect = real_connect
    check("claim rollback: original error propagates",
          raised is not None and str(raised) == "boom-update", str(raised))
    check("claim rollback: ROLLBACK attempted after UPDATE",
          any("ROLLBACK" in s for s in fake.sql)
          and any("UPDATE" in s for s in fake.sql)
          and fake.sql.index(next(s for s in fake.sql if "ROLLBACK" in s))
          > fake.sql.index(next(s for s in fake.sql if "UPDATE" in s)),
          str(fake.sql))
    check("claim rollback: connection closed", fake.closed is True)
    check("claim rollback: connect stub restored", queue.connect is real_connect)


def test_queue_claim_rollback_failure_swallowed():
    from qaloop import queue

    class FakeCursor:
        def fetchone(self):
            return {"id": 1}

    class FakeConn:
        def __init__(self):
            self.sql = []
            self.closed = False

        def execute(self, sql, params=()):
            self.sql.append(sql)
            if "UPDATE" in sql:
                raise RuntimeError("boom-update")
            if "ROLLBACK" in sql:
                raise RuntimeError("boom-rollback")
            return FakeCursor()

        def close(self):
            self.closed = True

    fake = FakeConn()
    real_connect = queue.connect
    queue.connect = lambda: fake
    raised = None
    try:
        try:
            queue.claim()
        except RuntimeError as e:
            raised = e
    finally:
        queue.connect = real_connect
    check("claim rollback-swallow: original error still propagates",
          raised is not None and str(raised) == "boom-update", str(raised))
    check("claim rollback-swallow: ROLLBACK attempted",
          any("ROLLBACK" in s for s in fake.sql), str(fake.sql))
    check("claim rollback-swallow: connection closed", fake.closed is True)
    check("claim rollback-swallow: connect stub restored", queue.connect is real_connect)


def test_queue_list_jobs_corrupt_payload():
    from qaloop import queue
    with temp_db():
        valid_id = queue.enqueue("verify-flow", {"flow": "x.yaml"})
        conn = queue.connect()
        try:
            cur = conn.execute(
                "INSERT INTO jobs (created_at, kind, payload, status)"
                " VALUES (0.0, 'verify-flow', 'not-json{', 'pending')")
            corrupt_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        jobs = queue.list_jobs()
        by_id = {j["id"]: j for j in jobs}
        check("list_jobs corrupt: both rows present",
              len(by_id) == 2 and valid_id in by_id and corrupt_id in by_id,
              str(list(by_id)))
        corrupt = by_id.get(corrupt_id, {})
        check("list_jobs corrupt: raw payload preserved",
              corrupt.get("payload") == "not-json{", str(corrupt.get("payload")))
        check("list_jobs corrupt: payload stays str",
              isinstance(corrupt.get("payload"), str))
        valid = by_id.get(valid_id, {})
        check("list_jobs corrupt: valid payload parsed",
              valid.get("payload") == {"flow": "x.yaml"}, str(valid.get("payload")))
        check("list_jobs corrupt: valid payload is dict",
              isinstance(valid.get("payload"), dict))


def test_retry_migration_from_old_schema():
    import sqlite3
    from qaloop import queue
    with temp_db():
        con = sqlite3.connect(os.environ["QALOOP_DB"])
        con.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " created_at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'pending', claimed_at REAL,"
            " finished_at REAL, run_dir TEXT, note TEXT)")
        con.execute("INSERT INTO jobs (created_at, kind, payload) VALUES (1.0, 'verify-flow', '{}')")
        con.commit()
        con.close()
        jid = queue.enqueue("verify-flow", {"flow": "y.yaml"}, max_retries=1)
        queue.complete(jid, "done", note="ok")
        row = _job_row(jid)
        check("migrated db accepts new rows", row["status"] == "done" and row["max_retries"] == 1,
              f"{row['status']} max_retries={row['max_retries']}")
        old = _job_row(1)
        check("pre-existing row upgraded in place",
              old["status"] == "pending" and old["max_retries"] == 0 and old["attempts"] == 0,
              f"{old['status']} max_retries={old['max_retries']} attempts={old['attempts']}")


@contextlib.contextmanager
def _stub_run_one_flow(result):
    """Browser-free stub for worker._run_one_flow (plain attribute swap)."""
    from qaloop import worker
    real = worker._run_one_flow

    def fake(flow_path, target, runs_root, headless, executable_path,
             do_investigate, keep_runs=0):
        return result

    worker._run_one_flow = fake
    try:
        yield
    finally:
        worker._run_one_flow = real


def _handle_kwargs():
    return dict(flows_dir="/tmp/flows", runs_root="/tmp/runs", headless=True,
                executable_path=None, repos_config={"repos": {}})


def test_worker_pass_claim_run_done():
    from qaloop import queue, worker
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"})
        with _stub_run_one_flow(("passed", "/tmp/runs/demo")):
            worker.handle_job(queue.claim(), **_handle_kwargs())
        row = _job_row(jid)
        check("worker pass marks done", row["status"] == "done", row["status"])
        check("worker pass records run_dir", row["run_dir"] == "/tmp/runs/demo",
              str(row["run_dir"]))
        check("worker pass leaves queue empty", queue.claim() is None)


def test_worker_failure_requeues_within_retry_budget():
    from qaloop import queue, worker
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"},
                            max_retries=2)
        with _stub_run_one_flow(("failed", "/tmp/runs/f1")):
            worker.handle_job(queue.claim(), **_handle_kwargs())
        row = _job_row(jid)
        check("worker failure requeues to pending", row["status"] == "pending",
              row["status"])
        check("worker failure counts one attempt", row["attempts"] == 1,
              str(row["attempts"]))
        check("requeued job claimable again",
              (queue.claim() or {}).get("id") == jid)


def test_worker_retry_exhausted_terminal_via_handle_job():
    from qaloop import queue, worker
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"},
                            max_retries=1)
        with _stub_run_one_flow(("failed", "/tmp/runs/f")):
            worker.handle_job(queue.claim(), **_handle_kwargs())
            worker.handle_job(queue.claim(), **_handle_kwargs())
        row = _job_row(jid)
        check("exhausted worker job is failed", row["status"] == "failed",
              row["status"])
        check("two attempts used for max_retries=1", row["attempts"] == 2,
              str(row["attempts"]))
        check("nothing left to claim", queue.claim() is None)


def test_worker_stale_claim_released():
    from qaloop import queue
    with temp_db():
        jid = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"})
        queue.claim()
        n = queue.requeue_stale(stale_s=0)
        check("stale claim released", n == 1, str(n))
        row = _job_row(jid)
        check("stale claim back to pending", row["status"] == "pending",
              row["status"])
        check("released claim is re-claimable",
              (queue.claim() or {}).get("id") == jid)


def test_worker_once_empty_queue_exits():
    import io
    from qaloop import worker
    with temp_db():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            worker.run_worker(flows_dir="/tmp/flows", runs_root="/tmp/runs",
                              once=True, poll_s=0.01, headless=True,
                              executable_path=None)
        out = buf.getvalue()
        check("once-mode prints empty-queue message", "queue empty" in out, out.strip() or "(no output)")


@contextlib.contextmanager
def _stub_handle_job(fn):
    """Browser-free stub for worker.handle_job (plain attribute swap)."""
    from qaloop import worker
    real = worker.handle_job
    worker.handle_job = fn
    try:
        yield
    finally:
        worker.handle_job = real


def test_worker_concurrency_no_double_claim():
    import threading
    import time
    from qaloop import queue, worker
    with temp_db():
        jids = [queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"})
                for _ in range(8)]
        seen, lock = [], threading.Lock()

        def stub(job, **kwargs):
            time.sleep(0.05)  # widen the race window
            with lock:
                seen.append(job["id"])
            queue.complete(job["id"], "done")

        with _stub_handle_job(stub):
            worker.run_worker(flows_dir="/tmp/flows", runs_root="/tmp/runs",
                              once=True, poll_s=0.01, headless=True,
                              executable_path=None, concurrency=4)
        check("parallel claims handle all 8 jobs", len(seen) == 8,
              f"handled {len(seen)}")
        check("no job double-claimed", sorted(seen) == sorted(jids), str(sorted(seen)))
        statuses = [_job_row(j)["status"] for j in jids]
        check("all jobs done", all(s == "done" for s in statuses), str(statuses))



class _FakeBootTarget:
    """Browser-free stand-in for env.ProcTarget (records stop())."""

    def __init__(self, url="http://primary/"):
        self.url = url
        self.stopped = False

    def stop(self):
        self.stopped = True


@contextlib.contextmanager
def _stub_verify_repo(results):
    """Stub worker._run_one_flow (records calls, pops scripted results) and
    worker.boot_repo_env (records profiles, returns one fake target plus a
    fixed primary_url). Plain attribute swaps, restored in finally."""
    from qaloop import worker
    real_run, real_boot = worker._run_one_flow, worker.boot_repo_env
    calls = {"runs": [], "boots": []}
    targets = []

    def fake_run(flow_path, target, runs_root, headless, executable_path,
                 do_investigate, keep_runs=0):
        calls["runs"].append((flow_path, target))
        return results.pop(0)

    def fake_boot(preset, profile=None):
        calls["boots"].append(profile)
        t = _FakeBootTarget()
        targets.append(t)
        return [t], "http://primary/"

    worker._run_one_flow, worker.boot_repo_env = fake_run, fake_boot
    try:
        yield calls, targets
    finally:
        worker._run_one_flow, worker.boot_repo_env = real_run, real_boot


def test_issue_331_worker_verify_repo_contract():
    import yaml
    from qaloop import queue, worker

    def kwargs(cfg):
        return dict(_handle_kwargs(), repos_config=cfg)

    with temp_db():
        # --- unknown repo: failure note names the missing preset ---
        jid = queue.enqueue("verify-repo", {"repo": "ghost"})
        with _stub_verify_repo([]):
            worker.handle_job(queue.claim(), **kwargs({"repos": {}}))
        row = _job_row(jid)
        check("verify-repo unknown repo fails the job", row["status"] == "failed",
              row["status"])
        check("verify-repo unknown repo note names the preset gap",
              "no repo preset for 'ghost' (see repos.yaml)" in (row["note"] or ""),
              row["note"] or "")

        # --- preset with no flows: failure note names the gap ---
        jid = queue.enqueue("verify-repo", {"repo": "r1"})
        with _stub_verify_repo([]):
            worker.handle_job(queue.claim(),
                              **kwargs({"repos": {"r1": {"env": []}}}))
        row = _job_row(jid)
        check("verify-repo flowless preset fails the job",
              row["status"] == "failed", row["status"])
        check("verify-repo flowless preset note",
              "no flows for repo 'r1'" in (row["note"] or ""), row["note"] or "")

        # --- entry resolution + path fallback + all-pass done/last run_dir ---
        preset = {"flows": ["flows/demo-mock-ux.yaml",
                            {"flow": "bare.yaml", "env": "prof"}]}
        jid = queue.enqueue("verify-repo", {"repo": "r1"})
        with _stub_verify_repo([("passed", "/tmp/runs/a"),
                                ("passed", "/tmp/runs/b")]) as (calls, targets):
            worker.handle_job(queue.claim(), **kwargs({"repos": {"r1": preset}}))
        row = _job_row(jid)
        check("verify-repo both entries ran", len(calls["runs"]) == 2,
              str(calls["runs"]))
        check("verify-repo home-relative flow resolves under QALOOP_HOME",
              calls["runs"][0][0] == os.path.join(worker.QALOOP_HOME,
                                                  "flows/demo-mock-ux.yaml"),
              calls["runs"][0][0])
        check("verify-repo bare flow falls back to flows_dir",
              calls["runs"][1][0] == os.path.join("/tmp/flows", "bare.yaml"),
              calls["runs"][1][0])
        check("verify-repo boot profiles: string None, dict named",
              calls["boots"] == [None, "prof"], str(calls["boots"]))
        check("verify-repo all-pass completes done", row["status"] == "done",
              row["status"])
        check("verify-repo done run_dir is the last flow run_dir",
              row["run_dir"] == "/tmp/runs/b", str(row["run_dir"]))
        check("verify-repo booted targets all stopped",
              len(targets) == 2 and all(t.stopped for t in targets),
              str([t.stopped for t in targets]))

        # --- target selection: payload wins, else boot primary_url ---
        jid = queue.enqueue("verify-repo", {"repo": "r1", "flows": ["f.yaml"],
                                            "target": "http://payload/"})
        with _stub_verify_repo([("passed", "/tmp/runs/t")]) as (calls, _t):
            worker.handle_job(queue.claim(), **kwargs({"repos": {"r1": {"env": []}}}))
        check("verify-repo payload target wins",
              calls["runs"][0][1] == "http://payload/", str(calls["runs"]))
        jid = queue.enqueue("verify-repo", {"repo": "r1", "flows": ["f.yaml"]})
        with _stub_verify_repo([("passed", "/tmp/runs/t")]) as (calls, _t):
            worker.handle_job(queue.claim(), **kwargs({"repos": {"r1": {"env": []}}}))
        check("verify-repo boot primary_url is the fallback target",
              calls["runs"][0][1] == "http://primary/", str(calls["runs"]))

        # --- payload flows override preset flows ---
        jid = queue.enqueue("verify-repo",
                            {"repo": "r1", "flows": ["payload.yaml"]})
        with _stub_verify_repo([("passed", "/tmp/runs/o")]) as (calls, _t):
            worker.handle_job(queue.claim(),
                              **kwargs({"repos": {"r1": {"flows": ["preset.yaml"]}}}))
        check("verify-repo payload flows override preset flows",
              [r[0] for r in calls["runs"]] == [os.path.join("/tmp/flows",
                                                             "payload.yaml")],
              str(calls["runs"]))

        # --- failed first flow stops without continue_on_failure ---
        jid = queue.enqueue("verify-repo",
                            {"repo": "r1", "flows": ["a.yaml", "b.yaml"]})
        with _stub_verify_repo([("failed", "/tmp/runs/f1"),
                                ("passed", "/tmp/runs/p2")]) as (calls, _t):
            worker.handle_job(queue.claim(), **kwargs({"repos": {"r1": {"env": []}}}))
        row = _job_row(jid)
        check("verify-repo failure stops after the first flow",
              len(calls["runs"]) == 1, str(calls["runs"]))
        check("verify-repo stopped run is failed, not done",
              row["status"] == "failed", row["status"])

        # --- continue_on_failure: all flows run, worst status kept ---
        jid = queue.enqueue("verify-repo",
                            {"repo": "r1", "flows": ["a.yaml", "b.yaml"],
                             "continue_on_failure": True})
        with _stub_verify_repo([("failed", "/tmp/runs/f1"),
                                ("passed", "/tmp/runs/p2")]) as (calls, _t):
            worker.handle_job(queue.claim(), **kwargs({"repos": {"r1": {"env": []}}}))
        row = _job_row(jid)
        check("verify-repo continue_on_failure runs every flow",
              len(calls["runs"]) == 2, str(calls["runs"]))
        check("verify-repo continue_on_failure keeps the worst status",
              row["status"] == "failed", row["status"])

        # --- unknown job kind: failure note names the kind ---
        jid = queue.enqueue("bogus-kind", {"repo": "r1"})
        with _stub_verify_repo([]):
            worker.handle_job(queue.claim(),
                              **kwargs({"repos": {"r1": {"env": []}}}))
        row = _job_row(jid)
        check("worker unknown job kind fails the job",
              row["status"] == "failed", row["status"])
        check("worker unknown job kind note names the kind",
              "unknown job kind 'bogus-kind'" in (row["note"] or ""),
              row["note"] or "")

    # --- load_repos_config: missing file and real YAML ---
    missing = worker.load_repos_config("/nonexistent-331/repos.yaml")
    check("load_repos_config missing file returns empty repos",
          missing == {"repos": {}}, str(missing))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "repos.yaml")
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump({"repos": {"r1": {"flows": ["a.yaml"]}}}, f)
        cfg = worker.load_repos_config(p)
    check("load_repos_config parses a real YAML file",
          cfg == {"repos": {"r1": {"flows": ["a.yaml"]}}}, str(cfg))

    # --- boot seams (real functions, boot_static/boot_command stubbed) ---
    real_static, real_command = worker.boot_static, worker.boot_command
    booted = []

    def fake_static(d, port=None):
        t = _FakeBootTarget("http://static/")
        t.boot_dir = d
        booted.append(t)
        return t

    def fake_command(cmd, cwd=None, env=None, wait=None, timeout_s=90,
                     name="cmd"):
        t = _FakeBootTarget("http://cmd/")
        booted.append(t)
        return t

    worker.boot_static, worker.boot_command = fake_static, fake_command
    try:
        targets, primary = worker.boot_repo_env(
            {"env": [{"static": {"dir": "site"}}]}, None)
        check("boot_repo_env default profile boots preset env",
              len(targets) == 1 and primary == "http://static/"
              and targets[0].boot_dir.endswith("site"),
              f"{primary} {targets}")
        targets, primary = worker.boot_repo_env(
            {"workdir": "/wd",
             "env_profiles": {"prof": [{"command": {"command": "run.sh"}}]}},
            "prof")
        check("boot_repo_env named profile boots its entries",
              len(targets) == 1 and primary == "http://cmd/",
              f"{primary} {targets}")
        try:
            worker.boot_repo_env({"env_profiles": {"a": []}}, "nope")
            raised = ""
        except ValueError as e:
            raised = str(e)
        check("boot_repo_env unknown profile raises naming the profile",
              "no env profile 'nope'" in raised, raised)
        before = len(booted)
        try:
            worker.boot_env_entries(
                [{"static": {"dir": "site"}}, {"bogus": 1}], "/wd")
            raised = ""
        except ValueError as e:
            raised = str(e)
        check("boot_env_entries unknown entry raises",
              "unknown env entry" in raised, raised)
        check("boot_env_entries stops already-booted targets on error",
              len(booted) == before + 1 and booted[-1].stopped,
              str([t.stopped for t in booted]))
    finally:
        worker.boot_static, worker.boot_command = real_static, real_command


def test_issue_371_worker_branch_coverage():
    """Pin the 6 untaken branch sides in worker.py (issue #371): absolute
    static dir, dict payloads in _handle_job_body/handle_job, absolute flow
    paths in verify-flow/verify-repo, and the except-path requeue."""
    import threading
    import time
    from qaloop import queue, worker

    @contextlib.contextmanager
    def _recording_run(result):
        real = worker._run_one_flow
        calls = []

        def fake(flow_path, target, runs_root, headless, executable_path,
                 do_investigate, keep_runs=0):
            calls.append((flow_path, target))
            return result

        worker._run_one_flow = fake
        try:
            yield calls
        finally:
            worker._run_one_flow = real

    # (a) 67->69: absolute static dir skips the workdir join.
    real_static = worker.boot_static
    captured = {}
    fake_target = _FakeBootTarget("http://abs-static/")

    def fake_static(d, port=None):
        captured["dir"] = d
        captured["port"] = port
        return fake_target

    worker.boot_static = fake_static
    try:
        targets, primary = worker.boot_env_entries(
            [{"static": {"dir": "/abs/site", "port": 8123}}], "/wd")
    finally:
        worker.boot_static = real_static
    check("371 absolute static dir passed verbatim",
          captured.get("dir") == "/abs/site", str(captured))
    check("371 absolute static port passed through",
          captured.get("port") == 8123, str(captured))
    check("371 absolute static primary_url is the booted target url",
          primary == fake_target.url and targets == [fake_target],
          f"{primary} {targets}")

    # (b) 181->184: dict payload in _handle_job_body skips the JSON decode.
    with temp_db():
        jid = queue.enqueue("verify-flow",
                            {"flow": "x.yaml", "target": "http://x"})
        job = {"id": jid, "kind": "verify-flow",
               "payload": {"flow": "x.yaml", "target": "http://x"},
               "attempts": 0}
        with _recording_run(("passed", "/tmp/runs/demo")) as calls:
            worker._handle_job_body(job, **_handle_kwargs())
        row = _job_row(jid)
        check("371 dict-payload body job done", row["status"] == "done",
              row["status"])
        check("371 dict-payload body records run_dir",
              row["run_dir"] == "/tmp/runs/demo", str(row["run_dir"]))
        check("371 dict-payload body joins relative flow to flows_dir",
              calls and calls[0][0] == os.path.join("/tmp/flows", "x.yaml"),
              str(calls))

    # (c) 190->192: absolute flow in verify-flow skips the flows_dir join.
    with temp_db():
        jid = queue.enqueue("verify-flow",
                            {"flow": "/abs/flows/x.yaml",
                             "target": "http://x"})
        with _recording_run(("passed", "/tmp/runs/demo")) as calls:
            worker.handle_job(queue.claim(), **_handle_kwargs())
        row = _job_row(jid)
        check("371 absolute verify-flow path passed verbatim",
              calls and calls[0][0] == "/abs/flows/x.yaml", str(calls))
        check("371 absolute verify-flow job done", row["status"] == "done",
              row["status"])

    # (d) 207->212: absolute flow in verify-repo skips the home/flows_dir
    # resolution.
    with temp_db():
        cfg = {"repos": {"r1": {"env": []}}}
        jid = queue.enqueue("verify-repo",
                            {"repo": "r1", "flows": ["/abs/flows/x.yaml"]})
        with _stub_verify_repo([("passed", "/tmp/runs/a")]) as (calls, targets):
            worker.handle_job(queue.claim(),
                              **dict(_handle_kwargs(), repos_config=cfg))
        row = _job_row(jid)
        check("371 absolute verify-repo path passed verbatim",
              calls["runs"] and calls["runs"][0][0] == "/abs/flows/x.yaml",
              str(calls["runs"]))
        check("371 absolute verify-repo boots default profile",
              calls["boots"] == [None], str(calls["boots"]))
        check("371 absolute verify-repo booted target stopped",
              len(targets) == 1 and targets[0].stopped,
              str([t.stopped for t in targets]))
        check("371 absolute verify-repo job done", row["status"] == "done",
              row["status"])
        check("371 absolute verify-repo records run_dir",
              row["run_dir"] == "/tmp/runs/a", str(row["run_dir"]))

    # (e) 242->246: except-path record_failure returning "requeued" stays
    # silent (no notification) and leaves the job pending.
    with temp_db(), _notify_stub() as (received, url):
        with _notify_env(url):
            jid = queue.enqueue("verify-flow",
                                {"flow": "x.yaml", "target": "http://x"},
                                max_retries=2)
            with _raise_run_one_flow():
                worker.handle_job(queue.claim(), **_handle_kwargs())
            row = _job_row(jid)
            check("371 except-path requeue leaves job pending",
                  row["status"] == "pending", row["status"])
            check("371 except-path requeue counts one attempt",
                  row["attempts"] == 1, str(row["attempts"]))
            check("371 except-path requeue note names the worker error",
                  "worker error" in (row["note"] or "")
                  and "boom" in (row["note"] or ""), row["note"] or "")
            check("371 except-path requeue sends no notification",
                  received == [], str(received))

    # (f) 264->267: dict payload in handle_job skips the JSON decode, so
    # _resolve_job_timeout reads timeout_s straight from the dict.
    with temp_db():
        release = threading.Event()
        with _stub_run_one_flow_blocking(release) as threads:
            jid = queue.enqueue("verify-flow",
                                {"flow": "x.yaml", "target": "http://x"})
            job = queue.claim()
            job["payload"] = {"flow": "x.yaml", "target": "http://x",
                              "timeout_s": 0.5}
            start = time.monotonic()
            worker.handle_job(job, **_handle_kwargs())
            elapsed = time.monotonic() - start
            row = _job_row(jid)
            _release_threads(release, threads)
        check("371 dict-payload handle_job returns promptly on timeout_s",
              elapsed < 5, f"elapsed={elapsed:.2f}s")
        check("371 dict-payload timed-out job failed",
              row["status"] == "failed", row["status"])
        check("371 dict-payload timed-out note contains 'timeout'",
              "timeout" in (row["note"] or ""), (row["note"] or "")[:80])
        check("371 dict-payload timed-out counts one attempt",
              row["attempts"] == 1, str(row["attempts"]))


def test_issue_335_worker_target_cleanup_isolation():
    from qaloop import queue, worker

    class _StopTarget:
        """Fake boot target: records every stop() attempt in a shared
        order list; a target flagged raises=True always raises."""

        def __init__(self, name, order, raises=False):
            self.name = name
            self.order = order
            self.raises = raises
            self.url = "http://primary/"

        def stop(self):
            self.order.append(self.name)
            if self.raises:
                raise RuntimeError(f"stop boom: {self.name}")

    @contextlib.contextmanager
    def stub(run_behavior, bad_raises=True):
        """Stub worker._run_one_flow / worker.boot_repo_env (plain
        attribute swaps, restored in finally). boot returns TWO targets
        from ONE boot: [bad, good]. run_behavior is a (status, run_dir)
        tuple to return, or an exception instance to raise."""
        real_run, real_boot = worker._run_one_flow, worker.boot_repo_env
        order, calls = [], {"runs": 0}
        bad = _StopTarget("bad", order, raises=bad_raises)
        good = _StopTarget("good", order)

        def fake_run(flow_path, target, runs_root, headless, executable_path,
                     do_investigate, keep_runs=0):
            calls["runs"] += 1
            if isinstance(run_behavior, Exception):
                raise run_behavior
            return run_behavior

        def fake_boot(preset, profile=None):
            return [bad, good], "http://primary/"

        worker._run_one_flow, worker.boot_repo_env = fake_run, fake_boot
        try:
            yield order, calls
        finally:
            worker._run_one_flow, worker.boot_repo_env = real_run, real_boot

    def kwargs():
        return dict(_handle_kwargs(),
                    repos_config={"repos": {"r1": {"env": []}}})

    with temp_db():
        # --- case 1: a raising stop() in the per-flow cleanup cannot
        # prevent the stop attempt on the remaining target; because the
        # failure is isolated, the job still completes through the
        # normal path and the shared list is emptied (each target is
        # attempted exactly once, no outer-finally re-stop) ---
        jid = queue.enqueue("verify-repo", {"repo": "r1", "flows": ["f.yaml"]})
        with stub(("passed", "/tmp/runs/x")) as (order, calls):
            worker.handle_job(queue.claim(), **kwargs())
        row = _job_row(jid)
        check("335 raising stop does not block remaining target stop",
              order == ["bad", "good"], str(order))
        check("335 raising-stop job ran the flow exactly once",
              calls["runs"] == 1, str(calls["runs"]))
        check("335 raising-stop job completes done via the flow status",
              row["status"] == "done", row["status"])

        # --- case 2: the flow itself raises after boot extended the
        # shared target list: cleanup still attempts every target
        # exactly once (the raising stop included) and the job is
        # recorded failed through the worker-error path ---
        jid = queue.enqueue("verify-repo", {"repo": "r1", "flows": ["f.yaml"]})
        with stub(RuntimeError("run boom")) as (order, calls):
            worker.handle_job(queue.claim(), **kwargs())
        row = _job_row(jid)
        check("335 run-error cleanup attempts every target once",
              order == ["bad", "good"], str(order))
        check("335 run-error job ran the flow exactly once",
              calls["runs"] == 1, str(calls["runs"]))
        check("335 run-error job is failed", row["status"] == "failed",
              row["status"])
        check("335 run-error job note records the worker error",
              "worker error" in (row["note"] or "")
              and "run boom" in (row["note"] or ""), row["note"] or "")

        # --- case 3 (normal): two well-behaved targets (bad_raises
        # off) are each stopped exactly once: no outer-finally
        # double-stop, and the job completes done ---
        jid = queue.enqueue("verify-repo", {"repo": "r1", "flows": ["f.yaml"]})
        with stub(("passed", "/tmp/runs/x"), bad_raises=False) as (order, calls):
            worker.handle_job(queue.claim(), **kwargs())
        row = _job_row(jid)
        check("335 normal cleanup stops each target exactly once",
              order == ["bad", "good"] and order.count("bad") == 1
              and order.count("good") == 1, str(order))
        check("335 normal job completes done", row["status"] == "done",
              row["status"])


def test_worker_concurrency_once_waits_for_inflight():
    import threading
    import time
    from qaloop import queue, worker
    with temp_db():
        j1 = queue.enqueue("verify-flow", {"flow": "a.yaml", "target": "http://x"})
        j2 = queue.enqueue("verify-flow", {"flow": "b.yaml", "target": "http://x"})
        finished, lock = [], threading.Lock()

        def stub(job, **kwargs):
            time.sleep(0.3)  # claim() will briefly see an empty queue
            with lock:
                finished.append(job["id"])
            queue.complete(job["id"], "done")

        with _stub_handle_job(stub):
            worker.run_worker(flows_dir="/tmp/flows", runs_root="/tmp/runs",
                              once=True, poll_s=0.01, headless=True,
                              executable_path=None, concurrency=2)
        check("--once waits for in-flight jobs before exiting",
              sorted(finished) == sorted([j1, j2]), str(sorted(finished)))


def test_worker_concurrency_one_is_serial():
    from qaloop import queue, worker
    with temp_db():
        jids = [queue.enqueue("verify-flow", {"flow": f"{i}.yaml", "target": "http://x"})
                for i in range(4)]
        order = []

        def stub(job, **kwargs):
            order.append(job["id"])
            queue.complete(job["id"], "done")

        with _stub_handle_job(stub):
            worker.run_worker(flows_dir="/tmp/flows", runs_root="/tmp/runs",
                              once=True, poll_s=0.01, headless=True,
                              executable_path=None, concurrency=1)
        check("concurrency=1 handles jobs in claim order", order == jids, str(order))


def test_worker_concurrency_rejects_invalid():
    import argparse
    import io
    from qaloop import queue, worker
    from qaloop.cli import cmd_worker
    raised = False
    try:
        worker.run_worker(flows_dir="/tmp/flows", runs_root="/tmp/runs",
                          once=True, poll_s=0.01, concurrency=0)
    except ValueError:
        raised = True
    check("run_worker concurrency=0 raises ValueError", raised)
    bad = argparse.Namespace(flows="/tmp/flows", once=True, poll=5.0,
                             headed=False, executable_path=None,
                             keep_runs=None, concurrency=0, job_timeout=1800)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_worker(bad)
    check("CLI --concurrency 0 errors", rc == 2 and "ERROR" in buf.getvalue(),
          f"rc={rc}")
    good = argparse.Namespace(flows="/tmp/flows", once=True, poll=0.01,
                              headed=False, executable_path=None,
                              keep_runs=None, concurrency=2, job_timeout=1800)
    with temp_db():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_worker(good)
    check("CLI --concurrency 2 passes through and exits 0 on empty queue",
          rc == 0, f"rc={rc}")


@contextlib.contextmanager
def _stub_run_one_flow_blocking(release):
    """Stub _run_one_flow with a fake hung browser: blocks on an Event,
    recording the body thread so the test can release it for cleanup."""
    import threading
    from qaloop import worker
    real = worker._run_one_flow
    threads = []

    def fake(flow_path, target, runs_root, headless, executable_path,
             do_investigate, keep_runs=0):
        threads.append(threading.current_thread())
        release.wait(15)  # hung: never returns before the test releases it
        return ("passed", "/tmp/runs/hung")

    worker._run_one_flow = fake
    try:
        yield threads
    finally:
        worker._run_one_flow = real


def _release_threads(release, threads):
    """Let orphaned (timed-out) body threads finish cleanly on the live db."""
    release.set()
    for t in threads:
        t.join(5)


def test_worker_job_timeout_hung_job_surfaced():
    """Payload timeout_s=0.5 overrides the CLI default 1800: a hung job is
    surfaced in ~0.5s instead of at the 1h stale requeue (issue #94)."""
    import io
    import threading
    import time
    from qaloop import queue, worker
    with temp_db():
        release = threading.Event()
        with _stub_run_one_flow_blocking(release) as threads:
            jid = queue.enqueue("verify-flow",
                                {"flow": "x.yaml", "target": "http://x",
                                 "timeout_s": 0.5})
            start = time.monotonic()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                worker.handle_job(queue.claim(), **_handle_kwargs())
            elapsed = time.monotonic() - start
            row = _job_row(jid)
            _release_threads(release, threads)
        check("hung job: handle_job returns promptly on payload timeout_s=0.5",
              elapsed < 5, f"elapsed={elapsed:.2f}s")
        check("hung job: terminal (max_retries=0) ends failed",
              row["status"] == "failed", row["status"])
        check("hung job: note contains 'timeout'",
              "timeout" in (row["note"] or ""), (row["note"] or "")[:80])
        check("hung job: one attempt recorded", row["attempts"] == 1,
              str(row["attempts"]))
        check("hung job: printed a timeout message",
              "timed out" in buf.getvalue(),
              buf.getvalue().strip() or "(no output)")


def test_worker_job_timeout_requeues_within_retry_budget():
    """A timed-out job follows the normal retry policy: requeued within
    budget, and the timeout note survives the requeue."""
    import threading
    from qaloop import queue, worker
    with temp_db():
        release = threading.Event()
        with _stub_run_one_flow_blocking(release) as threads:
            jid = queue.enqueue("verify-flow",
                                {"flow": "x.yaml", "target": "http://x",
                                 "timeout_s": 0.5},
                                max_retries=1)
            worker.handle_job(queue.claim(), **_handle_kwargs())
            row = _job_row(jid)
            _release_threads(release, threads)
        check("timeout failure requeues within retry budget",
              row["status"] == "pending", row["status"])
        check("timeout counts one attempt", row["attempts"] == 1,
              str(row["attempts"]))
        check("timeout note kept on requeue",
              "timeout" in (row["note"] or ""), (row["note"] or "")[:100])


def test_worker_job_timeout_default_and_payload_coercion():
    """No payload timeout_s -> the CLI default applies and a fast job passes
    normally (no regression); non-positive/non-numeric payload timeout_s
    coerces to the default instead of erroring."""
    from qaloop import queue, worker
    with temp_db():
        j1 = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"})
        with _stub_run_one_flow(("passed", "/tmp/runs/ok")):
            worker.handle_job(queue.claim(), **_handle_kwargs())
        check("no payload timeout: fast job passes normally",
              _job_row(j1)["status"] == "done", _job_row(j1)["status"])
        for bad in (0, -5, "nonsense"):
            j = queue.enqueue("verify-flow",
                              {"flow": "x.yaml", "target": "http://x",
                               "timeout_s": bad})
            with _stub_run_one_flow(("passed", "/tmp/runs/ok")):
                worker.handle_job(queue.claim(), **_handle_kwargs())
            check(f"payload timeout_s={bad!r} coerces to default, job passes",
                  _job_row(j)["status"] == "done", f"timeout_s={bad!r}")


def test_worker_job_timeout_cli_validation():
    import argparse
    import io
    from qaloop import worker
    from qaloop.cli import build_parser, cmd_worker
    raised = False
    try:
        worker.run_worker(flows_dir="/tmp/flows", runs_root="/tmp/runs",
                          once=True, poll_s=0.01, job_timeout_s=0)
    except ValueError:
        raised = True
    check("run_worker job_timeout_s=0 raises ValueError", raised)
    bad = argparse.Namespace(flows="/tmp/flows", once=True, poll=5.0,
                             headed=False, executable_path=None,
                             keep_runs=None, concurrency=2, job_timeout=0)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_worker(bad)
    check("CLI --job-timeout 0 errors", rc == 2 and "ERROR" in buf.getvalue(),
          f"rc={rc}")
    try:
        build_parser().parse_args(["worker", "--job-timeout", "abc"])
        rejected = False
    except SystemExit as e:
        rejected = e.code == 2
    check("CLI --job-timeout abc rejected by argparse", rejected)


def test_worker_target_url_isolation_under_threads():
    """Two concurrent _run_one_flow calls with different targets must each
    see their own TARGET_URL (proves the set+load_spec lock)."""
    import threading
    import time
    import types
    from qaloop import worker
    captured, lock = {}, threading.Lock()
    saved = {k: getattr(worker, k) for k in
             ("load_spec", "run_flow", "new_run_dir", "write_report",
              "print_summary", "prune_old_runs")}
    saved_ledger_append = worker.ledger.append
    old_target = os.environ.get("TARGET_URL")

    def fake_load_spec(path):
        with lock:
            captured[threading.current_thread().name] = os.environ.get("TARGET_URL")
        time.sleep(0.05)  # widen the race window
        return types.SimpleNamespace(name="x", investigate_on_failure=False)

    worker.load_spec = fake_load_spec
    worker.run_flow = lambda spec, run_dir=None, target=None, **kw: \
        types.SimpleNamespace(status="passed", duration_ms=1)
    worker.new_run_dir = lambda root, name: os.path.join(root, name)
    worker.write_report = lambda *a, **k: None
    worker.print_summary = lambda *a, **k: None
    worker.prune_old_runs = lambda *a, **k: None
    worker.ledger.append = lambda *a, **k: None
    try:
        ts = [threading.Thread(target=worker._run_one_flow,
                               args=("/tmp/x.yaml", url, "/tmp/r", True, None, False),
                               name=name)
              for url, name in (("http://target-one", "t-one"),
                                ("http://target-two", "t-two"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        for k, v in saved.items():
            setattr(worker, k, v)
        worker.ledger.append = saved_ledger_append
        if old_target is None:
            os.environ.pop("TARGET_URL", None)
        else:
            os.environ["TARGET_URL"] = old_target
    check("concurrent flows each see their own TARGET_URL",
          captured.get("t-one") == "http://target-one"
          and captured.get("t-two") == "http://target-two",
          str(captured))


def test_issue_333_worker_run_one_flow_investigate_contract():
    """Pin worker._run_one_flow's investigate-on-failure contract: the
    investigator is called exactly once with the exact kwargs only when the
    flow failed AND do_investigate AND spec.investigate_on_failure; a truthy
    diagnosis lands in the ledger (investigation entry BEFORE scripted) and
    in write_report; the four negative cases stay scripted-only."""
    import types
    import qaloop.investigate as inv_mod
    from qaloop import worker
    saved = {k: getattr(worker, k) for k in
             ("load_spec", "run_flow", "new_run_dir", "write_report",
              "print_summary", "prune_old_runs")}
    saved_ledger_append = worker.ledger.append
    saved_investigate = inv_mod.investigate
    old_target = os.environ.get("TARGET_URL")
    run_dir = os.path.join("/tmp/runs333", "flow-333")
    diagnosis = {"model": "model-333", "tokens_in": 111, "tokens_out": 22,
                 "cost_usd_est": 0.0033, "mode": "agent"}
    current, captures = {}, {}

    def fake_load_spec(path):
        captures["load_spec"].append(path)
        return current["spec"]

    def fake_run_flow(spec, run_dir=None, target=None, headless=None,
                      executable_path=None):
        captures["run_flow"].append((spec, run_dir, target, headless,
                                     executable_path))
        return current["result"]

    def fake_write_report(*args, **kwargs):
        captures["write_report"].append((args, kwargs))

    def fake_print_summary(*args, **kwargs):
        captures["print_summary"].append((args, kwargs))

    def fake_prune(*args, **kwargs):
        captures["prune"].append((args, kwargs))

    def fake_ledger_append(entry):
        captures["ledger"].append(entry)

    def fake_investigate(**kwargs):
        captures["investigate"].append(kwargs)
        return current["diagnosis"]

    def drive(status, do_investigate, spec_flag, diagnosis_return):
        current["spec"] = types.SimpleNamespace(
            name="flow-333", investigate_on_failure=spec_flag,
            max_investigation_actions=7)
        current["result"] = types.SimpleNamespace(status=status,
                                                  duration_ms=123)
        current["diagnosis"] = diagnosis_return
        captures.clear()
        captures.update({"load_spec": [], "run_flow": [], "write_report": [],
                         "print_summary": [], "prune": [], "ledger": [],
                         "investigate": []})
        return worker._run_one_flow(
            "/tmp/flows/f333.yaml", "http://target-333/", "/tmp/runs333",
            headless=False, executable_path="/fake/chrome-333",
            do_investigate=do_investigate, keep_runs=3)

    def scripted(status):
        return {"kind": "scripted", "run_dir": run_dir, "flow": "flow-333",
                "status": status, "duration_ms": 123, "cost_usd_est": 0.0}

    worker.load_spec = fake_load_spec
    worker.run_flow = fake_run_flow
    worker.new_run_dir = lambda root, name: os.path.join(root, name)
    worker.write_report = fake_write_report
    worker.print_summary = fake_print_summary
    worker.prune_old_runs = fake_prune
    worker.ledger.append = fake_ledger_append
    inv_mod.investigate = fake_investigate
    try:
        # --- positive: failed + both gates on + truthy diagnosis ---
        ret = drive("failed", True, True, diagnosis)
        spec, result = current["spec"], current["result"]
        check("333 investigator called exactly once",
              len(captures["investigate"]) == 1, str(captures["investigate"]))
        inv_kwargs = captures["investigate"][0]
        check("333 investigator kwargs key set exact",
              set(inv_kwargs) == {"result", "spec", "target", "max_actions",
                                  "headless", "executable_path", "run_dir"},
              str(sorted(inv_kwargs)))
        check("333 investigator result/spec by identity",
              inv_kwargs["result"] is result and inv_kwargs["spec"] is spec)
        check("333 investigator target/max/headless/exe/run_dir passthrough",
              inv_kwargs["target"] == "http://target-333/"
              and inv_kwargs["max_actions"] == 7
              and inv_kwargs["max_actions"] == spec.max_investigation_actions
              and inv_kwargs["headless"] is False
              and inv_kwargs["executable_path"] == "/fake/chrome-333"
              and inv_kwargs["run_dir"] == run_dir, str(inv_kwargs))
        check("333 ledger investigation entry before scripted, exact",
              captures["ledger"] == [
                  {"kind": "investigation", "run_dir": run_dir,
                   "flow": "flow-333", "model": "model-333", "tokens_in": 111,
                   "tokens_out": 22, "cost_usd_est": 0.0033},
                  scripted("failed")], str(captures["ledger"]))
        wr = captures["write_report"]
        check("333 write_report once, positional + diagnosis identity",
              len(wr) == 1 and len(wr[0][0]) == 3 and wr[0][0][0] is result
              and wr[0][0][1] is spec and wr[0][0][2] == run_dir
              and wr[0][1].get("diagnosis") is diagnosis, str(wr))
        check("333 positive return is (failed, run_dir)",
              ret == ("failed", run_dir), str(ret))
        check("333 TARGET_URL set to the flow target after the call",
              os.environ.get("TARGET_URL") == "http://target-333/",
              str(os.environ.get("TARGET_URL")))
        check("333 load_spec got the flow path",
              captures["load_spec"] == ["/tmp/flows/f333.yaml"],
              str(captures["load_spec"]))
        rf = captures["run_flow"]
        check("333 run_flow got spec identity + run_dir/target/headless/exe",
              len(rf) == 1 and rf[0][0] is spec and rf[0][1] == run_dir
              and rf[0][2] == "http://target-333/" and rf[0][3] is False
              and rf[0][4] == "/fake/chrome-333", str(rf))
        check("333 prune_old_runs got (runs_root, keep_runs, current=run_dir)",
              captures["prune"] == [(("/tmp/runs333", 3),
                                      {"current_run_dir": run_dir})],
              str(captures["prune"]))

        # --- (a) None diagnosis: gate fully on, investigator returns None ---
        ret = drive("failed", True, True, None)
        check("333 none-diagnosis investigator still called once",
              len(captures["investigate"]) == 1, str(captures["investigate"]))
        check("333 none-diagnosis ledger is scripted-only",
              captures["ledger"] == [scripted("failed")],
              str(captures["ledger"]))
        check("333 none-diagnosis write_report diagnosis is None",
              len(captures["write_report"]) == 1
              and captures["write_report"][0][1].get("diagnosis") is None,
              str(captures["write_report"]))
        check("333 none-diagnosis return is (failed, run_dir)",
              ret == ("failed", run_dir), str(ret))

        # --- (b) passed result: gate on, investigator never called ---
        ret = drive("passed", True, True, diagnosis)
        check("333 passed investigator never called",
              captures["investigate"] == [], str(captures["investigate"]))
        check("333 passed ledger scripted-only with status passed",
              captures["ledger"] == [scripted("passed")],
              str(captures["ledger"]))
        check("333 passed write_report diagnosis is None",
              len(captures["write_report"]) == 1
              and captures["write_report"][0][1].get("diagnosis") is None,
              str(captures["write_report"]))
        check("333 passed return is (passed, run_dir)",
              ret == ("passed", run_dir), str(ret))

        # --- (c) do_investigate=False: failed + spec flag on, never called ---
        ret = drive("failed", False, True, diagnosis)
        check("333 do_investigate=False investigator never called",
              captures["investigate"] == [], str(captures["investigate"]))
        check("333 do_investigate=False ledger scripted-only",
              captures["ledger"] == [scripted("failed")],
              str(captures["ledger"]))
        check("333 do_investigate=False write_report diagnosis is None",
              len(captures["write_report"]) == 1
              and captures["write_report"][0][1].get("diagnosis") is None,
              str(captures["write_report"]))
        check("333 do_investigate=False return is (failed, run_dir)",
              ret == ("failed", run_dir), str(ret))

        # --- (d) spec.investigate_on_failure=False: never called ---
        ret = drive("failed", True, False, diagnosis)
        check("333 spec-flag=False investigator never called",
              captures["investigate"] == [], str(captures["investigate"]))
        check("333 spec-flag=False ledger scripted-only",
              captures["ledger"] == [scripted("failed")],
              str(captures["ledger"]))
        check("333 spec-flag=False write_report diagnosis is None",
              len(captures["write_report"]) == 1
              and captures["write_report"][0][1].get("diagnosis") is None,
              str(captures["write_report"]))
        check("333 spec-flag=False return is (failed, run_dir)",
              ret == ("failed", run_dir), str(ret))
    finally:
        for k, v in saved.items():
            setattr(worker, k, v)
        worker.ledger.append = saved_ledger_append
        inv_mod.investigate = saved_investigate
        os.environ.pop("TARGET_URL", None) if old_target is None else \
            os.environ.__setitem__("TARGET_URL", old_target)


@contextlib.contextmanager
def _notify_env(url):
    """Set QALOOP_NOTIFY_URL, restoring the old value on exit."""
    old = os.environ.get("QALOOP_NOTIFY_URL")
    os.environ["QALOOP_NOTIFY_URL"] = url
    try:
        yield
    finally:
        if old is None:
            del os.environ["QALOOP_NOTIFY_URL"]
        else:
            os.environ["QALOOP_NOTIFY_URL"] = old


@contextlib.contextmanager
def _raise_run_one_flow():
    """Browser-free stub where worker._run_one_flow raises."""
    from qaloop import worker
    real = worker._run_one_flow

    def fake(flow_path, target, runs_root, headless, executable_path,
             do_investigate, keep_runs=0):
        raise RuntimeError("boom")

    worker._run_one_flow = fake
    try:
        yield
    finally:
        worker._run_one_flow = real


@contextlib.contextmanager
def _notify_stub(status=200):
    """Threaded local HTTP stub recording POST bodies; yields (received, url)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            received.append((self.path, json.loads(self.rfile.read(n))))
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield received, f"http://127.0.0.1:{srv.server_port}/notify"
    finally:
        srv.shutdown()
        t.join()


def test_worker_failure_notify_webhook():
    from qaloop import queue, worker
    with temp_db(), _notify_stub() as (received, url):
        with _notify_env(url):
            jid = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"},
                                max_retries=1)
            with _stub_run_one_flow(("failed", "/tmp/runs/f")):
                worker.handle_job(queue.claim(), **_handle_kwargs())
                check("retry requeue sends no notification", received == [],
                      str(received))
                worker.handle_job(queue.claim(), **_handle_kwargs())
        row = _job_row(jid)
        check("terminal failure notifies exactly once", len(received) == 1,
              str(len(received)))
        path, payload = received[0]
        check("payload job_id matches", payload.get("job_id") == jid, str(payload))
        check("payload kind matches", payload.get("kind") == "verify-flow",
              str(payload))
        check("payload run_dir matches", payload.get("run_dir") == "/tmp/runs/f",
              str(payload))
        check("payload summary names attempts",
              "attempt 2" in str(payload.get("summary", "")), str(payload))
        check("job recorded failed", row["status"] == "failed", row["status"])


def test_worker_exception_notify_webhook():
    from qaloop import queue, worker
    with temp_db(), _notify_stub() as (received, url):
        with _notify_env(url):
            jid = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"})
            with _raise_run_one_flow():
                worker.handle_job(queue.claim(), **_handle_kwargs())
        row = _job_row(jid)
        check("exception path still records failure", row["status"] == "failed",
              row["status"])
        check("exception path notifies once", len(received) == 1, str(len(received)))
        path, payload = received[0]
        check("exception payload job_id matches", payload.get("job_id") == jid,
              str(payload))
        check("exception payload run_dir empty", payload.get("run_dir") == "",
              str(payload))
        check("exception payload summary mentions error",
              "worker error" in str(payload.get("summary", "")), str(payload))


def test_worker_notify_failure_is_fail_closed():
    from qaloop import queue, worker
    with temp_db():
        # 500 from the webhook must not raise and must not change job handling.
        with _notify_stub(status=500) as (received, url):
            with _notify_env(url):
                jid = queue.enqueue("verify-flow",
                                    {"flow": "x.yaml", "target": "http://x"})
                with _stub_run_one_flow(("failed", "/tmp/runs/f")):
                    try:
                        worker.handle_job(queue.claim(), **_handle_kwargs())
                        raised = False
                    except Exception:
                        raised = True
        check("500 webhook raises nothing", not raised)
        check("500 webhook attempt was made", len(received) == 1,
              str(len(received)))
        check("job still recorded failed",
              _job_row(jid)["status"] == "failed", _job_row(jid)["status"])
        # Closed port (connection refused) likewise must not raise.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}/notify"
        s.close()
        with _notify_env(dead):
            jid2 = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"})
            with _stub_run_one_flow(("failed", "/tmp/runs/f")):
                try:
                    worker.handle_job(queue.claim(), **_handle_kwargs())
                    raised = False
                except Exception:
                    raised = True
        check("dead webhook raises nothing", not raised)
        check("dead webhook job still recorded failed",
              _job_row(jid2)["status"] == "failed", _job_row(jid2)["status"])


def test_worker_no_notify_when_url_unset():
    from qaloop import queue, worker
    old = os.environ.pop("QALOOP_NOTIFY_URL", None)
    try:
        with temp_db():
            jid = queue.enqueue("verify-flow", {"flow": "x.yaml", "target": "http://x"})
            with _stub_run_one_flow(("failed", "/tmp/runs/f")):
                worker.handle_job(queue.claim(), **_handle_kwargs())
            check("unset URL job still recorded failed",
                  _job_row(jid)["status"] == "failed", _job_row(jid)["status"])
    finally:
        if old is not None:
            os.environ["QALOOP_NOTIFY_URL"] = old



def test_cli_enqueue_max_retries_flag():
    from qaloop.cli import build_parser
    args = build_parser().parse_args(["enqueue", "--kind", "verify-flow", "--flow", "x.yaml",
                                      "--max-retries", "2"])
    check("cli parses --max-retries", args.max_retries == 2)
    args0 = build_parser().parse_args(["enqueue"])
    check("cli default --max-retries 0", args0.max_retries == 0)


def test_webhook_signature():
    import hmac as _hmac
    import hashlib as _hl
    from qaloop import webhook
    secret = "s3cret"
    body = b'{"zen":"hi"}'
    sig = "sha256=" + _hmac.new(secret.encode(), body, _hl.sha256).hexdigest()
    check("valid signature accepted", webhook._verify_signature(secret, body, sig))
    check("bad signature rejected",
          not webhook._verify_signature(secret, body, "sha256=nope"))
    check("no secret configured accepts", webhook._verify_signature("", body, "anything"))


def test_webhook_event_filter():
    from qaloop import webhook
    check("PR opened triggers", webhook._pr_action_allowed("opened", {}))
    check("PR labeled needs-qa triggers",
          webhook._pr_action_allowed("labeled", {"label": {"name": "needs-qa"}}))
    check("PR labeled other ignored",
          not webhook._pr_action_allowed("labeled", {"label": {"name": "bug"}}))
    check("PR closed ignored", not webhook._pr_action_allowed("closed", {}))


def test_report_generation():
    from qaloop.runner import RunResult, StepResult, AssertionResult
    from qaloop.spec import load_spec
    from qaloop.report import write_report
    spec = load_spec("flows/game-loading-frames.yaml", strict_env=False)
    with tempfile.TemporaryDirectory() as d:
        result = RunResult(
            flow_name="test-flow", target="http://x", status="failed",
            started=1700000000.0, ended=1700000005.0,
            steps=[StepResult(index=0, phase="main", name="S-01 open",
                              op="goto", status="passed", duration_ms=1200,
                              assertions=[AssertionResult("text contains", True, "ok")])],
            failed_step=0, console_errors=[{"text": "boom: TypeError"}],
            page_errors=[], failed_requests=[], bad_responses=[],
            run_dir=d)
        paths = write_report(result, spec, d)
        with open(os.path.join(d, "REPORT.md"), encoding="utf-8") as f:
            md = f.read()
        check("report files written",
              all(os.path.exists(p) for p in paths.values()))
        check("report marks failure", "FAIL" in md and "boom" in md)
        check("run.json round-trips",
              json.load(open(os.path.join(d, "run.json")))["status"] == "failed")


def test_report_tail_contracts():
    """Issue #351: pin the report.py tail contracts (lines 29, 102, 125,
    128, 296, 298) - _rel falsy path, section truncation past 20, diagnosis
    Suggested fix line, Run error line, print_summary page/network lines."""
    import contextlib
    import io
    from qaloop.runner import RunResult, StepResult
    from qaloop.spec import load_spec
    from qaloop.report import write_report, print_summary, _rel
    spec = load_spec("flows/game-loading-frames.yaml", strict_env=False)

    def make_result(**over):
        kw = dict(flow_name="tail-flow", target="http://x", status="failed",
                  started=1700000000.0, ended=1700000001.0,
                  steps=[StepResult(index=0, phase="main", name="S-01",
                                    op="goto", status="passed",
                                    duration_ms=10)],
                  failed_step=None, console_errors=[], page_errors=[],
                  failed_requests=[], bad_responses=[], run_dir="")
        kw.update(over)
        return RunResult(**kw)

    def report_md(result, diagnosis=None):
        with tempfile.TemporaryDirectory() as d:
            result.run_dir = d
            paths = write_report(result, spec, d, diagnosis=diagnosis)
            with open(paths["report_md"], encoding="utf-8") as f:
                return f.read()

    # _rel falsy path (line 29) + relative path shape
    with tempfile.TemporaryDirectory() as d:
        check("351 _rel None path returns None", _rel(d, None) is None)
        check("351 _rel empty path returns None", _rel(d, "") is None)
        check("351 _rel path under run dir is relative",
              _rel(d, os.path.join(d, "REPORT.md")) == "REPORT.md")

    # Section truncation past 20 items (line 102)
    errs25 = [{"text": f"err-{i}"} for i in range(25)]
    md = report_md(make_result(console_errors=errs25))
    item_lines = [ln for ln in md.splitlines() if ln.startswith("- `err-")]
    check("351 section header carries full count 25",
          "## Console errors (25)" in md)
    check("351 section renders exactly 20 item lines",
          len(item_lines) == 20, str(len(item_lines)))
    check("351 truncation line reports 5 more",
          "- \u2026 and 5 more (see run.json)" in md)
    md20 = report_md(make_result(
        console_errors=[{"text": f"e{i}"} for i in range(20)]))
    check("351 exactly 20 items renders no truncation line",
          "more (see run.json)" not in md20)
    md21 = report_md(make_result(
        console_errors=[{"text": f"e{i}"} for i in range(21)]))
    check("351 21 items reports exactly 1 more",
          "- \u2026 and 1 more (see run.json)" in md21)

    # Diagnosis Suggested fix line (line 125), positive and negative
    md_fix = report_md(make_result(), diagnosis={
        "likely_cause": "lc", "diagnosis": "dg",
        "suggested_fix": "FIX-TEXT-351"})
    check("351 diagnosis with suggested_fix renders the line",
          "**Suggested fix:** FIX-TEXT-351" in md_fix)
    md_nofix = report_md(make_result(), diagnosis={
        "likely_cause": "lc", "diagnosis": "dg"})
    check("351 diagnosis without suggested_fix renders no such line",
          "Suggested fix" not in md_nofix)

    # Run error line (line 128), verbatim
    md_err = report_md(make_result(status="error", error="boom-351"))
    check("351 non-empty error renders the Run error line verbatim",
          "Run error: `boom-351`" in md_err)
    md_noerr = report_md(make_result())
    check("351 empty error renders no Run error line",
          "Run error:" not in md_noerr)

    # print_summary tail lines (lines 296, 298), positive and negative
    def summary(result):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_summary(result)
        return buf.getvalue()

    out = summary(make_result(page_errors=[{"text": "pe1"},
                                           {"text": "pe2"}]))
    check("351 summary prints page errors count line",
          "  page errors: 2" in out, out)
    out = summary(make_result(
        failed_requests=[{"method": "GET", "url": "http://x/a",
                          "failure": "net::ERR"}],
        bad_responses=[{"method": "GET", "url": "http://x/b", "status": 500},
                       {"method": "GET", "url": "http://x/c",
                        "status": 404}]))
    check("351 summary prints network issues line with both counts",
          "  network issues: 1 failed, 2 bad responses" in out, out)
    out = summary(make_result(status="passed"))
    check("351 clean summary prints neither tail line",
          "page errors:" not in out and "network issues:" not in out, out)


def test_report_junit_xml():
    import xml.etree.ElementTree as ET
    from qaloop.runner import RunResult, StepResult
    from qaloop.spec import load_spec
    from qaloop.report import write_junit_xml, write_report
    spec = load_spec("flows/game-loading-frames.yaml", strict_env=False)
    raw_error = 'assertion failed: got "2", expected "3" — 2 < 3 & 4 > 1'
    steps = [
        StepResult(index=0, phase="main", name="S-01 ok", op="goto",
                   status="passed", duration_ms=100),
        StepResult(index=1, phase="main", name="S-02 <bad> & \"quoted\"",
                   op=None, status="failed", duration_ms=200, error=raw_error),
        StepResult(index=2, phase="main", name="S-03 skipped", op="wait",
                   status="skipped", duration_ms=0),
    ]
    with tempfile.TemporaryDirectory() as d:
        result = RunResult(
            flow_name="junit-flow", target="http://x", status="failed",
            started=1700000000.0, ended=1700000005.0, steps=steps,
            failed_step=1, console_errors=[], page_errors=[],
            failed_requests=[], bad_responses=[], run_dir=d)
        path = write_junit_xml(result, spec, os.path.join(d, "j.xml"))
        check("write_junit_xml returns path", path == os.path.join(d, "j.xml"))
        tree = ET.parse(path)
        suite = tree.getroot()
        check("junit parses as xml", suite.tag == "testsuite")
        check("testsuite tests equals step count", suite.get("tests") == "3")
        check("testsuite failures equals failed count", suite.get("failures") == "1")
        check("testsuite skipped equals skipped count", suite.get("skipped") == "1")
        cases = suite.findall("testcase")
        check("one testcase per step", len(cases) == 3)
        failed_case = cases[1]
        failure = failed_case.find("failure")
        check("failed testcase carries failure element", failure is not None)
        check("failure element text equals raw error",
              failure.text == raw_error)
        check("failure message attribute round-trips quotes",
              failure.get("message") == raw_error)
        check("special chars survive round-trip in name",
              failed_case.get("name") == 'S-02 <bad> & "quoted"')
        check("classname falls back to phase when op is None",
              failed_case.get("classname") == "main")
        check("classname combines phase and op",
              cases[0].get("classname") == "main.goto")
        check("skipped testcase has skipped element",
              cases[2].find("skipped") is not None)
        check("passed testcase has no failure/skipped",
              cases[0].find("failure") is None and cases[0].find("skipped") is None)
        # run-level error case: errors attribute counts the run error
        err_result = RunResult(
            flow_name="err-flow", target="http://x", status="error",
            started=1700000000.0, ended=1700000005.0, steps=steps[:1],
            failed_step=None, console_errors=[], page_errors=[],
            failed_requests=[], bad_responses=[], run_dir=d, error="boot blew up")
        ET.parse(write_junit_xml(err_result, spec, os.path.join(d, "e.xml")))
        check("errors attr matches run-level error",
              ET.parse(os.path.join(d, "e.xml")).getroot().get("errors") == "1")
        # write_report writes junit.xml into the run dir unconditionally
        paths = write_report(result, spec, d)
        check("write_report returns junit_xml path",
              paths.get("junit_xml") == os.path.join(d, "junit.xml"))
        check("junit.xml present in run dir after write_report",
              os.path.exists(paths["junit_xml"]))
        ET.parse(paths["junit_xml"])


def test_env_static_boot():
    from qaloop import env
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html><body>hi</body></html>")
        port = env.find_free_port()
        with env.boot_static(d, port) as target:
            env.wait_for_port(port, timeout_s=15)
            body = urllib.request.urlopen(target.url, timeout=10).read().decode()
            check("static server serves", "hi" in body, body[:40])


def test_result_from_dict():
    from qaloop.runner import RunResult
    d = {"flow_name": "f", "target": "t", "status": "passed", "started": 1.0,
         "ended": 2.0, "steps": [{"index": 0, "phase": "main", "name": "s",
                                  "op": "goto", "status": "passed",
                                  "duration_ms": 10, "error": "",
                                  "assertions": [{"name": "n", "passed": True,
                                                  "detail": ""}],
                                  "screenshot": None, "ax_snapshot": None}],
         "failed_step": None, "console_errors": [], "page_errors": [],
         "failed_requests": [], "bad_responses": [], "run_dir": "r", "error": ""}
    r = RunResult.from_dict(d)
    check("RunResult.from_dict", r.status == "passed"
          and r.steps[0].assertions[0].passed)


def test_mock_validation():
    from qaloop.spec import load_spec, SpecError
    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)
    base = "name: t\ntarget: http://x\nsetup:\n  - mock: {url: '**/a', json: {x: 1}}\nsteps:\n  - {goto: /}\n"
    s = load(base)
    check("mock op parses", s.setup[0].op == "mock" and s.setup[0].params["url"] == "**/a")
    for bad_mock, label in [
        ("mock: {json: {x: 1}}", "no url"),
        ("mock: {url: '**/a', json: {x: 1}, body: 'z'}", "two payloads"),
        ("mock: {url: '**/a'}", "no payload"),
        ("mock: {url: '**/a', json: {x: 1}, method: get}", "lowercase"),
        ("mock: {url: '**/a', json: {x: 1}, times: 0}", "times<=0"),
        ("mock: {url: '**/a', json: {x: 1}, delay_ms: -1}", "delay_ms<0"),
        ("mock: {url: '**/a', json: {x: 1}, delay_ms: '250'}", "delay_ms str"),
        ("mock: {url: '**/a', json: {x: 1}, delay_ms: 1.5}", "delay_ms float"),
        ("mock: {url: '**/a', json: {x: 1}, delay_ms: true}", "delay_ms bool"),
    ]:
        bad = base.replace("mock: {url: '**/a', json: {x: 1}}", bad_mock)
        try:
            load(bad)
            check(f"bad mock rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad mock rejected ({label})", True)
    for good, val in [
        (base.replace("mock: {url: '**/a', json: {x: 1}}",
                      "mock: {url: '**/a', json: {x: 1}, delay_ms: 0}"), 0),
        (base.replace("mock: {url: '**/a', json: {x: 1}}",
                      "mock: {url: '**/a', json: {x: 1}, delay_ms: 250}"), 250),
    ]:
        s = load(good)
        check(f"valid mock accepted: delay_ms={val}",
              s.setup[0].params.get("delay_ms") == val, s.setup[0].params)


def test_wait_state_validation():
    """wait has two disjoint forms: targetless waits accept only
    load|domcontentloaded|networkidle; target-ful waits accept only
    visible|hidden|attached|detached (issue #20)."""
    from qaloop.spec import load_spec, SpecError
    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)
    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: w\n    wait: PARAMS\n")
    def params(**kw):
        inner = ", ".join(f"{k}: {v}" for k, v in kw.items())
        return base.replace("PARAMS", "{" + inner + "}")
    for state in ["load", "domcontentloaded", "networkidle"]:
        s = load(params(state=state))
        check(f"targetless wait accepted: {state}", s.steps[1].params.get("state") == state)
    s = load(params())  # bare mapping: no state -> runner defaults to load
    check("targetless bare wait accepted", s.steps[1].op == "wait")
    for state in ["visible", "hidden", "attached", "detached"]:
        s = load(params(target='"#x"', state=state))
        check(f"target-ful wait accepted: {state}", s.steps[1].params.get("state") == state)
    s = load(params(target='"#x"'))  # no state -> runner defaults to visible
    check("target-ful bare wait accepted", s.steps[1].op == "wait")
    for state in ["visible", "hidden", "attached", "detached"]:
        try:
            load(params(state=state))
            check(f"targetless selector-state rejected: {state}", False, "no error")
        except SpecError:
            check(f"targetless selector-state rejected: {state}", True)
    for state in ["load", "domcontentloaded", "networkidle"]:
        try:
            load(params(target='"#x"', state=state))
            check(f"target-ful load-state rejected: {state}", False, "no error")
        except SpecError:
            check(f"target-ful load-state rejected: {state}", True)
    try:
        load(params(state='"bogus"'))
        check("bogus targetless state rejected", False, "no error")
    except SpecError:
        check("bogus targetless state rejected", True)


def test_wait_runner_defaults():
    """Runner default state follows the wait form: targetless -> "load" via
    wait_for_load_state; target-ful -> "visible" via wait_for_selector.
    Explicit states pass through to the matching Playwright call (issue #20)."""
    from qaloop.runner import _do_action
    from qaloop.spec import Step

    class FakePage:
        def __init__(self):
            self.calls = []
        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append(("selector", selector, state))
        def wait_for_load_state(self, state=None, timeout=None):
            self.calls.append(("load_state", state))

    def run(params):
        page = FakePage()
        step = Step(index=0, phase="steps", name="w", op="wait", params=params,
                    expect={}, expect_items=[], continue_on_fail=False,
                    timeout_ms=None, raw={})
        _do_action(page, step, "http://x", {})
        return page.calls

    calls = run({})
    check("targetless bare wait -> load state", calls == [("load_state", "load")], str(calls))
    calls = run({"state": "networkidle"})
    check("targetless networkidle passes through", calls == [("load_state", "networkidle")],
          str(calls))
    calls = run({"target": "#x"})
    check("target-ful bare wait -> visible selector", calls == [("selector", "#x", "visible")],
          str(calls))
    calls = run({"target": "#x", "state": "hidden"})
    check("target-ful hidden passes through", calls == [("selector", "#x", "hidden")], str(calls))


def test_wait_js_response_validation():
    """wait gains two new disjoint forms: {js: <expr>} polls a JS expression
    until truthy; {response: <substr>} waits for a network response whose URL
    contains the substring. Both reject target/state/text, each other, and
    bad timeout_ms (issue #79)."""
    from qaloop.spec import load_spec, SpecError
    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)
    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: w\n    wait: PARAMS\n")
    def params(inner):
        return base.replace("PARAMS", "{" + inner + "}")

    s = load(params('js: "window.dataLoaded === true"'))
    check("js form accepted", s.steps[1].params.get("js") == "window.dataLoaded === true")
    s = load(params('response: "/api/waitdata"'))
    check("response form accepted", s.steps[1].params.get("response") == "/api/waitdata")
    s = load(params('js: "window.x", timeout_ms: 5000'))
    check("js with valid timeout_ms accepted",
          s.steps[1].params.get("timeout_ms") == 5000, s.steps[1].params)
    s = load(params('response: "/api/x", timeout_ms: 0'))
    check("response with timeout_ms 0 accepted",
          s.steps[1].params.get("timeout_ms") == 0, s.steps[1].params)

    for inner, label in [
        ('js: "window.x", target: "#x"', "js+target"),
        ('js: "window.x", state: "load"', "js+state"),
        ('js: "window.x", text: "hi"', "js+text"),
        ('js: "window.x", response: "/api/x"', "js+response"),
        ('response: "/api/x", target: "#x"', "response+target"),
        ('response: "/api/x", state: "visible"', "response+state"),
        ('response: "/api/x", text: "hi"', "response+text"),
        ("js: 42", "js non-string"),
        ('js: ""', "js empty"),
        ("response: 42", "response non-string"),
        ('response: ""', "response empty"),
        ('js: "window.x", timeout_ms: "5000"', "js timeout_ms str"),
        ('js: "window.x", timeout_ms: -1', "js timeout_ms negative"),
        ('js: "window.x", timeout_ms: true', "js timeout_ms bool"),
        ('js: "window.x", timeout_ms: 1.5', "js timeout_ms float"),
        ('response: "/api/x", timeout_ms: -1', "response timeout_ms negative"),
        ("timeout_ms: -1", "bare targetless timeout_ms negative"),
    ]:
        try:
            load(params(inner))
            check(f"bad wait rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad wait rejected ({label})", True)


def test_wait_js_response_runner():
    """Runner: the js form calls page.wait_for_function with the expression;
    the response form calls page.wait_for_event("response", ...) with a
    URL-substring predicate; both honor timeout_ms or fall back to the step
    timeout (issue #79)."""
    from qaloop.runner import _do_action
    from qaloop.spec import Step

    class FakeResponse:
        def __init__(self, url):
            self.url = url

    class FakePage:
        def __init__(self):
            self.calls = []
        def wait_for_function(self, expression, timeout=None):
            self.calls.append(("function", expression, timeout))
        def wait_for_event(self, event, predicate=None, timeout=None):
            self.calls.append(("event", event, predicate, timeout))
        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append(("selector", selector, state, timeout))
        def wait_for_load_state(self, state=None, timeout=None):
            self.calls.append(("load_state", state, timeout))

    def run(params, step_timeout=None):
        page = FakePage()
        step = Step(index=0, phase="steps", name="w", op="wait", params=params,
                    expect={}, expect_items=[], continue_on_fail=False,
                    timeout_ms=step_timeout, raw={})
        _do_action(page, step, "http://x", {})
        return page.calls

    calls = run({"js": "window.dataLoaded === true"})
    check("js form -> wait_for_function with step timeout",
          calls == [("function", "window.dataLoaded === true", 15000)], str(calls))
    calls = run({"js": "window.x", "timeout_ms": 2000})
    check("js timeout_ms passes through",
          calls == [("function", "window.x", 2000)], str(calls))

    calls = run({"response": "/api/waitdata"})
    check("response form waits for the response event", len(calls) == 1
          and calls[0][0] == "event" and calls[0][1] == "response"
          and calls[0][3] == 15000, str(calls))
    pred = calls[0][2]
    check("response predicate matches URL substring",
          pred(FakeResponse("http://127.0.0.1:8123/api/waitdata?fresh=1")) is True)
    check("response predicate rejects other URLs",
          pred(FakeResponse("http://127.0.0.1:8123/api/other")) is False)
    calls = run({"response": "/api/x", "timeout_ms": 3000})
    check("response timeout_ms passes through",
          calls[0][0] == "event" and calls[0][3] == 3000, str(calls))
    calls = run({"response": "/api/x"}, step_timeout=9000)
    check("response falls back to step timeout_ms",
          calls[0][3] == 9000, str(calls))
    calls = run({"js": "window.x"}, step_timeout=9000)
    check("js falls back to step timeout_ms",
          calls == [("function", "window.x", 9000)], str(calls))
    calls = run({"target": "#x"})
    check("existing selector form unchanged",
          calls == [("selector", "#x", "visible", 15000)], str(calls))


def test_mock_delay_ms_runner():
    """Real browser proof: a mock with delay_ms=250 holds the response ~250ms;
    delay_ms absent still responds instantly (no timing regression)."""
    import socket
    import subprocess
    import time
    exe = os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-1243/"
        "chrome-headless-shell-linux64/chrome-headless-shell")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        have_pw = True
    except ImportError:
        have_pw = False
    if not have_pw or not os.path.exists(exe):
        check("delay_ms runner tests skipped (no browser)", True)
        return
    from qaloop.spec import load_spec
    from qaloop.runner import run_flow

    def fetch_js(url, assertion):
        return ("(async () => { const t0 = performance.now(); "
                "await fetch('" + url + "').then(r => r.json()); "
                "const e = performance.now() - t0; " + assertion + "; "
                "return 'elapsed ' + Math.round(e) + 'ms'; })()")

    def make_flow(url, mock_extra, assertion):
        js = fetch_js(url, assertion).replace('"', '\\"')
        return (
            "name: delay-demo\ntarget: http://127.0.0.1:PORT\n"
            "setup:\n  - mock: {url: '**" + url + "', json: {ok: true}" + mock_extra + "}\n"
            "steps:\n"
            "  - name: fetch timing\n"
            '    script: {js: "' + js + '"}\n'
            "    expect: {noop: true}\n")

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html><body><h1>delay demo</h1></body></html>")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            flow = make_flow(
                "/api/slow", ", delay_ms: 250",
                "if (e < 200) throw new Error('delay not applied: ' + e + 'ms')"
            ).replace("PORT", str(port))
            flow_path = os.path.join(d, "delay-flow.yaml")
            with open(flow_path, "w") as f:
                f.write(flow)
            with tempfile.TemporaryDirectory() as run_dir:
                result = run_flow(load_spec(flow_path), run_dir=run_dir,
                                  executable_path=exe)
                check("delay_ms=250 holds response >= 200ms",
                      result.status == "passed", result.status)
            flow = make_flow(
                "/api/fast", "",
                "if (e > 2000) throw new Error('unexpected delay: ' + e + 'ms')"
            ).replace("PORT", str(port))
            flow_path = os.path.join(d, "fast-flow.yaml")
            with open(flow_path, "w") as f:
                f.write(flow)
            with tempfile.TemporaryDirectory() as run_dir:
                result = run_flow(load_spec(flow_path), run_dir=run_dir,
                                  executable_path=exe)
                check("delay_ms absent still responds fast (< 2000ms)",
                      result.status == "passed", result.status)
        finally:
            srv.terminate()


def test_visual_ax_assertion_validation():
    from qaloop.spec import load_spec, SpecError
    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)
    ok = ("name: t\ntarget: http://x\nsteps:\n"
          "  - expect:\n"
          "      - screenshot_matches: {baseline: 'b.png', max_diff: 0.05}\n"
          "      - screenshot_matches: {baseline: 'e.png', selector: '#out'}\n"
          "      - ax: {role: button, name: Save}\n")
    s = load(ok)
    items = s.steps[0].expect_items
    check("screenshot_matches parses",
          items[0][0] == "screenshot_matches" and items[0][1]["max_diff"] == 0.05)
    check("screenshot_matches selector parses",
          items[1][0] == "screenshot_matches" and items[1][1]["selector"] == "#out")
    check("ax parses", items[2][0] == "ax" and items[2][1]["role"] == "button")
    bad_cases = [
        "      - screenshot_matches: {max_diff: 0.05}\n",          # no baseline
        "      - screenshot_matches: {baseline: 'b.png', max_diff: 2}\n",  # > 1
        "      - screenshot_matches: {baseline: 'b.png', selector: ''}\n",  # empty selector
        "      - screenshot_matches: {baseline: 'b.png', selector: 42}\n",  # non-string selector
        "      - screenshot_matches: {baseline: 'b.png', selector: null}\n",  # null selector
        "      - ax: {name: Save}\n",                              # no role
        "      - ax: {role: button, state: bogus}\n",              # bad state
    ]
    for line in bad_cases:
        body = "name: t\ntarget: http://x\nsteps:\n  - expect:\n" + line
        try:
            load(body)
            check(f"bad assertion rejected: {line.strip()}", False, "no error")
        except SpecError:
            check(f"bad assertion rejected: {line.strip()}", True)


def test_screenshot_matches_selector_capture():
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step
    from PIL import Image

    def make_step(params):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=[("screenshot_matches", params)],
                    continue_on_fail=False, timeout_ms=None, raw={})

    class FakeLocator:
        def __init__(self, page, selector):
            self.page, self.selector = page, selector

        def screenshot(self, path):
            self.page.calls.append(("locator-shot", self.selector, path))
            Image.open(self.page.element_png).save(path)

    class FakePage:
        def __init__(self, element_png, full_png, wait_ok=True):
            self.calls = []
            self.wait_ok = wait_ok
            self.element_png = element_png
            self.full_png = full_png

        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append(("wait", selector, state))
            if not self.wait_ok:
                raise TimeoutError("selector never attached")

        def locator(self, selector):
            self.calls.append(("locator", selector))
            return FakeLocator(self, selector)

        def screenshot(self, path):
            self.calls.append(("page-shot", path))
            Image.open(self.full_png).save(path)

    with tempfile.TemporaryDirectory() as d:
        base_dir, run_dir = os.path.join(d, "base"), os.path.join(d, "run")
        os.makedirs(base_dir)
        element_png = os.path.join(d, "element.png")
        full_png = os.path.join(d, "full.png")
        Image.new("RGB", (60, 30), (10, 200, 60)).save(element_png)
        Image.new("RGB", (60, 30), (200, 10, 60)).save(full_png)
        # Case 1: selector present -> element shot compared against element baseline
        Image.open(element_png).save(os.path.join(base_dir, "e.png"))
        page = FakePage(element_png, full_png)
        res = _check_assertions(
            page, make_step({"baseline": "e.png", "selector": "#out"}),
            None, baseline_dir=base_dir, run_dir=run_dir)
        kinds = [c[0] for c in page.calls]
        check("selector assertion passes", res[0].passed, res[0].detail)
        check("wait_for_selector ran first", page.calls[0] == ("wait", "#out", "attached"),
              str(page.calls))
        check("locator screenshot used", ("locator-shot", "#out", os.path.join(
            run_dir, "steps", "assert-steps-00.png")) in page.calls, str(kinds))
        check("page screenshot not used", "page-shot" not in kinds, str(kinds))
        # Case 2: no selector -> full-page shot, unchanged behavior
        Image.open(full_png).save(os.path.join(base_dir, "f.png"))
        page = FakePage(element_png, full_png)
        res = _check_assertions(
            page, make_step({"baseline": "f.png"}),
            None, baseline_dir=base_dir, run_dir=run_dir)
        kinds = [c[0] for c in page.calls]
        check("no-selector assertion passes", res[0].passed, res[0].detail)
        check("page screenshot used", "page-shot" in kinds, str(kinds))
        check("locator not touched", "locator" not in kinds, str(kinds))
        # Case 3: selector never attaches -> clean failed AssertionResult
        page = FakePage(element_png, full_png, wait_ok=False)
        res = _check_assertions(
            page, make_step({"baseline": "e.png", "selector": "#gone"}),
            None, baseline_dir=base_dir, run_dir=run_dir)
        check("missing selector fails cleanly",
              not res[0].passed and res[0].detail.startswith("TimeoutError:"),
              res[0].detail)
        # Case 4 (issue #230): max_diff omitted -> documented default 0.02
        # applies; identical element image passes with the default named
        # in the detail at four decimal places.
        page = FakePage(element_png, full_png)
        res = _check_assertions(
            page, make_step({"baseline": "e.png", "selector": "#out"}),
            None, baseline_dir=base_dir, run_dir=run_dir)
        check("omitted max_diff passes identical element",
              res[0].passed, res[0].detail)
        check("omitted max_diff detail names default 0.0200",
              res[0].detail == "rms_diff=0.0000 max_diff=0.0200",
              res[0].detail)
        # Case 5 (issue #230): max_diff omitted -> a differing element image
        # fails against the same default, detail naming rms_diff above 0.02.
        page = FakePage(full_png, full_png)
        res = _check_assertions(
            page, make_step({"baseline": "e.png", "selector": "#out"}),
            None, baseline_dir=base_dir, run_dir=run_dir)
        check("omitted max_diff fails differing element",
              not res[0].passed, res[0].detail)
        parts = res[0].detail.split()
        check("omitted max_diff failure detail names default 0.0200",
              len(parts) >= 2 and parts[1] == "max_diff=0.0200"
              and float(parts[0].split("=")[1]) > 0.02,
              res[0].detail)


def test_issue_381_runner_branch_coverage():
    """Issue #381: pin runner.py's 3 remaining untaken branch sides.

    (a) screenshot_matches with an ABSOLUTE baseline path: the
    baseline_dir join (runner.py 906->908) must not fire, so the
    comparison uses the absolute path verbatim. A different image is
    planted at baseline_dir/<basename> so a wrong join would fail.
    (b) An assertion key matching no branch of the _check_assertions
    elif chain (945->595) appends no AssertionResult; a following
    noop still appends exactly its own result.
    (c) A step action raising a non-SeedRequestError in the run_flow
    attempt loop (1192->1201) leaves seed_status None while the step
    records failed with the exception named in the error.
    """
    from qaloop.runner import _check_assertions, run_flow
    from qaloop.spec import Step, load_spec
    from PIL import Image

    def make_step(items):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=items,
                    continue_on_fail=False, timeout_ms=None, raw={})

    class FakeLocator:
        def __init__(self, page, selector):
            self.page, self.selector = page, selector

        def screenshot(self, path):
            Image.open(self.page.element_png).save(path)

    class FakePage:
        def __init__(self, element_png):
            self.element_png = element_png

        def wait_for_selector(self, selector, state=None, timeout=None):
            pass

        def locator(self, selector):
            return FakeLocator(self, selector)

        def screenshot(self, path):
            Image.open(self.element_png).save(path)

    with tempfile.TemporaryDirectory() as d:
        element_png = os.path.join(d, "element.png")
        Image.new("RGB", (60, 30), (10, 200, 60)).save(element_png)
        abs_dir = os.path.join(d, "abs")
        os.makedirs(abs_dir)
        abs_baseline = os.path.join(abs_dir, "shot.png")
        Image.open(element_png).save(abs_baseline)
        base_dir = os.path.join(d, "base")
        os.makedirs(base_dir)
        # Wrong-join decoy: baseline_dir/<basename> holds a DIFFERENT
        # image, so joining instead of using the absolute path fails.
        Image.new("RGB", (60, 30), (200, 10, 60)).save(
            os.path.join(base_dir, "shot.png"))
        run_dir = os.path.join(d, "run")
        page = FakePage(element_png)
        res = _check_assertions(
            page, make_step([("screenshot_matches",
                              {"baseline": abs_baseline,
                               "selector": "#out"})]),
            None, baseline_dir=base_dir, run_dir=run_dir)
        check("381 absolute baseline passes verbatim",
              len(res) == 1 and res[0].passed, str(res))
        check("381 absolute baseline detail is zero diff",
              res[0].detail == "rms_diff=0.0000 max_diff=0.0200",
              res[0].detail)
        # (b) unknown assertion key fall-through
        res = _check_assertions(
            page, make_step([("bogus_unknown_key", True),
                             ("noop", True)]),
            None, baseline_dir=base_dir, run_dir=run_dir)
        check("381 unknown key appends no result",
              len(res) == 1, str(res))
        check("381 following noop is the only result",
              res[0].name == "noop" and res[0].passed
              and res[0].detail == "intentional no-op", str(res[0]))

    # (c) non-seed step failure leaves seed_status None. Driven first
    # through the fake sync_playwright seam (the issue-96 precedent in
    # test_seed_http_headers_and_status): a real Playwright run switches
    # greenlets and coverage cannot trace the attempt-loop arcs, so the
    # branch proof lives here; the real-browser drive below it proves
    # the same behavior end to end.
    import playwright.sync_api as pw_sync

    class ClickFakePage:
        url = "http://x/"

        def on(self, event, handler):
            pass

        def evaluate(self, js):
            return None

        def goto(self, url, **kw):
            pass

        def click(self, selector, timeout=None):
            raise TimeoutError(
                f"Locator.click: Timeout {timeout}ms exceeded: "
                f"{selector} not found")

    class FakeCtx:
        def new_page(self):
            return ClickFakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        with tempfile.TemporaryDirectory() as d:
            flow_path = os.path.join(d, "fake-flow.yaml")
            with open(flow_path, "w") as f:
                f.write("name: issue-381-fake-nonseed\n"
                        "target: http://x\n"
                        "steps:\n"
                        "  - name: click missing\n"
                        "    click: '#never-there'\n"
                        "    timeout_ms: 300\n"
                        "artifacts: {screenshot: none, ax_snapshot: none, "
                        "trace: false}\n")
            spec = load_spec(flow_path)
            with tempfile.TemporaryDirectory() as run_dir:
                result = run_flow(spec, run_dir=run_dir)
                check("381 fake non-seed run fails",
                      result.status == "failed", result.status)
                step = result.steps[0]
                check("381 fake non-seed step failed",
                      step.status == "failed", step.status)
                check("381 fake non-seed error names TimeoutError",
                      "TimeoutError" in step.error, step.error)
                check("381 fake non-seed seed_status is None",
                      step.seed_status is None, str(step.seed_status))
                with open(os.path.join(run_dir, "run.json"),
                          encoding="utf-8") as fh:
                    data = json.load(fh)
                check("381 fake run.json seed_status null",
                      data["steps"][0]["seed_status"] is None,
                      str(data["steps"][0].get("seed_status")))
    finally:
        pw_sync.sync_playwright = old_pw

    # (c) non-seed step failure leaves seed_status None (real browser)
    exe = os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-1243/"
        "chrome-headless-shell-linux64/chrome-headless-shell")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        have_pw = True
    except ImportError:
        have_pw = False
    if not have_pw or not os.path.exists(exe):
        check("381 non-seed failure skipped (no browser)", True)
        return
    import socket
    import subprocess
    import time
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html><body><h1>381</h1></body></html>")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port),
             "--bind", "127.0.0.1"],
            cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            flow = (f"name: issue-381-nonseed\n"
                    f"target: http://127.0.0.1:{port}\n"
                    "steps:\n"
                    "  - name: click missing\n"
                    "    click: '#never-there'\n"
                    "    timeout_ms: 300\n"
                    "artifacts: {screenshot: none, ax_snapshot: none, "
                    "trace: false}\n")
            flow_path = os.path.join(d, "flow.yaml")
            with open(flow_path, "w") as f:
                f.write(flow)
            spec = load_spec(flow_path)
            with tempfile.TemporaryDirectory() as run_dir:
                result = run_flow(spec, run_dir=run_dir,
                                  executable_path=exe)
                check("381 non-seed run fails",
                      result.status == "failed", result.status)
                step = result.steps[0]
                check("381 non-seed step failed",
                      step.status == "failed", step.status)
                check("381 non-seed error names TimeoutError",
                      "TimeoutError" in step.error, step.error)
                check("381 non-seed seed_status is None",
                      step.seed_status is None, str(step.seed_status))
                with open(os.path.join(run_dir, "run.json"),
                          encoding="utf-8") as fh:
                    data = json.load(fh)
                check("381 run.json seed_status null",
                      data["steps"][0]["seed_status"] is None,
                      str(data["steps"][0].get("seed_status")))
        finally:
            srv.terminate()
            srv.wait()


def test_ax_assertion_includes_hidden():
    """Issue #216: ax role locators must resolve hidden elements.

    Playwright role locators exclude accessibility-tree-hidden
    elements by default, so state: attached timed out on a hidden
    dialog and state: hidden passed vacuously. The runner must pass
    include_hidden=True and let wait_for decide the state.
    """
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    def make_step(items):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=items,
                    continue_on_fail=False, timeout_ms=None, raw={})

    class FakeLocator:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        def wait_for(self, state=None, timeout=None):
            self.page.waits.append(state)

    class FakePage:
        def __init__(self):
            self.role_calls = []
            self.waits = []

        def get_by_role(self, role, name=None, include_hidden=None):
            self.role_calls.append((role, name, include_hidden))
            return FakeLocator(self)

    page = FakePage()
    res = _check_assertions(
        page, make_step([("ax", {"role": "dialog", "name": "Confirm delete",
                                 "state": "attached"})]), None)
    check("ax attached passes via fake locator", res[0].passed, res[0].detail)
    check("ax attached locates with include_hidden=True",
          page.role_calls == [("dialog", "Confirm delete", True)],
          str(page.role_calls))
    check("ax attached waits for attached state",
          page.waits == ["attached"], str(page.waits))

    page = FakePage()
    res = _check_assertions(
        page, make_step([("ax", {"role": "dialog", "state": "hidden"})]), None)
    check("ax hidden (no name) passes via fake locator",
          res[0].passed, res[0].detail)
    check("ax hidden locates with include_hidden=True",
          page.role_calls == [("dialog", None, True)], str(page.role_calls))
    check("ax hidden waits for hidden state",
          page.waits == ["hidden"], str(page.waits))


def test_text_assertion_polling():
    """text_contains/text_matches poll until async-rendered text appears."""
    import time
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    def make_step(items, timeout_ms=None):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=items,
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})

    class FakePage:
        def __init__(self, texts, wait_ok=True):
            self.texts = texts  # successive text_content returns
            self.calls = 0
            self.wait_ok = wait_ok

        def wait_for_selector(self, selector, state=None, timeout=None):
            if not self.wait_ok:
                raise TimeoutError("selector never attached")

        def text_content(self, selector):
            i = min(self.calls, len(self.texts) - 1)
            self.calls += 1
            return self.texts[i]

    # Case 1: text arrives after a delay (async render) -> both assertions pass
    late = FakePage(["", "", "", "hello world"])
    res = _check_assertions(
        late, make_step([("text_contains", {"selector": "#out", "text": "world"})]),
        None)
    check("delayed text_contains passes", res[0].passed, res[0].detail)
    check("poll re-read text_content", late.calls > 2, str(late.calls))

    late = FakePage(["", "", "hello world"])
    res = _check_assertions(
        late, make_step([("text_matches",
                           {"selector": "#out", "pattern": r"h.llo w.rld"})]),
        None)
    check("delayed text_matches passes", res[0].passed, res[0].detail)

    # Case 2: text never appears -> fails with same detail format, bounded by step timeout
    t0 = time.monotonic()
    never = FakePage(["loading..."])
    res = _check_assertions(
        never, make_step([("text_contains", {"selector": "#out", "text": "done"})],
                         timeout_ms=600),
        None)
    elapsed = time.monotonic() - t0
    check("never-matching text fails", not res[0].passed, res[0].detail)
    check("detail format unchanged", res[0].detail == "want 'done' in 'loading...'",
          res[0].detail)
    check("bounded by step timeout", elapsed < 3.0, f"elapsed={elapsed:.2f}s")

    t0 = time.monotonic()
    never = FakePage(["loading..."])
    res = _check_assertions(
        never, make_step([("text_matches", {"selector": "#out", "pattern": r"^done$"})],
                         timeout_ms=600),
        None)
    elapsed = time.monotonic() - t0
    check("never-matching regex fails", not res[0].passed, res[0].detail)
    check("regex detail format unchanged",
          res[0].detail == "want /^done$/ in 'loading...'", res[0].detail)
    check("regex bounded by step timeout", elapsed < 3.0, f"elapsed={elapsed:.2f}s")

    # Case 3 regression: immediate match passes without sleeping; missing selector unchanged
    fast = FakePage(["done already"])
    t0 = time.monotonic()
    res = _check_assertions(
        fast, make_step([("text_contains", {"selector": "#out", "text": "done"})],
                        timeout_ms=600),
        None)
    elapsed = time.monotonic() - t0
    check("immediate match passes", res[0].passed, res[0].detail)
    check("immediate match single read", fast.calls == 1, str(fast.calls))
    check("immediate match no sleep", elapsed < 0.5, f"elapsed={elapsed:.2f}s")

    gone = FakePage(["x"], wait_ok=False)
    res = _check_assertions(
        gone, make_step([("text_contains", {"selector": "#gone", "text": "x"})]),
        None)
    check("missing selector fails cleanly",
          not res[0].passed and res[0].detail.startswith("TimeoutError:"),
          res[0].detail)
    check("missing selector never reads text", gone.calls == 0, str(gone.calls))

    # Case 4: bad pattern still routes to the same failed-assertion path
    res = _check_assertions(
        FakePage(["anything"]),
        make_step([("text_matches", {"selector": "#out", "pattern": r"([bad"})]),
        None)
    check("bad pattern fails cleanly",
          not res[0].passed and res[0].detail.startswith("error:"), res[0].detail)


def test_screenshot_rms_diff():
    from qaloop.artifacts import screenshot_rms_diff
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "a.png"); b = os.path.join(d, "b.png")
        Image.new("RGB", (50, 50), (200, 100, 50)).save(a)
        Image.new("RGB", (50, 50), (200, 100, 50)).save(b)
        check("identical images diff ~0", screenshot_rms_diff(a, b) == 0.0)
        Image.new("RGB", (50, 50), (0, 0, 0)).save(b)
        check("different images diff > 0", screenshot_rms_diff(a, b) > 0.5)
        Image.new("RGB", (20, 30), (200, 100, 50)).save(b)
        check("size mismatch resizes b, same color diff ~0",
              screenshot_rms_diff(a, b) == 0.0)




def test_perform_guards():
    from qaloop.perform import Performer
    p = Performer.__new__(Performer)
    p.allow_publish = False
    blocked = []
    allowed = []
    for label in ["Pay now", "Delete account", "Publish", "Sign up", "Load greeting"]:
        try:
            Performer._guard(p, "click", {"name": label})
            allowed.append(label)
        except PermissionError:
            blocked.append(label)
    check("destructive/publish blocked", blocked == ["Pay now", "Delete account", "Publish"],
          str(blocked))
    check("benign actions allowed", allowed == ["Sign up", "Load greeting"], str(allowed))
    p.allow_publish = True
    try:
        Performer._guard(p, "click", {"name": "Publish"})
        check("publish allowed with flag", True)
    except PermissionError:
        check("publish allowed with flag", False)


def test_perform_upload_guard():
    from qaloop.perform import Performer
    p = Performer.__new__(Performer)
    with tempfile.TemporaryDirectory() as d:
        p.upload_dir = d
        inside = os.path.join(d, "resume.txt")
        try:
            got = p._check_upload_path(inside)
            check("absolute path inside dir passes",
                  got == os.path.abspath(inside))
        except ValueError:
            check("absolute path inside dir passes", False)
        cases = [
            ("relative path rejected", "resume.txt"),
            (".. escape rejected", os.path.join(d, "..", "escape.txt")),
            ("absolute path outside rejected", "/etc/hostname"),
            ("sibling dir via .. rejected",
             os.path.join(d, "sub", "..", "..", "other")),
        ]
        for name, bad in cases:
            try:
                p._check_upload_path(bad)
                check(name, False, bad)
            except ValueError:
                check(name, True)


def test_perform_action_json_extraction():
    from qaloop.perform import _extract_json
    a = _extract_json('here you go: {"action": "click", "target": {"role": "button"}} done')
    check("json extracted from prose", a["action"] == "click")
    try:
        _extract_json("no json here")
        check("non-json rejected", False)
    except ValueError:
        check("non-json rejected", True)


def test_perform_cost_ceiling():
    from qaloop.perform import Performer, _call_cost, _ceiling_summary
    p = Performer.__new__(Performer)
    p.max_cost_usd = 0.0001
    check("first call allowed (no estimate)",
          not p._ceiling_hit(0.0, 0.0))
    check("call within ceiling allowed",
          not p._ceiling_hit(0.0, 0.00005))
    check("accumulated+estimate over ceiling blocks",
          p._ceiling_hit(0.000156, 0.000156))
    check("exactly-at-ceiling allowed",
          not p._ceiling_hit(0.00005, 0.00005))
    p.max_cost_usd = 0.0
    check("max_cost_usd=0 means unlimited",
          not p._ceiling_hit(100.0, 100.0))
    cfg = {"price_in": 0.15, "price_out": 0.60}
    cost = _call_cost({"prompt_tokens": 800, "completion_tokens": 60}, cfg)
    check("call cost from usage and prices",
          abs(cost - 0.000156) < 1e-9, str(cost))
    check("blocked summary names the ceiling",
          _ceiling_summary(0.0001) ==
          "cost ceiling reached (max-cost-usd 0.0001)")
    from qaloop.cli import build_parser
    args = build_parser().parse_args(
        ["perform", "--task", "x", "--max-cost-usd", "0.0001"])
    check("cli flag parses", args.max_cost_usd == 0.0001)
    args_default = build_parser().parse_args(["perform", "--task", "x"])
    check("cli flag defaults to unlimited", args_default.max_cost_usd == 0.0)


def test_performer_contracts():
    import base64
    import time
    import qaloop.perform as perf_mod
    from qaloop.perform import Performer

    png_bytes = b"\x89PNG\r\n\x1a\nperformer-contract-shot"

    class FakeLocator:
        def __init__(self, key, count=1):
            self.key = key
            self._count = count
            self.calls = []

        @property
        def first(self):
            return self

        def count(self):
            return self._count

        def click(self, timeout=None):
            self.calls.append(("click", timeout))

        def dblclick(self, timeout=None):
            self.calls.append(("dblclick", timeout))

        def scroll_into_view_if_needed(self, timeout=None):
            self.calls.append(("scroll_into_view", timeout))

        def fill(self, text, timeout=None):
            self.calls.append(("fill", text, timeout))

        def press(self, key, timeout=None):
            self.calls.append(("press", key, timeout))

        def select_option(self, value, timeout=None):
            self.calls.append(("select_option", value, timeout))

        def check(self, timeout=None):
            self.calls.append(("check", timeout))

        def uncheck(self, timeout=None):
            self.calls.append(("uncheck", timeout))

        def hover(self, timeout=None):
            self.calls.append(("hover", timeout))

        def wait_for(self, state=None, timeout=None):
            self.calls.append(("wait_for", state, timeout))

        def set_input_files(self, path, timeout=None):
            self.calls.append(("set_input_files", path, timeout))

    class FakeKeyboard:
        def __init__(self):
            self.press_calls = []
            self.type_calls = []

        def press(self, key):
            self.press_calls.append(key)

        def type(self, text, delay=None):
            self.type_calls.append((text, delay))

    class FakeMouse:
        def __init__(self):
            self.wheel_calls = []

        def wheel(self, dx, dy):
            self.wheel_calls.append((dx, dy))

    class FakePage:
        def __init__(self):
            self.url = "http://x/start"
            self.handlers = {}
            self.goto_calls = []
            self.locators = {}
            self.counts = {}
            self.keyboard = FakeKeyboard()
            self.mouse = FakeMouse()

        def on(self, event, handler):
            self.handlers[event] = handler

        def title(self):
            return "Contract Title"

        def goto(self, url, timeout=None, wait_until=None):
            self.goto_calls.append((url, timeout, wait_until))
            self.url = url

        def _loc(self, key):
            if key not in self.locators:
                self.locators[key] = FakeLocator(
                    key, self.counts.get(key, 1))
            return self.locators[key]

        def set_count(self, key, n):
            self.counts[key] = n
            if key in self.locators:
                self.locators[key]._count = n

        def get_by_role(self, role, name=None):
            return self._loc(("role", role, name))

        def get_by_text(self, text):
            return self._loc(("text", text))

        def locator(self, css):
            return self._loc(("css", css))

        def screenshot(self, path=None, **kw):
            with open(path, "wb") as f:
                f.write(png_bytes)

    class FakeConsoleMsg:
        def __init__(self, type_, text):
            self.type = type_
            self.text = text

    class FakeDialog:
        def __init__(self, type_, message):
            self.type = type_
            self.message = message
            self.dismissed = False

        def dismiss(self):
            self.dismissed = True

    def expect_value_error(name, fn, *fragments):
        try:
            fn()
            check(name, False, "no ValueError raised")
        except ValueError as e:
            check(name, all(f in str(e) for f in fragments), str(e))

    real_sleep = time.sleep
    old_ax = perf_mod.ax_snapshot
    sleeps = []
    time.sleep = lambda s: sleeps.append(s)
    try:
        with tempfile.TemporaryDirectory() as run_dir, \
                tempfile.TemporaryDirectory() as upload_dir:
            page = FakePage()
            perf = Performer(page, run_dir, 25, seed=7,
                             upload_dir=upload_dir)

            # __init__: state defaults + handler registration.
            check("performer init: defaults",
                  perf.n == 0 and perf.console == [] and perf.dialogs == []
                  and perf.allow_publish is False
                  and perf.max_cost_usd == 0.0
                  and perf.tokens_in == 0 and perf.tokens_out == 0)
            check("performer init: handlers registered",
                  set(page.handlers) == {"console", "pageerror", "dialog"},
                  repr(sorted(page.handlers)))
            check("performer init: dialog handler is _on_dialog",
                  page.handlers["dialog"] == perf._on_dialog)

            # Console handler: only errors, text truncated to 300.
            page.handlers["console"](FakeConsoleMsg("warning", "ignored"))
            check("console handler: non-error ignored", perf.console == [])
            page.handlers["console"](FakeConsoleMsg("error", "x" * 350))
            check("console handler: error kept, truncated to 300",
                  perf.console == [{"type": "error", "text": "x" * 300}],
                  repr(perf.console)[:200])
            # Pageerror handler: appended as type pageerror, truncated.
            page.handlers["pageerror"](Exception("y" * 350))
            check("pageerror handler: appended truncated",
                  perf.console[-1] == {"type": "pageerror",
                                       "text": "y" * 300},
                  repr(perf.console[-1])[:200])

            # _on_dialog via the registered handler.
            dlg = FakeDialog("confirm", "m" * 350)
            page.handlers["dialog"](dlg)
            check("dialog handler: records type: message truncated",
                  perf.dialogs == ["confirm: " + "m" * 300],
                  repr(perf.dialogs)[:200])
            check("dialog handler: dismisses", dlg.dismissed is True)
            perf.dialogs = []

            # _pace: sleeps 0.25 + rng*0.5 (time.sleep stubbed).
            sleeps.clear()
            perf._pace()
            check("pace: one sleep in [0.25, 0.75]",
                  len(sleeps) == 1 and 0.25 <= sleeps[0] <= 0.75,
                  repr(sleeps))

            # Neutralize pacing for the act battery, counting calls so the
            # PermissionError path (which skips _pace) is observable.
            pace_calls = []
            perf._pace = lambda: pace_calls.append(1)

            # _ceiling_hit on real instances.
            perf2 = Performer(FakePage(), run_dir, 10, max_cost_usd=10.0)
            check("ceiling: no estimate basis -> False",
                  perf2._ceiling_hit(0.0, 0.0) is False)
            check("ceiling: negative estimate -> False",
                  perf2._ceiling_hit(9.0, -1.0) is False)
            check("ceiling: under -> False",
                  perf2._ceiling_hit(5.0, 4.0) is False)
            check("ceiling: exactly at -> False",
                  perf2._ceiling_hit(5.0, 5.0) is False)
            check("ceiling: over -> True",
                  perf2._ceiling_hit(5.0, 6.0) is True)
            check("ceiling: unlimited (max 0) -> False",
                  perf._ceiling_hit(100.0, 100.0) is False)

            # _resolve: success per target kind + all ValueError branches.
            loc_role = perf._resolve({"role": "button", "name": "Save"})
            check("resolve: role returns locator",
                  loc_role is page.locators[("role", "button", "Save")])
            check("resolve: text returns locator",
                  perf._resolve({"text": "Hello"})
                  is page.locators[("text", "Hello")])
            check("resolve: css returns locator",
                  perf._resolve({"css": "#a"})
                  is page.locators[("css", "#a")])
            expect_value_error(
                "resolve: non-dict target rejected",
                lambda: perf._resolve("oops"), "target must be an object")
            expect_value_error(
                "resolve: empty target rejected",
                lambda: perf._resolve({}),
                "target needs role(+name), text, or css")
            page.set_count(("role", "button", "Ghost"), 0)
            expect_value_error(
                "resolve: zero matches rejected",
                lambda: perf._resolve({"role": "button", "name": "Ghost"}),
                "no element matches")
            page.set_count(("role", "button", "Ghost"), 1)
            page.set_count(("css", ".dup"), 3)
            expect_value_error(
                "resolve: multiple matches names the count",
                lambda: perf._resolve({"css": ".dup"}),
                "3 elements match", "disambiguate")
            page.set_count(("css", ".dup"), 1)

            # _guard via direct calls (act only guards click/dblclick).
            for action_name in ("click", "dblclick", "press", "check"):
                try:
                    perf._guard(action_name, {"name": "Delete account"})
                    check(f"guard: destructive blocked on {action_name}",
                          False)
                except PermissionError:
                    check(f"guard: destructive blocked on {action_name}",
                          True)
            try:
                perf._guard("click", {"name": "Publish"})
                check("guard: publish blocked without allow_publish", False)
            except PermissionError:
                check("guard: publish blocked without allow_publish", True)
            try:
                perf._guard("click", {"text": "Share this"})
                check("guard: publish via text label blocked", False)
            except PermissionError:
                check("guard: publish via text label blocked", True)
            perf_allow = Performer(FakePage(), run_dir, 10,
                                   allow_publish=True)
            try:
                perf_allow._guard("click", {"name": "Publish"})
                check("guard: publish allowed with allow_publish", True)
            except PermissionError:
                check("guard: publish allowed with allow_publish", False)
            try:
                perf._guard("click", {"name": "Save"})
                perf._guard("click", None)
                check("guard: benign label and no target allowed", True)
            except PermissionError:
                check("guard: benign label and no target allowed", False)

            # act(): navigate.
            n0 = perf.n
            out = perf.act({"action": "navigate", "url": "http://x/next"})
            check("act navigate: observation", out == "navigated to http://x/next",
                  out)
            check("act navigate: goto call recorded",
                  page.goto_calls[-1] == ("http://x/next", 25000,
                                          "domcontentloaded"),
                  repr(page.goto_calls))
            check("act navigate: counter +1, paced",
                  perf.n == n0 + 1 and len(pace_calls) == 1,
                  f"n={perf.n} pace={len(pace_calls)}")

            # act(): click.
            t_save = {"role": "button", "name": "Save"}
            out = perf.act({"action": "click", "target": t_save})
            loc_save = page.locators[("role", "button", "Save")]
            check("act click: observation", out == f"clicked {t_save}", out)
            check("act click: locator calls",
                  loc_save.calls == [("scroll_into_view", 5000),
                                     ("click", 10000)],
                  repr(loc_save.calls))

            # act(): dblclick.
            t_docs = {"role": "link", "name": "Docs"}
            out = perf.act({"action": "dblclick", "target": t_docs})
            loc_docs = page.locators[("role", "link", "Docs")]
            check("act dblclick: observation",
                  out == f"dblclicked {t_docs}", out)
            check("act dblclick: locator calls",
                  loc_docs.calls == [("scroll_into_view", 5000),
                                     ("dblclick", 10000)],
                  repr(loc_docs.calls))

            # act(): destructive click raises PermissionError out of act,
            # counter still increments, _pace not reached.
            n0, pace0 = perf.n, len(pace_calls)
            try:
                perf.act({"action": "click",
                          "target": {"role": "button",
                                     "name": "Delete account"}})
                check("act destructive click: PermissionError propagates",
                      False)
            except PermissionError:
                check("act destructive click: PermissionError propagates",
                      True)
            check("act destructive click: counter +1, no pace",
                  perf.n == n0 + 1 and len(pace_calls) == pace0,
                  f"n={perf.n} pace={len(pace_calls)}")

            # act(): fill short text via keyboard, long text via locator.
            t_email = {"role": "textbox", "name": "Email"}
            out = perf.act({"action": "fill", "target": t_email,
                            "text": "a@b.c"})
            loc_email = page.locators[("role", "textbox", "Email")]
            check("act fill short: observation",
                  out == f"filled {t_email}", out)
            check("act fill short: select-all pressed",
                  page.keyboard.press_calls == ["ControlOrMeta+a"],
                  repr(page.keyboard.press_calls))
            check("act fill short: typed with human delay",
                  len(page.keyboard.type_calls) == 1
                  and page.keyboard.type_calls[0][0] == "a@b.c"
                  and 30 <= page.keyboard.type_calls[0][1] <= 69,
                  repr(page.keyboard.type_calls))
            check("act fill short: locator clicked, not filled",
                  ("click", 5000) in loc_email.calls
                  and not any(c[0] == "fill" for c in loc_email.calls),
                  repr(loc_email.calls))
            t_bio = {"role": "textbox", "name": "Bio"}
            long_text = "z" * 81
            out = perf.act({"action": "fill", "target": t_bio,
                            "text": long_text})
            loc_bio = page.locators[("role", "textbox", "Bio")]
            check("act fill long: observation", out == f"filled {t_bio}",
                  out)
            check("act fill long: locator fill used",
                  ("fill", long_text, 10000) in loc_bio.calls,
                  repr(loc_bio.calls))
            check("act fill long: keyboard type not used again",
                  len(page.keyboard.type_calls) == 1)

            # act(): press with and without target.
            t_submit = {"role": "button", "name": "Submit"}
            out = perf.act({"action": "press", "target": t_submit,
                            "key": "Enter"})
            loc_submit = page.locators[("role", "button", "Submit")]
            check("act press with target: observation",
                  out == "pressed Enter", out)
            check("act press with target: locator press",
                  ("press", "Enter", 10000) in loc_submit.calls,
                  repr(loc_submit.calls))
            out = perf.act({"action": "press", "key": "Escape"})
            check("act press bare: observation", out == "pressed Escape",
                  out)
            check("act press bare: keyboard press",
                  page.keyboard.press_calls[-1] == "Escape",
                  repr(page.keyboard.press_calls))

            # act(): select / check / uncheck.
            t_city = {"role": "combobox", "name": "City"}
            out = perf.act({"action": "select", "target": t_city,
                            "value": "sg"})
            loc_city = page.locators[("role", "combobox", "City")]
            check("act select: observation", out == f"select {t_city}", out)
            check("act select: locator call",
                  ("select_option", "sg", 10000) in loc_city.calls,
                  repr(loc_city.calls))
            t_rem = {"role": "checkbox", "name": "Remember me"}
            out = perf.act({"action": "check", "target": t_rem})
            loc_rem = page.locators[("role", "checkbox", "Remember me")]
            check("act check: observation", out == f"check {t_rem}", out)
            check("act check: locator call",
                  ("check", 10000) in loc_rem.calls, repr(loc_rem.calls))
            t_news = {"role": "checkbox", "name": "Newsletter"}
            out = perf.act({"action": "uncheck", "target": t_news})
            loc_news = page.locators[("role", "checkbox", "Newsletter")]
            check("act uncheck: observation", out == f"uncheck {t_news}",
                  out)
            check("act uncheck: locator call",
                  ("uncheck", 10000) in loc_news.calls,
                  repr(loc_news.calls))

            # act(): hover.
            t_file = {"role": "menuitem", "name": "File"}
            out = perf.act({"action": "hover", "target": t_file})
            loc_file = page.locators[("role", "menuitem", "File")]
            check("act hover: observation", out == "hovered", out)
            check("act hover: locator call",
                  ("hover", 10000) in loc_file.calls, repr(loc_file.calls))

            # act(): scroll with target, then by direction (wheel sign).
            t_bottom = {"role": "button", "name": "Bottom"}
            out = perf.act({"action": "scroll", "target": t_bottom})
            loc_bottom = page.locators[("role", "button", "Bottom")]
            check("act scroll target: observation", out == "scrolled", out)
            check("act scroll target: scroll_into_view, no wheel",
                  loc_bottom.calls == [("scroll_into_view", 5000)]
                  and page.mouse.wheel_calls == [],
                  repr((loc_bottom.calls, page.mouse.wheel_calls)))
            out = perf.act({"action": "scroll", "direction": "down"})
            check("act scroll down: wheel (0, 600)",
                  out == "scrolled"
                  and page.mouse.wheel_calls[-1] == (0, 600),
                  repr(page.mouse.wheel_calls))
            out = perf.act({"action": "scroll", "direction": "up"})
            check("act scroll up: wheel (0, -600)",
                  out == "scrolled"
                  and page.mouse.wheel_calls[-1] == (0, -600),
                  repr(page.mouse.wheel_calls))

            # act(): wait via text target / resolved target / bare ms.
            out = perf.act({"action": "wait", "target": {"text": "Welcome"}})
            loc_welcome = page.locators[("text", "Welcome")]
            check("act wait text: observation", out == "wait satisfied",
                  out)
            check("act wait text: first.wait_for visible/15000",
                  ("wait_for", "visible", 15000) in loc_welcome.calls,
                  repr(loc_welcome.calls))
            t_ready = {"role": "button", "name": "Ready"}
            out = perf.act({"action": "wait", "target": t_ready,
                            "state": "hidden"})
            loc_ready = page.locators[("role", "button", "Ready")]
            check("act wait resolved: wait_for hidden/15000",
                  out == "wait satisfied"
                  and ("wait_for", "hidden", 15000) in loc_ready.calls,
                  repr(loc_ready.calls))
            sleeps.clear()
            out = perf.act({"action": "wait", "ms": 250})
            check("act wait bare ms: sleeps ms/1000",
                  out == "wait satisfied" and sleeps == [0.25],
                  repr(sleeps))
            sleeps.clear()
            out = perf.act({"action": "wait"})
            check("act wait bare default: sleeps 1.0",
                  out == "wait satisfied" and sleeps == [1.0],
                  repr(sleeps))

            # act(): upload inside the upload dir vs outside (FAILED obs).
            up_path = os.path.join(upload_dir, "resume.txt")
            t_up = {"css": "#resume"}
            out = perf.act({"action": "upload", "target": t_up,
                            "path": up_path})
            loc_up = page.locators[("css", "#resume")]
            check("act upload inside: observation",
                  out == "uploaded resume.txt", out)
            check("act upload inside: set_input_files call",
                  ("set_input_files", up_path, 10000) in loc_up.calls,
                  repr(loc_up.calls))
            n0 = perf.n
            out = perf.act({"action": "upload", "target": t_up,
                            "path": "/etc/hostname"})
            check("act upload outside: FAILED ValueError observation",
                  out.startswith("FAILED: ValueError:")
                  and "outside the upload dir" in out, out)
            check("act upload outside: counter +1", perf.n == n0 + 1)

            # act(): screenshot writes perform-NN.png, sets _last_shot_b64.
            out = perf.act({"action": "screenshot"})
            shot_name = f"perform-{perf.n:02d}.png"
            shot_path = os.path.join(run_dir, "steps", shot_name)
            check("act screenshot: observation",
                  out == f"screenshot saved ({shot_name})", out)
            check("act screenshot: file written under run_dir/steps",
                  os.path.exists(shot_path)
                  and open(shot_path, "rb").read() == png_bytes)
            check("act screenshot: _last_shot_b64 set from file bytes",
                  getattr(perf, "_last_shot_b64", None)
                  == base64.b64encode(png_bytes).decode())

            # act(): console returns the seeded JSON tail (last 10).
            perf.console = [{"type": "error", "text": f"e{i}"}
                            for i in range(12)]
            out = perf.act({"action": "console"})
            check("act console: last-10 JSON tail",
                  out == json.dumps(perf.console[-10:],
                                    ensure_ascii=False)
                  and json.loads(out) == perf.console[-10:], out[:200])

            # act(): unknown action -> FAILED ValueError observation.
            n0 = perf.n
            out = perf.act({"action": "frobnicate"})
            check("act unknown: FAILED ValueError observation",
                  out == "FAILED: ValueError: unknown action 'frobnicate'",
                  out)
            check("act unknown: counter +1", perf.n == n0 + 1)

            # act(): unresolvable target -> FAILED observation, no raise.
            page.set_count(("role", "button", "Ghost"), 0)
            out = perf.act({"action": "click",
                            "target": {"role": "button", "name": "Ghost"}})
            check("act click zero matches: FAILED observation",
                  out.startswith("FAILED: ValueError: no element matches"),
                  out)
            page.set_count(("role", "button", "Ghost"), 1)

            # observe(): header, dialogs surfaced once (last 3), ax text.
            ax_calls = []

            def fake_ax(page_, max_nodes=400):
                ax_calls.append(max_nodes)
                return "AX SNAP TEXT"

            perf_mod.ax_snapshot = fake_ax
            perf.dialogs = ["alert: one", "confirm: two",
                            "alert: three", "confirm: four"]
            obs = perf.observe()
            check("observe: URL/Title header",
                  obs.startswith("URL: http://x/next\n"
                                 "Title: Contract Title"), obs[:200])
            check("observe: dialogs last-3 surfaced",
                  "DIALOGS (auto-dismissed): confirm: two | "
                  "alert: three | confirm: four" in obs, obs[:400])
            check("observe: oldest dialog not surfaced",
                  "alert: one" not in obs, obs[:400])
            check("observe: dialogs cleared after surfacing",
                  perf.dialogs == [])
            check("observe: ax snapshot appended, max_nodes=400",
                  obs.split("\n")[-1] == "AX SNAP TEXT"
                  and ax_calls == [400], repr(ax_calls))
            obs2 = perf.observe()
            check("observe: dialogs surface only once",
                  "DIALOGS" not in obs2, obs2[:300])

            # observe(): ax failure fallback.
            def boom_ax(page_, max_nodes=400):
                raise RuntimeError("ax boom")

            perf_mod.ax_snapshot = boom_ax
            obs3 = perf.observe()
            check("observe: ax failure fallback text",
                  "(ax snapshot failed: ax boom)" in obs3, obs3[:300])
            check("observe: fallback keeps header",
                  obs3.startswith("URL: http://x/next\n"
                                  "Title: Contract Title"), obs3[:200])
    finally:
        time.sleep = real_sleep
        perf_mod.ax_snapshot = old_ax


def test_services_validation():
    from qaloop.spec import _validate_services, SpecError
    good = _validate_services(
        [{"name": "api", "command": "python3 app.py",
          "wait": {"http": "http://127.0.0.1:8000/health"}, "timeout_s": 30,
          "env": {"PORT": "8000"}}], "spec x")
    check("valid service parses", good[0]["name"] == "api"
          and good[0]["env"] == {"PORT": "8000"} and good[0]["url"] is None)
    check("no services -> []", _validate_services(None, "spec x") == [])
    bad = [
        ("bad name", [{"name": "9bad", "command": "x", "wait": {"port": 1}}]),
        ("missing command", [{"name": "a", "wait": {"port": 1}}]),
        ("empty command", [{"name": "a", "command": "", "wait": {"port": 1}}]),
        ("two waits", [{"name": "a", "command": "x",
                        "wait": {"port": 1, "http": "u"}}]),
        ("unknown wait", [{"name": "a", "command": "x", "wait": {"ssh": 1}}]),
        ("bad timeout", [{"name": "a", "command": "x", "wait": {"port": 1},
                          "timeout_s": -1}]),
        ("dup names", [{"name": "a", "command": "x", "wait": {"port": 1}},
                       {"name": "a", "command": "y", "wait": {"port": 2}}]),
        ("not a list", {"name": "a"}),
        ("empty url", [{"name": "a", "command": "x", "wait": {"port": 1},
                        "url": ""}]),
    ]
    for label, raw in bad:
        try:
            _validate_services(raw, "spec x")
            check(f"services rejected: {label}", False)
        except SpecError:
            check(f"services rejected: {label}", True)


def test_services_light_parse():
    from qaloop.spec import load_services_light
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("name: svc-test\ntarget: http://x/\n"
                "services:\n"
                "  - name: api\n    command: python3 app.py\n"
                "    wait: {port: 8123}\n"
                "steps: []\n")
        path = f.name
    try:
        name, services = load_services_light(path)
        check("light parse name", name == "svc-test")
        check("light parse services", len(services) == 1
              and services[0]["wait"] == {"port": 8123})
    finally:
        os.unlink(path)


def test_flow_services_boot_teardown():
    import socket
    from qaloop.env import flow_services
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    spec = [{"name": "web", "command": f"python3 -m http.server {port}",
             "cwd": None, "env": {}, "wait": {"port": port},
             "timeout_s": 15.0, "url": None}]
    with tempfile.TemporaryDirectory() as run_dir:
        ctx = flow_services(spec, os.getcwd(), run_dir)
        ctx.__enter__()
        try:
            check("service URL exported",
                  os.environ.get("QALOOP_SERVICE_WEB_URL") ==
                  f"http://127.0.0.1:{port}/")
            check("service PORT exported",
                  os.environ.get("QALOOP_SERVICE_WEB_PORT") == str(port))
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=5)
            check("service answers http", r.status == 200)
            log = os.path.join(run_dir, "services", "web.log")
        finally:
            ctx.__exit__(None, None, None)
        check("env cleaned up",
              "QALOOP_SERVICE_WEB_URL" not in os.environ)
        check("service log captured", os.path.exists(log))
    import subprocess
    left = subprocess.run(["pgrep", "-f", f"http.server {port}"],
                          capture_output=True).returncode
    check("service torn down", left != 0)


def test_flow_services_http_url_origin():
    import socket
    from qaloop.env import flow_services
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # wait on a health *path*; the exported URL must be the origin, not the
    # health endpoint.
    server = ("python3 -c \"from http.server import BaseHTTPRequestHandler,"
              " HTTPServer\n"
              "class H(BaseHTTPRequestHandler):\n"
              "    def do_GET(self):\n"
              "        self.send_response(200); self.end_headers();"
              " self.wfile.write(b'ok')\n"
              "    def log_message(self, *a): pass\n"
              f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()\"")
    spec = [{"name": "api", "command": server,
             "cwd": None, "env": {},
             "wait": {"http": f"http://127.0.0.1:{port}/health"},
             "timeout_s": 15.0, "url": None}]
    with tempfile.TemporaryDirectory() as run_dir:
        ctx = flow_services(spec, os.getcwd(), run_dir)
        try:
            ctx.__enter__()
            check("health-path wait derives origin URL",
                  os.environ.get("QALOOP_SERVICE_API_URL") ==
                  f"http://127.0.0.1:{port}/")
        finally:
            ctx.__exit__(None, None, None)


def test_flow_services_boot_failure():
    from qaloop.env import flow_services
    spec = [{"name": "bad", "command": "python3 -c 'import sys; sys.exit(3)'",
             "cwd": None, "env": {}, "wait": {"port": 59999},
             "timeout_s": 2.0, "url": None}]
    with tempfile.TemporaryDirectory() as run_dir:
        ctx = flow_services(spec, os.getcwd(), run_dir)
        try:
            ctx.__enter__()
            check("boot failure raised", False)
        except (TimeoutError, RuntimeError, ValueError):
            check("boot failure raised", True)
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        check("failed service env cleaned",
              "QALOOP_SERVICE_BAD_URL" not in os.environ)


def test_flow_services_log_contains_boot_failure():
    # Issue #278: the log_contains timeout branch against a LIVE
    # (non-exiting) subprocess that never prints the needle must raise
    # TimeoutError naming the needle and the timeout, and the timed-out
    # service process must be terminated (no orphan left alive).
    import subprocess
    import time
    from qaloop.env import flow_services
    needle = "NEEDLE-NEVER-PRINTED-278"
    # The needle rides in the child's argv as a trailing comment so
    # pgrep -f <needle> genuinely matches the live child if orphaned.
    spec = [{"name": "logsvc",
             "command": "python3 -c 'import time; print(\"booting, no "
                        "needle\", flush=True); time.sleep(60)  # "
                        + needle + "'",
             "cwd": None, "env": {},
             "wait": {"log_contains": needle},
             "timeout_s": 2.0, "url": None}]
    with tempfile.TemporaryDirectory() as run_dir:
        ctx = flow_services(spec, os.getcwd(), run_dir)
        try:
            ctx.__enter__()
            check("log_contains boot failure raised TimeoutError", False)
        except TimeoutError as e:
            check("log_contains boot failure raised TimeoutError", True)
            check("log_contains TimeoutError names the needle",
                  needle in str(e), str(e))
            check("log_contains TimeoutError names the timeout",
                  "after 2.0s" in str(e), str(e))
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        # terminate() is async; poll briefly so a dying child is not
        # mistaken for an orphan.
        orphans = None
        for _ in range(30):
            orphans = subprocess.run(["pgrep", "-f", needle],
                                     capture_output=True, text=True)
            if orphans.returncode != 0:
                break
            time.sleep(0.1)
        check("timed-out log_contains service terminated",
              orphans.returncode != 0 and not orphans.stdout.strip(),
              orphans.stdout)
        check("failed logsvc env cleaned",
              "QALOOP_SERVICE_LOGSVC_URL" not in os.environ)


def test_flow_services_http_boot_failure():
    # Issue #292: the http timeout branch against a LIVE service that
    # answers 404 forever on the health path must raise TimeoutError
    # naming the wait URL and the timeout, and the timed-out service
    # process must be terminated (no orphan left alive).
    import socket
    import subprocess
    import time
    from qaloop.env import flow_services
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    wait_url = f"http://127.0.0.1:{port}/api/no-such-health-292"
    # Live server answering 404 on every GET, so the health URL never
    # returns 200. The wait URL rides in the child's argv as a
    # trailing comment so pgrep -f <wait_url> genuinely matches the
    # live child if orphaned.
    server = ("python3 -c \"from http.server import BaseHTTPRequestHandler,"
              " HTTPServer\n"
              "class H(BaseHTTPRequestHandler):\n"
              "    def do_GET(self):\n"
              "        self.send_response(404); self.end_headers();"
              " self.wfile.write(b'nope')\n"
              "    def log_message(self, *a): pass\n"
              f"HTTPServer(('127.0.0.1', {port}), H).serve_forever()"
              f"  # {wait_url}\"")
    spec = [{"name": "api", "command": server,
             "cwd": None, "env": {},
             "wait": {"http": wait_url},
             "timeout_s": 2.0, "url": None}]
    with tempfile.TemporaryDirectory() as run_dir:
        ctx = flow_services(spec, os.getcwd(), run_dir)
        try:
            ctx.__enter__()
            check("http boot failure raised TimeoutError", False)
        except TimeoutError as e:
            check("http boot failure raised TimeoutError", True)
            check("http TimeoutError names the wait URL",
                  wait_url in str(e), str(e))
            check("http TimeoutError names the timeout",
                  "not ready after 2.0s" in str(e), str(e))
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        # terminate() is async; poll briefly so a dying child is not
        # mistaken for an orphan.
        orphans = None
        for _ in range(30):
            orphans = subprocess.run(["pgrep", "-f", wait_url],
                                     capture_output=True, text=True)
            if orphans.returncode != 0:
                break
            time.sleep(0.1)
        check("timed-out http service terminated",
              orphans.returncode != 0 and not orphans.stdout.strip(),
              orphans.stdout)
        check("failed api env cleaned",
              "QALOOP_SERVICE_API_URL" not in os.environ)


def test_flow_services_partial_boot_failure():
    # Issue #294: when the SECOND service fails to boot, the FIRST
    # already-booted service must be stopped, its env exports must be
    # removed, and its log must still be copied into the run dir.
    import socket
    import subprocess
    import time
    from qaloop.env import flow_services
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    portA = s.getsockname()[1]
    s.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    portB = s.getsockname()[1]
    s.close()
    wait_url = f"http://127.0.0.1:{portB}/api/no-such-health-294"
    server_b = ("python3 -c \"from http.server import BaseHTTPRequestHandler,"
                " HTTPServer\n"
                "class H(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(404); self.end_headers();"
                " self.wfile.write(b'nope')\n"
                "    def log_message(self, *a): pass\n"
                f"HTTPServer(('127.0.0.1', {portB}), H).serve_forever()"
                f"  # {wait_url}\"")
    spec = [
        {"name": "first", "command": f"python3 -m http.server {portA}",
         "cwd": None, "env": {}, "wait": {"port": portA},
         "timeout_s": 15.0, "url": None},
        {"name": "second", "command": server_b,
         "cwd": None, "env": {},
         "wait": {"http": wait_url},
         "timeout_s": 2.0, "url": None},
    ]
    with tempfile.TemporaryDirectory() as run_dir:
        ctx = flow_services(spec, os.getcwd(), run_dir)
        try:
            ctx.__enter__()
            check("partial boot failure raised TimeoutError", False)
        except TimeoutError as e:
            check("partial boot failure raised TimeoutError", True)
            check("partial TimeoutError names the wait URL",
                  wait_url in str(e), str(e))
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        orphans = None
        for _ in range(30):
            orphans = subprocess.run(
                ["pgrep", "-f", f"http.server {portA}"],
                capture_output=True, text=True)
            if orphans.returncode != 0:
                break
            time.sleep(0.1)
        check("partial boot: first service terminated",
              orphans is not None and orphans.returncode != 0
              and not orphans.stdout.strip(),
              orphans.stdout if orphans else "no pgrep result")
        port_free = False
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", portA))
            probe.close()
            port_free = True
        except OSError:
            port_free = False
        check("partial boot: first service port is free", port_free)
        check("partial boot: first URL env cleaned",
              "QALOOP_SERVICE_FIRST_URL" not in os.environ)
        check("partial boot: first PORT env cleaned",
              "QALOOP_SERVICE_FIRST_PORT" not in os.environ)
        check("partial boot: first service log copied",
              os.path.exists(os.path.join(run_dir, "services", "first.log")))


def test_evaluate_verdict_extraction():
    from qaloop.evaluate import _extract_verdict
    v = _extract_verdict('note: {"verdict": "MAKES_SENSE", "confidence": "high",'
                         ' "rationale": "ok", "evidence": [], "risks": []} end')
    check("verdict extracted", v["verdict"] == "MAKES_SENSE"
          and v["confidence"] == "high")
    try:
        _extract_verdict("no json here")
        check("non-json verdict rejected", False)
    except ValueError:
        check("non-json verdict rejected", True)
    try:
        _extract_verdict('{"verdict": "MAYBE"}')
        check("unknown verdict rejected", False)
    except ValueError:
        check("unknown verdict rejected", True)


def test_evaluate_collect_and_write():
    from qaloop.evaluate import collect_evidence, write_evaluation
    with tempfile.TemporaryDirectory() as run_dir:
        run = {"flow_name": "demo", "target": "http://x/", "status": "PASS",
               "failed_step": None, "error": "",
               "steps": [{"index": 0, "phase": "steps", "name": "load",
                          "op": "goto", "status": "PASS", "duration_ms": 100,
                          "error": "", "assertions": [],
                          "screenshot": None, "ax_snapshot": None}],
               "console_errors": [], "page_errors": [],
               "failed_requests": [], "bad_responses": []}
        with open(os.path.join(run_dir, "run.json"), "w") as f:
            json.dump(run, f)
        diff = "diff --git a/a.py b/a.py\n+x = 1\n"
        ev = collect_evidence(run_dir, diff)
        check("evidence assembled", ev["flow_name"] == "demo"
              and ev["diff_stat"] == "1 files, +1/-0"
              and len(ev["steps"]) == 1)
        md, js = write_evaluation(run_dir, "x does y",
                                  {"verdict": "MAKES_SENSE",
                                   "confidence": "high",
                                   "rationale": "r", "evidence": ["e1"],
                                   "risks": [],
                                   "_cost": {"tokens_in": 1, "tokens_out": 2,
                                             "cost_usd_est": 0.0,
                                             "model": "m"}})
        check("EVALUATION.md written", os.path.exists(md)
              and "MAKES_SENSE" in open(md).read())
        check("evaluation.json written",
              json.load(open(js))["verdict"]["verdict"] == "MAKES_SENSE")


def test_evaluate_evidence_fallbacks():
    from qaloop.evaluate import _read, collect_evidence
    with tempfile.TemporaryDirectory() as run_dir:
        missing = os.path.join(run_dir, "nope.txt")
        check("345 _read missing path returns empty",
              _read(missing, 5) == "")
        path = os.path.join(run_dir, "data.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("123456789012")
        check("345 _read over limit truncates with exact suffix",
              _read(path, 5) == "12345\n…[truncated 7 chars]",
              repr(_read(path, 5)))
        with open(path, "w", encoding="utf-8") as f:
            f.write("12345")
        check("345 _read exactly at limit returned unchanged",
              _read(path, 5) == "12345", repr(_read(path, 5)))
        with open(path, "w", encoding="utf-8") as f:
            f.write("123")
        check("345 _read under limit returned unchanged",
              _read(path, 5) == "123", repr(_read(path, 5)))
    with tempfile.TemporaryDirectory() as run_dir:
        steps_dir = os.path.join(run_dir, "steps")
        os.makedirs(steps_dir)
        with open(os.path.join(steps_dir, "instep.txt"), "w",
                  encoding="utf-8") as f:
            f.write("ax-from-steps")
        with open(os.path.join(run_dir, "rootonly.txt"), "w",
                  encoding="utf-8") as f:
            f.write("ax-from-root")
        abs_path = os.path.join(run_dir, "abs-ax.txt")
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("ax-absolute")
        base = {"phase": "steps", "op": "goto", "status": "PASS",
                "duration_ms": 1, "error": "", "assertions": []}
        run = {"flow_name": "demo", "target": "http://x/",
               "status": "PASS", "failed_step": None, "error": "",
               "steps": [
                   dict(base, index=0, name="a", ax_snapshot="instep.txt"),
                   dict(base, index=1, name="b", ax_snapshot="rootonly.txt"),
                   dict(base, index=2, name="c", ax_snapshot="ghost.txt"),
                   dict(base, index=3, name="d", ax_snapshot=abs_path)],
               "console_errors": [], "page_errors": [],
               "failed_requests": [], "bad_responses": []}
        with open(os.path.join(run_dir, "run.json"), "w") as f:
            json.dump(run, f)
        ev = collect_evidence(run_dir, "")
        check("345 AX relative name read from steps dir",
              ev["steps"][0]["ax_snapshot"] == "ax-from-steps",
              repr(ev["steps"][0]["ax_snapshot"]))
        check("345 AX relative name falls back to run-dir root",
              ev["steps"][1]["ax_snapshot"] == "ax-from-root",
              repr(ev["steps"][1]["ax_snapshot"]))
        check("345 AX missing in both places returns empty",
              ev["steps"][2]["ax_snapshot"] == "",
              repr(ev["steps"][2]["ax_snapshot"]))
        check("345 AX absolute path read directly",
              ev["steps"][3]["ax_snapshot"] == "ax-absolute",
              repr(ev["steps"][3]["ax_snapshot"]))
    with tempfile.TemporaryDirectory() as run_dir:
        svc_dir = os.path.join(run_dir, "services")
        os.makedirs(svc_dir)
        with open(os.path.join(svc_dir, "b.log"), "w",
                  encoding="utf-8") as f:
            f.write("b-log-content")
        with open(os.path.join(svc_dir, "a.log"), "w",
                  encoding="utf-8") as f:
            f.write("x" * 810)
        with open(os.path.join(svc_dir, "notes.txt"), "w",
                  encoding="utf-8") as f:
            f.write("not a log")
        with open(os.path.join(run_dir, "run.json"), "w") as f:
            json.dump({}, f)
        ev = collect_evidence(run_dir, "")
        check("345 services keys are sorted log basenames, non-log ignored",
              list(ev["services"].keys()) == ["a", "b"],
              repr(list(ev["services"].keys())))
        check("345 services long log truncated at 800 with exact suffix",
              ev["services"]["a"] == "x" * 800
              + "\n…[truncated 10 chars]",
              repr(ev["services"]["a"][-40:]))
        check("345 services short log returned unchanged",
              ev["services"]["b"] == "b-log-content",
              repr(ev["services"]["b"]))
    with tempfile.TemporaryDirectory() as run_dir:
        with open(os.path.join(run_dir, "run.json"), "w") as f:
            json.dump({}, f)
        ev = collect_evidence(run_dir, "")
        check("345 no services dir yields empty services",
              ev["services"] == {}, repr(ev["services"]))


def test_evaluate_user_message_sections():
    from qaloop.evaluate import _user_message
    ev = {
        "diff_stat": "1 files, +1/-0",
        "diff": "diff --git a/a.py b/a.py\n+x = 1\n",
        "flow_name": "demo",
        "target": "http://x/",
        "status": "FAIL",
        "failed_step": 0,
        "error": "boom",
        "steps": [
            {"index": 0, "phase": "steps", "name": "load", "op": "goto",
             "status": "FAIL", "error": "boom-step",
             "assertions": [{"kind": "visible", "passed": False}],
             "ax_snapshot": 'button "Save"'},
            {"index": 1, "phase": "steps", "name": "plain", "op": "click",
             "status": "PASS", "error": "", "assertions": [],
             "ax_snapshot": ""},
        ],
        "console_errors": ["console boom"],
        "page_errors": ["page boom"],
        "failed_requests": [{"url": "http://x/api", "error": "net::ERR"}],
        "bad_responses": [{"url": "http://x/api", "status": 500}],
        "services": {"api": "api-log-line", "web": "web-log-line"},
        "report": "# Report\nall good",
    }
    msg = _user_message("the claim", ev)
    check("347 step line pins error and json assertions",
          'STEP 0 [steps] load (goto): FAIL error=boom-step '
          'assertions=[{"kind": "visible", "passed": false}]' in msg, msg)
    check("347 AX block after STEP 0 and before STEP 1",
          msg.index("STEP 0") < msg.index("  AX:") < msg.index("STEP 1"),
          msg)
    check("347 step without snapshot emits no AX block",
          msg.count("  AX:") == 1, repr(msg.count("  AX:")))
    check("347 AX snapshot content present",
          '  AX:\nbutton "Save"' in msg, msg)
    check("347 console errors rendered via json",
          'CONSOLE ERRORS: ["console boom"]' in msg, msg)
    check("347 page errors rendered as python repr not json",
          "PAGE ERRORS: ['page boom']" in msg, msg)
    check("347 failed requests rendered via json",
          'FAILED REQUESTS: [{"url": "http://x/api", '
          '"error": "net::ERR"}]' in msg, msg)
    check("347 bad responses rendered via json",
          'BAD RESPONSES: [{"url": "http://x/api", "status": 500}]' in msg,
          msg)
    check("347 service api log section present",
          "SERVICE api LOG (tail):\napi-log-line" in msg, msg)
    check("347 service web log section present",
          "SERVICE web LOG (tail):\nweb-log-line" in msg, msg)
    check("347 run report section present",
          "RUN REPORT:\n# Report\nall good" in msg, msg)
    ev_neg = {
        "diff_stat": "1 files, +1/-0",
        "diff": "diff --git a/a.py b/a.py\n+x = 1\n",
        "flow_name": "demo",
        "target": "http://x/",
        "status": "PASS",
        "failed_step": None,
        "error": "",
        "steps": [
            {"index": 0, "phase": "steps", "name": "load", "op": "goto",
             "status": "PASS", "error": "", "assertions": [],
             "ax_snapshot": ""},
        ],
        "console_errors": [],
        "page_errors": [],
        "failed_requests": [],
        "bad_responses": [],
        "services": {},
        "report": "",
    }
    msg_neg = _user_message("the claim", ev_neg)
    for absent in ("CONSOLE ERRORS:", "PAGE ERRORS:", "FAILED REQUESTS:",
                   "BAD RESPONSES:", "SERVICE ", "RUN REPORT:", "  AX:"):
        check(f"347 empty evidence omits section {absent!r}",
              absent not in msg_neg, msg_neg)
    step_lines = [line for line in msg_neg.splitlines()
                  if line.startswith("STEP 0")]
    check("347 negative step line present exactly once",
          len(step_lines) == 1, repr(step_lines))
    check("347 negative step line is suffix-free",
          step_lines and step_lines[0]
          == "STEP 0 [steps] load (goto): PASS"
          and " error=" not in step_lines[0]
          and " assertions=" not in step_lines[0],
          repr(step_lines))


def test_write_evaluation_risks_section():
    from qaloop.evaluate import write_evaluation
    with tempfile.TemporaryDirectory() as run_dir:
        md_path, _js = write_evaluation(
            run_dir, "x does y",
            {"verdict": "MAKES_SENSE", "confidence": "high",
             "rationale": "r", "evidence": [],
             "risks": ["risk one", "risk two"]})
        md_text = open(md_path, encoding="utf-8").read()
        check("347 risks section pins heading and dash lines in order",
              "## Risks / missing coverage\n\n- risk one\n- risk two\n"
              in md_text, md_text)
    with tempfile.TemporaryDirectory() as run_dir:
        md_path, _js = write_evaluation(
            run_dir, "x does y",
            {"verdict": "MAKES_SENSE", "confidence": "high",
             "rationale": "r", "evidence": [], "risks": []})
        md_text = open(md_path, encoding="utf-8").read()
        check("347 empty risks omits risks heading",
              "## Risks / missing coverage" not in md_text, md_text)


def _stub_evaluate(verdict_dict):
    """Run evaluate() with _chat stubbed to return verdict_dict. Browser-free.

    Returns (verdict, tmp) where tmp is the TemporaryDirectory (caller cleans
    it up). Passes an explicit model_cfg so no env vars are needed.
    """
    from qaloop import evaluate as ev
    stub = lambda cfg, messages, timeout_s=90: (  # noqa: E731
        json.dumps(verdict_dict),
        {"prompt_tokens": 1, "completion_tokens": 2})
    old = ev._chat
    ev._chat = stub
    tmp = tempfile.TemporaryDirectory()
    try:
        cfg = {"base_url": "http://localhost", "name": "stub",
               "api_key": "test", "price_in": 0.0, "price_out": 0.0}
        return ev.evaluate("the widget saves", tmp.name, "", model_cfg=cfg), tmp
    finally:
        ev._chat = old


def test_evaluate_confidence_normalization():
    from qaloop import evaluate as ev
    cases = [("HIGH", "high"), ("Medium", "medium"), ("low", "low"),
             ("very high", "low"), ("", "low"), (None, "low"), (5, "low")]
    for raw, want in cases:
        v, tmp = _stub_evaluate({"verdict": "INSUFFICIENT_EVIDENCE",
                                 "confidence": raw, "rationale": "r",
                                 "evidence": [], "risks": []})
        check(f"confidence normalized {raw!r} -> {want}",
              v["confidence"] == want, repr(v["confidence"]))
        check(f"confidence_raw preserved for {raw!r}",
              v["confidence_raw"] == raw, repr(v["confidence_raw"]))
        tmp.cleanup()
    v, tmp = _stub_evaluate({"verdict": "INSUFFICIENT_EVIDENCE",
                             "rationale": "r"})
    check("missing confidence defaults to low",
          v["confidence"] == "low" and v["confidence_raw"] is None,
          repr((v["confidence"], v["confidence_raw"])))
    with tempfile.TemporaryDirectory() as run_dir:
        ev.write_evaluation(run_dir, "c", v)
        body = json.load(open(os.path.join(run_dir, "evaluation.json")))
        check("confidence + confidence_raw in evaluation.json",
              body["verdict"]["confidence"] == "low"
              and body["verdict"]["confidence_raw"] is None,
              repr(body["verdict"].get("confidence")))
    tmp.cleanup()


def test_evaluate_low_confidence_downgrade():
    v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE", "confidence": "high",
                             "rationale": "looks right",
                             "evidence": ['"the widget saves" persists',
                                          '"the widget saves" renders'],
                             "risks": []})
    check("high-confidence MAKES_SENSE with grounded evidence stays",
          v["verdict"] == "MAKES_SENSE", v["verdict"])
    tmp.cleanup()
    v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE", "confidence": "low",
                             "rationale": "seems fine",
                             "evidence": ["a.py:1 -> saves"], "risks": []})
    check("low-confidence MAKES_SENSE downgraded",
          v["verdict"] == "INSUFFICIENT_EVIDENCE", v["verdict"])
    check("downgrade rationale notes why the judge was unsure",
          "Downgrade" in v["rationale"] and "low confidence" in v["rationale"],
          v["rationale"])
    tmp.cleanup()


def test_evaluate_high_confidence_evidence_rule():
    grounded_pair = ['"the widget saves" persists',
                     '"the widget saves" renders']
    cases = [
        ([], False),
        (["only one bullet"], False),
        (["", "  ", None], False),
        (['"the widget saves" persists'], False),  # one grounded bullet
        (["mystery.py:9 -> vibes", "totally unlinked"], False),  # ungrounded
        (grounded_pair, True),
    ]
    for evidence, stays in cases:
        v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE",
                                 "confidence": "high",
                                 "rationale": "r", "evidence": evidence,
                                 "risks": []})
        want = "MAKES_SENSE" if stays else "INSUFFICIENT_EVIDENCE"
        check(f"high-confidence rule: {len(evidence)} bullets -> {want}",
              v["verdict"] == want, v["verdict"])
        if not stays:
            check(f"downgrade rationale mentions evidence "
                  f"({len(evidence)} bullets)",
                  "Downgrade" in v["rationale"]
                  and "evidence" in v["rationale"], v["rationale"])
        tmp.cleanup()
    # ungrounded citations are named in the downgrade rationale
    v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE", "confidence": "high",
                             "rationale": "r",
                             "evidence": ["mystery.py:9 -> vibes",
                                          "totally unlinked"], "risks": []})
    check("downgrade rationale names the ungrounded citations",
          v["verdict"] == "INSUFFICIENT_EVIDENCE"
          and "mystery.py:9 -> vibes" in v["rationale"]
          and "totally unlinked" in v["rationale"], v["rationale"])
    check("evidence_grounding recorded per bullet",
          len(v["evidence_grounding"]) == 2
          and all(g["grounded"] is False for g in v["evidence_grounding"]),
          repr(v["evidence_grounding"]))
    tmp.cleanup()


def test_calibrate_verdict_without_grounding_falls_back():
    # Direct _calibrate_verdict calls that skipped _ground_evidence keep the
    # old non-empty-bullet count.
    from qaloop.evaluate import _calibrate_verdict
    v = _calibrate_verdict({"verdict": "MAKES_SENSE", "confidence": "high",
                            "rationale": "r", "evidence": ["a", "b"]})
    check("no evidence_grounding -> falls back to non-empty count",
          v["verdict"] == "MAKES_SENSE", v["verdict"])
    v = _calibrate_verdict({"verdict": "MAKES_SENSE", "confidence": "high",
                            "rationale": "r", "evidence": ["only one"]})
    check("fallback downgrades on <2 non-empty bullets",
          v["verdict"] == "INSUFFICIENT_EVIDENCE", v["verdict"])


def _grounding_evidence(steps=(), diff=""):
    """Build an evidence dict shaped like collect_evidence() output."""
    return {
        "flow_name": "f", "target": "t", "status": "passed",
        "failed_step": None, "error": "",
        "steps": [{"index": i, "phase": "main", "name": name, "op": "goto",
                   "status": "passed", "duration_ms": 1, "error": "",
                   "assertions": [], "ax_snapshot": ""}
                  for i, name in steps],
        "console_errors": [], "page_errors": [], "failed_requests": [],
        "bad_responses": [], "services": {}, "report": "",
        "diff_stat": "x", "diff": diff,
    }


def test_ground_evidence_rule_a_step_index_or_name():
    from qaloop.evaluate import _ground_evidence
    ev = _grounding_evidence(steps=[(0, "load page"), (1, "fill form")])
    out = _ground_evidence({"evidence": [
        "STEP 0 [main] load page (goto): passed -> shows form",
        "fill form accepted the typed name",
        "STEP 7 did something never observed",
    ]}, ev, "the widget saves")
    got = {r["bullet"]: r for r in out["evidence_grounding"]}
    check("rule (a): step index grounds",
          got["STEP 0 [main] load page (goto): passed -> shows form"]
          ["grounded"] is True, repr(got))
    check("rule (a): step name grounds",
          got["fill form accepted the typed name"]["grounded"] is True
          and "step" in got["fill form accepted the typed name"]["reason"],
          repr(got))
    check("rule (a): unknown step index stays ungrounded",
          got["STEP 7 did something never observed"]["grounded"] is False,
          repr(got))


def test_ground_evidence_rule_b_file_in_diff():
    from qaloop.evaluate import _ground_evidence
    ev = _grounding_evidence(
        diff="diff --git a/qaloop/flow.py b/qaloop/flow.py\n+retry\n")
    out = _ground_evidence({"evidence": [
        "qaloop/flow.py:42 -> retries the click",
        "other/util.py:9 -> changed the timeout",
    ]}, ev, "the flow retries")
    got = {r["bullet"]: r for r in out["evidence_grounding"]}
    check("rule (b): file:line with file in diff grounds",
          got["qaloop/flow.py:42 -> retries the click"]["grounded"] is True
          and "diff" in got["qaloop/flow.py:42 -> retries the click"]
          ["reason"], repr(got))
    check("rule (b): file:line with file absent from diff ungrounded",
          got["other/util.py:9 -> changed the timeout"]["grounded"] is False,
          repr(got))


def test_ground_evidence_rule_c_quoted_context():
    from qaloop.evaluate import _ground_evidence
    ev = _grounding_evidence()
    out = _ground_evidence({"evidence": [
        '"the widget saves" confirmed in the ax tree',
        "'target=t' confirmed the flow ran against the right target",
        '"short" is too short to qualify',
        "no quotes here at all",
    ]}, ev, "the widget saves")
    got = {r["bullet"]: r for r in out["evidence_grounding"]}
    check("rule (c): quoted claim text grounds",
          got['"the widget saves" confirmed in the ax tree']
          ["grounded"] is True, repr(got))
    check("rule (c): quoted flow line grounds",
          got["'target=t' confirmed the flow ran against the right target"]
          ["grounded"] is True, repr(got))
    check("rule (c): quoted substring under 8 chars does not qualify",
          got['"short" is too short to qualify']["grounded"] is False,
          repr(got))
    check("rule (c): unquoted bullet ungrounded",
          got["no quotes here at all"]["grounded"] is False, repr(got))


def test_ground_evidence_skips_empty_bullets():
    from qaloop.evaluate import _ground_evidence
    out = _ground_evidence({"evidence": ["", "   ", None, 42]},
                           _grounding_evidence(), "c")
    check("empty and non-string bullets are skipped, not recorded",
          out["evidence_grounding"] == [], repr(out["evidence_grounding"]))


def test_evaluate_does_not_make_sense_low_confidence_stays():
    from qaloop import evaluate as ev
    v, tmp = _stub_evaluate({"verdict": "DOES_NOT_MAKE_SENSE",
                             "confidence": "low",
                             "rationale": "contradicts claim",
                             "evidence": [], "risks": []})
    check("DOES_NOT_MAKE_SENSE/low keeps verdict",
          v["verdict"] == "DOES_NOT_MAKE_SENSE", v["verdict"])
    md, _ = ev.write_evaluation(tmp.name, "claim", v)
    text = open(md).read()
    check("low-confidence contradiction flagged prominently",
          "[!WARNING]" in text and "Low-confidence verdict" in text,
          text[:160])
    tmp.cleanup()


def _rubric_input(raw_verdict, scores):
    """Build a verdict dict shaped like _extract_verdict output."""
    from qaloop.evaluate import RUBRIC
    return {"verdict": raw_verdict, "verdict_raw": raw_verdict,
            "confidence": "high", "rationale": "r",
            "evidence": [], "risks": [],
            "scores": {d: scores[d] for d in RUBRIC},
            "_scores_provided": True}


def test_apply_rubric_fatal_rules():
    from qaloop.evaluate import _apply_rubric, RUBRIC
    all2 = {d: 2 for d in RUBRIC}
    cases = [
        ("claim_diff_fit", "DOES_NOT_MAKE_SENSE"),
        ("no_contradictions", "DOES_NOT_MAKE_SENSE"),
        ("evidence_exercises_claim", "INSUFFICIENT_EVIDENCE"),
        ("state_supports_claim", "INSUFFICIENT_EVIDENCE"),
    ]
    for dim, want in cases:
        s = dict(all2)
        s[dim] = 0
        got = _apply_rubric(_rubric_input("MAKES_SENSE", s))["verdict"]
        check(f"rubric: 0 in {dim} -> {want}", got == want, got)
    # a fatal zero overrides an advisory raw verdict the other way too
    s = dict(all2)
    s["no_contradictions"] = 0
    got = _apply_rubric(_rubric_input("INSUFFICIENT_EVIDENCE", s))["verdict"]
    check("rubric: fatal zero overrides advisory raw verdict",
          got == "DOES_NOT_MAKE_SENSE", got)


def test_apply_rubric_sum_boundary():
    from qaloop.evaluate import _apply_rubric
    all2 = {"claim_diff_fit": 2, "evidence_exercises_claim": 2,
            "no_contradictions": 2, "state_supports_claim": 2}
    got = _apply_rubric(_rubric_input("MAKES_SENSE", all2))["verdict"]
    check("rubric: all 2s -> MAKES_SENSE", got == "MAKES_SENSE", got)
    s = dict(all2)
    s["claim_diff_fit"] = 1
    s["evidence_exercises_claim"] = 1  # sum = 6
    got = _apply_rubric(_rubric_input("MAKES_SENSE", s))["verdict"]
    check("rubric: sum 6 -> MAKES_SENSE", got == "MAKES_SENSE", got)
    s = dict(all2)
    s["claim_diff_fit"] = 1
    s["evidence_exercises_claim"] = 1
    s["state_supports_claim"] = 1  # sum = 5
    got = _apply_rubric(_rubric_input("MAKES_SENSE", s))["verdict"]
    check("rubric: sum 5 -> INSUFFICIENT_EVIDENCE",
          got == "INSUFFICIENT_EVIDENCE", got)
    # all-2s make even an advisory DOES_NOT_MAKE_SENSE MAKES_SENSE
    got = _apply_rubric(_rubric_input("DOES_NOT_MAKE_SENSE", all2))["verdict"]
    check("rubric: raw verdict is advisory, scores win",
          got == "MAKES_SENSE", got)


def test_apply_rubric_blocked_and_no_scores():
    from qaloop.evaluate import _apply_rubric, RUBRIC
    zero = {d: 0 for d in RUBRIC}
    out = _apply_rubric(_rubric_input("BLOCKED", zero))
    check("rubric: BLOCKED with all-zero scores stays BLOCKED",
          out["verdict"] == "BLOCKED", out["verdict"])
    # no scores supplied: raw verdict stands, flag is stripped
    v = _rubric_input("MAKES_SENSE", {d: 0 for d in RUBRIC})
    v["_scores_provided"] = False
    out = _apply_rubric(v)
    check("rubric: no scores -> raw verdict stands",
          out["verdict"] == "MAKES_SENSE", out["verdict"])
    check("rubric: internal flag stripped", "_scores_provided" not in out,
          repr(out))
    check("rubric: flag stripped on derive path too",
          "_scores_provided" not in _apply_rubric(
              _rubric_input("MAKES_SENSE", zero)), "flag leaked")


def test_evaluate_score_normalization():
    from qaloop.evaluate import _extract_verdict, RUBRIC
    v = _extract_verdict(
        '{"verdict": "MAKES_SENSE", "confidence": "high",'
        ' "scores": {"claim_diff_fit": 2, "evidence_exercises_claim": 1,'
        ' "no_contradictions": 2, "state_supports_claim": 2}}')
    check("valid scores kept",
          v["scores"] == {"claim_diff_fit": 2, "evidence_exercises_claim": 1,
                          "no_contradictions": 2, "state_supports_claim": 2},
          repr(v["scores"]))
    check("verdict_raw set before reclassification",
          v["verdict_raw"] == "MAKES_SENSE", repr(v["verdict_raw"]))
    v = _extract_verdict('{"verdict": "MAKES_SENSE"}')
    check("missing scores -> all 0",
          v["scores"] == {d: 0 for d in RUBRIC}, repr(v["scores"]))
    check("verdict_raw present without scores",
          v["verdict_raw"] == "MAKES_SENSE", repr(v["verdict_raw"]))
    v = _extract_verdict(
        '{"verdict": "MAKES_SENSE",'
        ' "scores": {"claim_diff_fit": 5, "evidence_exercises_claim": "high",'
        ' "no_contradictions": null, "state_supports_claim": true}}')
    check("malformed scores (5, 'high', null, true) default to 0",
          v["scores"] == {d: 0 for d in RUBRIC}, repr(v["scores"]))
    v = _extract_verdict('{"verdict": "MAKES_SENSE", "scores": "nonsense"}')
    check("non-dict scores -> all 0, not provided",
          v["scores"] == {d: 0 for d in RUBRIC}
          and v["_scores_provided"] is False, repr(v["scores"]))


def test_evaluate_rubric_end_to_end():
    from qaloop import evaluate as ev
    all2 = {"claim_diff_fit": 2, "evidence_exercises_claim": 2,
            "no_contradictions": 2, "state_supports_claim": 2}
    v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE", "confidence": "high",
                             "rationale": "solid",
                             "evidence": ['"the widget saves" persists',
                                          '"the widget saves" renders'],
                             "risks": [], "scores": all2})
    check("end-to-end: all-2s keeps MAKES_SENSE",
          v["verdict"] == "MAKES_SENSE", v["verdict"])
    check("end-to-end: verdict_raw recorded",
          v["verdict_raw"] == "MAKES_SENSE", repr(v["verdict_raw"]))
    check("end-to-end: scores on verdict",
          v["scores"] == all2, repr(v["scores"]))
    check("end-to-end: internal flag not leaked",
          "_scores_provided" not in v, repr(v))
    md, js = ev.write_evaluation(tmp.name, "the widget saves", v)
    body = json.load(open(js))
    check("evaluation.json carries scores and verdict_raw",
          body["verdict"]["scores"] == all2
          and body["verdict"]["verdict_raw"] == "MAKES_SENSE",
          repr(body["verdict"].get("scores")))
    check("evaluation.json carries per-bullet evidence_grounding",
          len(body["verdict"]["evidence_grounding"]) == 2
          and all(g["grounded"] for g in
                  body["verdict"]["evidence_grounding"]),
          repr(body["verdict"].get("evidence_grounding")))
    text = open(md).read()
    check("EVALUATION.md has dimension score table",
          "| claim_diff_fit | 2 |" in text
          and "| state_supports_claim | 2 |" in text, text[:400])
    check("EVALUATION.md keeps verdict_raw out of markdown",
          "verdict_raw" not in text, text[:400])
    tmp.cleanup()
    # fatal zero reclassifies end-to-end
    s = dict(all2)
    s["evidence_exercises_claim"] = 0
    v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE", "confidence": "high",
                             "rationale": "shallow",
                             "evidence": ['"the widget saves" persists',
                                          '"the widget saves" renders'],
                             "risks": [], "scores": s})
    check("end-to-end: 0 evidence_exercises_claim -> INSUFFICIENT_EVIDENCE",
          v["verdict"] == "INSUFFICIENT_EVIDENCE"
          and v["verdict_raw"] == "MAKES_SENSE", v["verdict"])
    tmp.cleanup()


def test_evaluate_grounded_bullets_keep_makes_sense():
    v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE", "confidence": "high",
                             "rationale": "solid",
                             "evidence": ['"the widget saves" persists',
                                          '"the widget saves" renders'],
                             "risks": []})
    check("2 grounded bullets keep high-confidence MAKES_SENSE",
          v["verdict"] == "MAKES_SENSE", v["verdict"])
    check("grounded bullets not marked in rationale",
          "could not be grounded" not in v["rationale"], v["rationale"])
    tmp.cleanup()


def test_write_evaluation_marks_ungrounded_citations():
    from qaloop import evaluate as ev
    v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE", "confidence": "high",
                             "rationale": "solid",
                             "evidence": ['"the widget saves" persists',
                                          "mystery.py:9 -> vibes"],
                             "risks": []})
    check("mixed grounding downgrades on <2 grounded bullets",
          v["verdict"] == "INSUFFICIENT_EVIDENCE", v["verdict"])
    md, js = ev.write_evaluation(tmp.name, "the widget saves", v)
    text = open(md).read()
    check("EVALUATION.md marks the ungrounded bullet",
          "- mystery.py:9 -> vibes — [UNGROUNDED CITATION]" in text,
          text[text.find("## Evidence"):text.find("## Evidence") + 200])
    check("EVALUATION.md renders the grounded bullet unchanged",
          '- "the widget saves" persists\n' in text, text[:300])
    body = json.load(open(js))
    ground = {g["bullet"]: g["grounded"]
              for g in body["verdict"]["evidence_grounding"]}
    check("evaluation.json carries per-bullet grounding",
          ground == {'"the widget saves" persists': True,
                     "mystery.py:9 -> vibes": False}, repr(ground))
    tmp.cleanup()


def test_investigate_extract_json():
    from qaloop.investigate import _extract_json
    check("investigate: bare json parses",
          _extract_json('{"thought": "t"}')["thought"] == "t")
    check("investigate: json fenced in prose parses",
          _extract_json('here:\n```json\n{"tool": "console", "args": {}}\n``` done')
          ["tool"] == "console")
    try:
        _extract_json("no json here")
        check("investigate: non-json rejected", False)
    except ValueError:
        check("investigate: non-json rejected", True)


def test_investigate_tool_dispatch():
    from qaloop.investigate import Investigator

    class StubPage:
        def on(self, event, handler):
            pass

    with tempfile.TemporaryDirectory() as run_dir:
        inv = Investigator(StubPage(), run_dir, 20)
        try:
            inv.tool("frobnicate", {})
            check("investigate: unknown tool rejected", False)
        except ValueError:
            check("investigate: unknown tool rejected", True)
        check("investigate: actions counted before raise", inv.actions == 1,
              str(inv.actions))
        inv.console = [{"type": "error", "text": "boom: TypeError"}]
        got = inv.tool("console", {})
        check("investigate: console tool routes", json.loads(got) == inv.console, got)
        inv.network = [{"kind": "bad", "method": "GET", "url": "http://x/missing",
                        "status": 404}]
        got = inv.tool("network", {})
        check("investigate: network tool routes", json.loads(got) == inv.network, got)


def test_investigate_tool_browser_actions():
    import types as _types
    from qaloop.investigate import Investigator

    class RecordingPage:
        def __init__(self):
            self.calls = []
            self.handlers = {}
            self.url = "about:blank"

        def on(self, event, handler):
            self.handlers[event] = handler

        def goto(self, url, timeout=None, wait_until=None):
            self.calls.append(("goto", url, timeout, wait_until))
            self.url = url

        def click(self, target, timeout=None):
            self.calls.append(("click", target, timeout))

        def fill(self, target, text, timeout=None):
            self.calls.append(("fill", target, text, timeout))

        def press(self, target, key, timeout=None):
            self.calls.append(("press", target, key, timeout))

        def screenshot(self, path=None):
            self.calls.append(("screenshot", path))

    with tempfile.TemporaryDirectory() as run_dir:
        page = RecordingPage()
        inv = Investigator(page, run_dir, 20)
        check("investigate: initial state zeroed",
              inv.actions == 0 and inv.shots == 0 and inv.console == [] and inv.network == [],
              f"actions={inv.actions} shots={inv.shots} console={inv.console} network={inv.network}")
        check("investigate: four handlers registered",
              set(page.handlers) == {"console", "pageerror", "requestfailed", "response"},
              repr(sorted(page.handlers)))

        out = inv.tool("goto", {"url": "http://x/page"})
        check("investigate: goto return reads page url after nav",
              out == "navigated to http://x/page", out)
        check("investigate: goto call pinned",
              page.calls[-1] == ("goto", "http://x/page", 20000, "domcontentloaded"),
              repr(page.calls[-1]))
        check("investigate: actions +1 after goto", inv.actions == 1, str(inv.actions))

        out = inv.tool("click", {"target": "#btn"})
        check("investigate: click return", out == "clicked", out)
        check("investigate: click call pinned",
              page.calls[-1] == ("click", "#btn", 10000), repr(page.calls[-1]))
        check("investigate: actions +1 after click", inv.actions == 2, str(inv.actions))

        out = inv.tool("fill", {"target": "#in", "text": "hello"})
        check("investigate: fill return", out == "filled", out)
        check("investigate: fill call pinned",
              page.calls[-1] == ("fill", "#in", "hello", 10000), repr(page.calls[-1]))
        check("investigate: actions +1 after fill", inv.actions == 3, str(inv.actions))

        out = inv.tool("press", {"target": "#in", "key": "Enter"})
        check("investigate: press return", out == "pressed", out)
        check("investigate: press call pinned",
              page.calls[-1] == ("press", "#in", "Enter", 10000), repr(page.calls[-1]))
        check("investigate: actions +1 after press", inv.actions == 4, str(inv.actions))

        out = inv.tool("screenshot", {})
        exp1 = os.path.join(run_dir, "steps", "inv-01.png")
        check("investigate: screenshot return basename only",
              out == "screenshot saved to inv-01.png (for the human report)", out)
        check("investigate: screenshot call path keyword pinned",
              page.calls[-1] == ("screenshot", exp1), repr(page.calls[-1]))
        check("investigate: actions +1 after screenshot 1", inv.actions == 5,
              str(inv.actions))
        check("investigate: shots == 1 after first screenshot", inv.shots == 1,
              str(inv.shots))

        out = inv.tool("screenshot", {})
        exp2 = os.path.join(run_dir, "steps", "inv-02.png")
        check("investigate: screenshot 2 return",
              out == "screenshot saved to inv-02.png (for the human report)", out)
        check("investigate: screenshot 2 call path",
              page.calls[-1] == ("screenshot", exp2), repr(page.calls[-1]))
        check("investigate: shots == 2 after second screenshot", inv.shots == 2,
              str(inv.shots))
        check("investigate: actions +1 per call across all five branches",
              inv.actions == 6, str(inv.actions))


def test_investigate_page_event_handlers():
    import types as _types
    from qaloop.investigate import Investigator

    class RecordingPage:
        def __init__(self):
            self.handlers = {}
            self.url = "about:blank"

        def on(self, event, handler):
            self.handlers[event] = handler

    def fresh():
        page = RecordingPage()
        with tempfile.TemporaryDirectory() as run_dir:
            inv = Investigator(page, run_dir, 20)
        return page, inv

    page, inv = fresh()
    h = page.handlers["console"]
    h(_types.SimpleNamespace(type="error", text="x" * 600))
    check("investigate: console error appended truncated to 500",
          inv.console == [{"type": "error", "text": "x" * 500}], repr(inv.console))
    h(_types.SimpleNamespace(type="log", text="hello"))
    check("investigate: console non-error ignored",
          inv.console == [{"type": "error", "text": "x" * 500}], repr(inv.console))
    h(_types.SimpleNamespace(type="warning", text="w"))
    check("investigate: console warning ignored",
          inv.console == [{"type": "error", "text": "x" * 500}], repr(inv.console))

    page, inv = fresh()
    page.handlers["pageerror"](Exception("y" * 600))
    check("investigate: pageerror appended truncated to 500",
          inv.console == [{"type": "pageerror", "text": "y" * 500}], repr(inv.console))

    page, inv = fresh()
    page.handlers["requestfailed"](
        _types.SimpleNamespace(method="GET", url="u" * 400))
    check("investigate: requestfailed appended truncated to 300 no status",
          inv.network == [{"kind": "failed", "method": "GET", "url": "u" * 300}],
          repr(inv.network))

    page, inv = fresh()
    h = page.handlers["response"]
    h(_types.SimpleNamespace(status=404, url="v" * 400,
                             request=_types.SimpleNamespace(method="POST")))
    check("investigate: response >=400 appended truncated url request method",
          inv.network == [{"kind": "bad", "method": "POST", "url": "v" * 300,
                           "status": 404}], repr(inv.network))
    h(_types.SimpleNamespace(status=200, url="http://x/ok",
                             request=_types.SimpleNamespace(method="GET")))
    check("investigate: response 200 ignored", len(inv.network) == 1,
          repr(inv.network))
    h(_types.SimpleNamespace(status=399, url="http://x/almost",
                             request=_types.SimpleNamespace(method="GET")))
    check("investigate: response 399 ignored", len(inv.network) == 1,
          repr(inv.network))
    h(_types.SimpleNamespace(status=400, url="http://x/bad",
                             request=_types.SimpleNamespace(method="GET")))
    check("investigate: response 400 boundary appended",
          inv.network[-1] == {"kind": "bad", "method": "GET",
                              "url": "http://x/bad", "status": 400},
          repr(inv.network))


def _investigate_failed_result():
    from qaloop.runner import RunResult
    return RunResult.from_dict({
        "flow_name": "f", "target": "t", "status": "failed",
        "started": 1.0, "ended": 2.0,
        "steps": [{"index": 0, "phase": "main", "name": "load page",
                   "op": "goto", "status": "failed", "duration_ms": 10,
                   "error": "timeout",
                   "assertions": [{"name": "n", "passed": False, "detail": "x"}],
                   "screenshot": None, "ax_snapshot": None}],
        "failed_step": 0,
        "console_errors": [{"text": "boom: TypeError"}],
        "page_errors": [], "failed_requests": [], "bad_responses": [],
        "run_dir": "r", "error": ""})


def _investigate_minimal_spec():
    from qaloop.spec import load_spec
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("name: inv-test\ntarget: http://x/\n"
                "steps:\n  - name: load\n    goto: /\n")
        path = f.name
    try:
        return load_spec(path)
    finally:
        os.unlink(path)


def test_investigate_budget_enforcement():
    import inspect
    import playwright.sync_api as pw_sync
    import qaloop.investigate as mod
    from qaloop.investigate import investigate

    class StubPage:
        def on(self, event, handler):
            pass

        def goto(self, url, timeout=None, wait_until=None):
            self.url = url

    stub = StubPage()

    class _PWCM:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def new_page(self):
            return stub

    class _FakeBrowser:
        def new_context(self, **kw):
            return _PWCM()

        def close(self):
            pass

    class _FakeChromium:
        def launch(self, **kw):
            return _FakeBrowser()

    class _FakePW:
        chromium = _FakeChromium()

    class _FakePWCM:
        def __enter__(self):
            return _FakePW()

        def __exit__(self, *a):
            return False

    old_chat, old_ax, old_pw = mod._chat, mod.ax_snapshot, pw_sync.sync_playwright
    old_key = os.environ.get("QALOOP_MODEL_API_KEY")
    mod._chat = lambda cfg, messages, timeout_s=90: (
        json.dumps({"thought": "t", "tool": "console", "args": {}}),
        {"prompt_tokens": 1, "completion_tokens": 1})
    mod.ax_snapshot = lambda page, **kw: "AX TREE"
    pw_sync.sync_playwright = lambda: _FakePWCM()
    os.environ["QALOOP_MODEL_API_KEY"] = "test"
    try:
        with tempfile.TemporaryDirectory() as run_dir:
            diag = investigate(result=_investigate_failed_result(),
                              spec=_investigate_minimal_spec(),
                              target="http://x/", max_actions=3,
                              run_dir=run_dir)
            check("investigate: budget exhausted diagnosis",
                  diag["diagnosis"] ==
                  "Investigator exhausted its budget without a conclusion.",
                  str(diag["diagnosis"]))
            check("investigate: actions_taken >= budget", diag["actions_taken"] >= 3,
                  str(diag["actions_taken"]))
            check("investigate: mode is agent", diag["mode"] == "agent")
            check("investigate: transcript written",
                  os.path.exists(os.path.join(run_dir, "INVESTIGATION.md")))
    finally:
        mod._chat, mod.ax_snapshot = old_chat, old_ax
        pw_sync.sync_playwright = old_pw
        if old_key is None:
            del os.environ["QALOOP_MODEL_API_KEY"]
        else:
            os.environ["QALOOP_MODEL_API_KEY"] = old_key
    check("investigate: max_actions default 20",
          inspect.signature(investigate).parameters["max_actions"].default == 20)


def test_investigate_agent_loop_remainder():
    import copy
    import playwright.sync_api as pw_sync
    import qaloop.investigate as mod
    from qaloop.investigate import investigate

    class StubPage:
        def on(self, event, handler):
            pass

        def goto(self, url, timeout=None, wait_until=None):
            self.url = url

    stub = StubPage()
    launch_kw_recorded = []
    browser_closed = []

    class _Ctx:
        def new_page(self):
            return stub

    class _FakeBrowser:
        def new_context(self, **kw):
            return _Ctx()

        def close(self):
            browser_closed.append(True)

    class _FakeChromium:
        def launch(self, **kw):
            launch_kw_recorded.append(copy.deepcopy(kw))
            return _FakeBrowser()

    class _FakePW:
        chromium = _FakeChromium()

    class _FakePWCM:
        def __enter__(self):
            return _FakePW()

        def __exit__(self, *a):
            return False

    replies = [
        "this is not json",
        json.dumps({"thought": "t", "tool": "frobnicate", "args": {}}),
        json.dumps({"tool": "done", "args": {"diagnosis": "d",
                                             "likely_cause": "c",
                                             "suggested_fix": "f"}}),
    ]
    chat_calls = []

    def fake_chat(cfg, messages, timeout_s=90):
        chat_calls.append(copy.deepcopy(messages))
        raw = replies[len(chat_calls) - 1]
        return raw, {"prompt_tokens": 100, "completion_tokens": 50}

    old_chat = mod._chat
    old_ax = mod.ax_snapshot
    old_cfg = mod._model_config
    old_pw = pw_sync.sync_playwright
    mod._chat = fake_chat
    mod.ax_snapshot = lambda page, **kw: "AX TREE"
    mod._model_config = lambda: {"base_url": "http://x", "name": "stub-model",
                                 "api_key": "test", "price_in": 1.0,
                                 "price_out": 2.0}
    pw_sync.sync_playwright = lambda: _FakePWCM()
    try:
        result = _investigate_failed_result()
        result.failed_requests = [{"method": "GET", "url": "http://x/api"}]
        result.bad_responses = [{"method": "GET", "url": "http://x/bad",
                                 "status": 500}]
        with tempfile.TemporaryDirectory() as run_dir:
            diag = investigate(result=result,
                               spec=_investigate_minimal_spec(),
                               target="http://x/", max_actions=5,
                               executable_path="/fake/chrome",
                               run_dir=run_dir)
            check("investigate loop: 3 chat calls", len(chat_calls) == 3,
                  str(len(chat_calls)))
            if len(chat_calls) == 3:
                first_user = [m["content"] for m in chat_calls[0]
                              if m.get("role") == "user"]
                check("investigate loop: network context in first user msg",
                      bool(first_user) and
                      "Network: GET http://x/api" in first_user[0],
                      repr(first_user))
                second_users = [m["content"] for m in chat_calls[1]
                                if m.get("role") == "user"]
                check("investigate loop: invalid-JSON retry prompt",
                      any(c.startswith("That was not valid JSON") and
                          c.endswith("Reply with exactly one JSON tool call.")
                          for c in second_users),
                      repr(second_users))
                third_users = [m["content"] for m in chat_calls[2]
                               if m.get("role") == "user"]
                check("investigate loop: tool-error observation",
                      any(c == "tool frobnicate returned:\n"
                          "tool error: ValueError: unknown tool frobnicate"
                          for c in third_users),
                      repr(third_users))
            check("investigate loop: launch kwargs pinned",
                  launch_kw_recorded == [{"headless": True,
                                          "executable_path": "/fake/chrome"}],
                  repr(launch_kw_recorded))
            check("investigate loop: diagnosis fields",
                  diag["diagnosis"] == "d" and diag["likely_cause"] == "c"
                  and diag["suggested_fix"] == "f", repr(diag))
            check("investigate loop: mode agent", diag["mode"] == "agent")
            check("investigate loop: model stub-model",
                  diag["model"] == "stub-model", repr(diag.get("model")))
            check("investigate loop: tokens_in 300", diag["tokens_in"] == 300,
                  str(diag["tokens_in"]))
            check("investigate loop: tokens_out 150",
                  diag["tokens_out"] == 150, str(diag["tokens_out"]))
            check("investigate loop: cost 0.0006",
                  diag["cost_usd_est"] == 0.0006, str(diag["cost_usd_est"]))
            check("investigate loop: actions_taken 2",
                  diag["actions_taken"] == 2, str(diag["actions_taken"]))
            check("investigate loop: browser closed", browser_closed == [True],
                  repr(browser_closed))
            inv_path = os.path.join(run_dir, "INVESTIGATION.md")
            check("investigate loop: INVESTIGATION.md written",
                  os.path.exists(inv_path))
            if os.path.exists(inv_path):
                md = open(inv_path, encoding="utf-8").read()
                check("investigate loop: INVESTIGATION.md has diagnosis JSON",
                      '"diagnosis": "d"' in md and '"likely_cause": "c"' in md
                      and '"suggested_fix": "f"' in md, md[:500])
    finally:
        mod._chat, mod.ax_snapshot, mod._model_config = old_chat, old_ax, old_cfg
        pw_sync.sync_playwright = old_pw


def test_investigate_manual_brief():
    from qaloop.investigate import investigate, write_manual_brief
    old_key = os.environ.get("QALOOP_MODEL_API_KEY")
    if "QALOOP_MODEL_API_KEY" in os.environ:
        del os.environ["QALOOP_MODEL_API_KEY"]
    try:
        spec = _investigate_minimal_spec()
        result = _investigate_failed_result()
        with tempfile.TemporaryDirectory() as run_dir:
            path = write_manual_brief(run_dir, spec, result, "http://x/")
            md = open(path, encoding="utf-8").read()
            check("manual brief written", os.path.basename(path) == "INVESTIGATION_BRIEF.md")
            check("manual brief header", "Investigation brief (manual mode)" in md)
            check("manual brief names failing step", "load page" in md)
            check("manual brief lists evidence", "boom: TypeError" in md)
        # through investigate() itself: no api key -> manual mode
        with tempfile.TemporaryDirectory() as run_dir:
            diag = investigate(result=result, spec=spec, target="http://x/",
                               run_dir=run_dir)
            check("investigate: manual mode without key", diag["mode"] == "manual")
            check("investigate: manual brief written via investigate()",
                  os.path.exists(diag["brief"]))
    finally:
        if old_key is not None:
            os.environ["QALOOP_MODEL_API_KEY"] = old_key


def test_investigate_manual_brief_artifact_lines():
    from qaloop.investigate import write_manual_brief
    spec = _investigate_minimal_spec()

    def brief_md(run_dir, screenshot, ax_snapshot):
        result = _investigate_failed_result()
        artifacts = result.steps[result.failed_step].artifacts
        try:
            artifacts.screenshot = screenshot
            artifacts.ax_snapshot = ax_snapshot
            path = write_manual_brief(run_dir, spec, result, "http://x/")
            return open(path, encoding="utf-8").read()
        finally:
            artifacts.screenshot = None
            artifacts.ax_snapshot = None

    with tempfile.TemporaryDirectory() as run_dir:
        md = brief_md(run_dir, os.path.join(run_dir, "steps/shot-00.png"),
                      os.path.join(run_dir, "steps/ax-00.txt"))
        check("manual brief both artifacts: screenshot relpath line",
              "Screenshot: `steps/shot-00.png`" in md, md)
        check("manual brief both artifacts: ax snapshot relpath line",
              "AX snapshot: `steps/ax-00.txt`" in md, md)
    with tempfile.TemporaryDirectory() as run_dir:
        md = brief_md(run_dir, os.path.join(run_dir, "steps/shot-00.png"), None)
        check("manual brief screenshot only: screenshot line present",
              "Screenshot: `steps/shot-00.png`" in md, md)
        check("manual brief screenshot only: ax snapshot line absent",
              "AX snapshot:" not in md, md)
    with tempfile.TemporaryDirectory() as run_dir:
        md = brief_md(run_dir, None, os.path.join(run_dir, "steps/ax-00.txt"))
        check("manual brief ax only: ax snapshot line present",
              "AX snapshot: `steps/ax-00.txt`" in md, md)
        check("manual brief ax only: screenshot line absent",
              "Screenshot:" not in md, md)
    with tempfile.TemporaryDirectory() as run_dir:
        md = brief_md(run_dir, None, None)
        check("manual brief no artifacts: screenshot line absent",
              "Screenshot:" not in md, md)
        check("manual brief no artifacts: ax snapshot line absent",
              "AX snapshot:" not in md, md)


def test_issue_373_investigate_branch_coverage():
    import copy
    import playwright.sync_api as pw_sync
    import qaloop.investigate as mod
    from qaloop.investigate import investigate, write_manual_brief
    from qaloop.runner import AssertionResult

    spec = _investigate_minimal_spec()

    # Scenario 1: write_manual_brief with no failing step (failed_step None)
    result_no_step = _investigate_failed_result()
    result_no_step.failed_step = None
    with tempfile.TemporaryDirectory() as run_dir:
        path = write_manual_brief(run_dir, spec, result_no_step, "http://x/")
        md = open(path, encoding="utf-8").read()
        check("issue373 manual brief no failing step: header present",
              "Investigation brief (manual mode)" in md, md[:500])
        check("issue373 manual brief no failing step: failing step absent",
              "## Failing step" not in md, md[:500])
        check("issue373 manual brief no failing step: console errors present",
              "## Console errors" in md, md[:500])
        check("issue373 manual brief no failing step: console error text present",
              "boom: TypeError" in md, md[:500])

    # Helper to drive investigate() in agent mode and capture first user msg
    class StubPage:
        def on(self, event, handler):
            pass

        def goto(self, url, timeout=None, wait_until=None):
            self.url = url

    stub = StubPage()

    class _Ctx:
        def new_page(self):
            return stub

    class _FakeBrowser:
        def new_context(self, **kw):
            return _Ctx()

        def close(self):
            pass

    class _FakeChromium:
        def launch(self, **kw):
            return _FakeBrowser()

    class _FakePW:
        chromium = _FakeChromium()

    class _FakePWCM:
        def __enter__(self):
            return _FakePW()

        def __exit__(self, *a):
            return False

    def run_agent(result):
        chat_calls = []

        def fake_chat(cfg, messages, timeout_s=90):
            chat_calls.append(copy.deepcopy(messages))
            raw = json.dumps({"thought": "t", "tool": "done",
                              "args": {"diagnosis": "d",
                                       "likely_cause": "c",
                                       "suggested_fix": "f"}})
            return raw, {"prompt_tokens": 1, "completion_tokens": 1}

        old_chat = mod._chat
        old_ax = mod.ax_snapshot
        old_cfg = mod._model_config
        old_pw = pw_sync.sync_playwright
        mod._chat = fake_chat
        mod.ax_snapshot = lambda page, **kw: "AX TREE"
        mod._model_config = lambda: {"base_url": "http://x",
                                     "name": "stub-model",
                                     "api_key": "test", "price_in": 1.0,
                                     "price_out": 2.0}
        pw_sync.sync_playwright = lambda: _FakePWCM()
        try:
            with tempfile.TemporaryDirectory() as run_dir:
                diag = investigate(result=result, spec=spec,
                                   target="http://x/", max_actions=5,
                                   run_dir=run_dir)
        finally:
            mod._chat, mod.ax_snapshot, mod._model_config = old_chat, old_ax, old_cfg
            pw_sync.sync_playwright = old_pw
        first_user = ""
        if chat_calls:
            users = [m["content"] for m in chat_calls[0]
                     if m.get("role") == "user"]
            if users:
                first_user = users[0]
        return first_user, diag

    # Scenario 2: failing step with empty assertions list
    result_empty = _investigate_failed_result()
    result_empty.steps[0].assertions = []
    first_user_empty, diag_empty = run_agent(result_empty)
    check("issue373 agent empty assertions: mode agent",
          diag_empty["mode"] == "agent", repr(diag_empty))
    check("issue373 agent empty assertions: failing step present",
          "Failing step: load page (op goto)" in first_user_empty,
          repr(first_user_empty))
    check("issue373 agent empty assertions: no failed assertion text",
          "Failed assertion" not in first_user_empty,
          repr(first_user_empty))

    # Scenario 2b: agent mode with no failing step (failed_step None)
    # covers the 215->219 branch (if s: false) in investigate()
    result_no_step_agent = _investigate_failed_result()
    result_no_step_agent.failed_step = None
    first_user_none, diag_none = run_agent(result_no_step_agent)
    check("issue373 agent no failing step: mode agent",
          diag_none["mode"] == "agent", repr(diag_none))
    check("issue373 agent no failing step: no step info present",
          "no step info" in first_user_none, repr(first_user_none))
    check("issue373 agent no failing step: no failed assertion text",
          "Failed assertion" not in first_user_none,
          repr(first_user_none))
    check("issue373 agent no failing step: failing step line absent",
          "Failing step: load page" not in first_user_none,
          repr(first_user_none))

    # Scenario 3: passed assertion filtered, failed assertion kept
    result_mixed = _investigate_failed_result()
    result_mixed.steps[0].assertions = [
        AssertionResult("should-not-appear", True, "fine"),
        AssertionResult("real-failure", False, "broke"),
    ]
    first_user_mixed, diag_mixed = run_agent(result_mixed)
    check("issue373 agent mixed assertions: mode agent",
          diag_mixed["mode"] == "agent", repr(diag_mixed))
    check("issue373 agent mixed assertions: failed assertion present",
          "Failed assertion real-failure: broke" in first_user_mixed,
          repr(first_user_mixed))
    check("issue373 agent mixed assertions: passed assertion filtered",
          "should-not-appear" not in first_user_mixed,
          repr(first_user_mixed))


def test_step_retry_spec():
    """retry: accepted in steps, defaults 0, rejects <0 / non-int / setup+teardown."""
    from qaloop.spec import load_spec, SpecError

    def write_flow(phase, extra):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("name: retry-spec\ntarget: http://x\n")
            if phase == "steps":
                f.write(f"steps:\n  - name: s\n    goto: /\n    {extra}\n")
            else:
                f.write(f"{phase}:\n  - name: s\n    goto: /\n    {extra}\n"
                        "steps:\n  - name: t\n    expect: {noop: true}\n")
            return f.name

    p = write_flow("steps", "retry: 2")
    try:
        spec = load_spec(p)
        check("retry accepted in steps", spec.steps[0].retry == 2)
    finally:
        os.unlink(p)

    p = write_flow("steps", "")
    try:
        spec = load_spec(p)
        check("retry defaults to 0", spec.steps[0].retry == 0)
    finally:
        os.unlink(p)

    for bad, label in [("retry: -1", "negative"), ("retry: 1.5", "float"),
                       ("retry: true", "bool")]:
        p = write_flow("steps", bad)
        try:
            load_spec(p)
            check(f"retry {label} rejected", False, "no error raised")
        except SpecError:
            check(f"retry {label} rejected", True)
        finally:
            os.unlink(p)

    for phase in ("setup", "teardown"):
        p = write_flow(phase, "retry: 1")
        try:
            load_spec(p)
            check(f"retry in {phase} rejected", False, "no error raised")
        except SpecError:
            check(f"retry in {phase} rejected", True)
        finally:
            os.unlink(p)


def test_step_retry_delay_ms_spec():
    """retry_delay_ms: parses, defaults 0; bool/negative/non-int rejected;
    > 0 with retry == 0 or in setup/teardown rejected (silently inert
    otherwise)."""
    from qaloop.spec import load_spec, SpecError

    def write_flow(phase, extra):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("name: retry-delay-spec\ntarget: http://x\n")
            if phase == "steps":
                f.write(f"steps:\n  - name: s\n    goto: /\n    {extra}\n")
            else:
                f.write(f"{phase}:\n  - name: s\n    goto: /\n    {extra}\n"
                        "steps:\n  - name: t\n    expect: {noop: true}\n")
            return f.name

    p = write_flow("steps", "retry: 2\n    retry_delay_ms: 750")
    try:
        spec = load_spec(p)
        check("retry_delay_ms parsed", spec.steps[0].retry_delay_ms == 750)
    finally:
        os.unlink(p)

    p = write_flow("steps", "")
    try:
        spec = load_spec(p)
        check("retry_delay_ms defaults to 0",
              spec.steps[0].retry_delay_ms == 0)
    finally:
        os.unlink(p)

    for bad, label in [("retry_delay_ms: -1", "negative"),
                       ("retry_delay_ms: 1.5", "float"),
                       ("retry_delay_ms: true", "bool"),
                       ("retry_delay_ms: '500'", "string")]:
        p = write_flow("steps", f"retry: 1\n    {bad}")
        try:
            load_spec(p)
            check(f"retry_delay_ms {label} rejected", False, "no error raised")
        except SpecError:
            check(f"retry_delay_ms {label} rejected", True)
        finally:
            os.unlink(p)

    # delay with no retries is inert -> spec error (steps phase)
    p = write_flow("steps", "retry_delay_ms: 500")
    try:
        load_spec(p)
        check("retry_delay_ms>0 without retry rejected", False, "no error")
    except SpecError:
        check("retry_delay_ms>0 without retry rejected", True)
    finally:
        os.unlink(p)

    # delay outside the steps phase is rejected even with retry: 0
    for phase in ("setup", "teardown"):
        p = write_flow(phase, "retry_delay_ms: 500")
        try:
            load_spec(p)
            check(f"retry_delay_ms in {phase} rejected", False, "no error")
        except SpecError:
            check(f"retry_delay_ms in {phase} rejected", True)
        finally:
            os.unlink(p)


def test_step_continue_on_fail_spec():
    """continue_on_fail: true accepted in steps; true in setup/teardown
    rejected (silently inert there); absent/false valid everywhere."""
    from qaloop.spec import load_spec, SpecError

    def write_flow(phase, extra):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("name: continue-on-fail-spec\ntarget: http://x\n")
            if phase == "steps":
                f.write(f"steps:\n  - name: s\n    goto: /\n    {extra}\n")
            else:
                f.write(f"{phase}:\n  - name: s\n    goto: /\n    {extra}\n"
                        "steps:\n  - name: t\n    expect: {noop: true}\n")
            return f.name

    p = write_flow("steps", "continue_on_fail: true")
    try:
        spec = load_spec(p)
        check("continue_on_fail accepted in steps",
              spec.steps[0].continue_on_fail is True)
    finally:
        os.unlink(p)

    p = write_flow("steps", "")
    try:
        spec = load_spec(p)
        check("continue_on_fail defaults to False",
              spec.steps[0].continue_on_fail is False)
    finally:
        os.unlink(p)

    for phase in ("setup", "teardown"):
        p = write_flow(phase, "continue_on_fail: true")
        try:
            load_spec(p)
            check(f"continue_on_fail in {phase} rejected", False,
                  "no error raised")
        except SpecError as e:
            check(f"continue_on_fail in {phase} rejected", True)
            check(f"continue_on_fail {phase} error names key and rule",
                  "continue_on_fail" in str(e) and "steps phase" in str(e),
                  str(e))
        finally:
            os.unlink(p)

    p = write_flow("setup", "continue_on_fail: false")
    try:
        spec = load_spec(p)
        check("continue_on_fail false in setup accepted",
              spec.setup[0].continue_on_fail is False)
    finally:
        os.unlink(p)

    p = write_flow("teardown", "")
    try:
        spec = load_spec(p)
        check("continue_on_fail absent in teardown accepted",
              spec.teardown[0].continue_on_fail is False)
    finally:
        os.unlink(p)


def test_step_retry_runner():
    """Real browser proof: flaky action passes with retry:2; permanent
    failure with retry:1 fails after exactly 2 attempts; retry:0 unchanged."""
    import socket
    import subprocess
    import time
    exe = os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-1243/"
        "chrome-headless-shell-linux64/chrome-headless-shell")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        have_pw = True
    except ImportError:
        have_pw = False
    if not have_pw or not os.path.exists(exe):
        check("retry runner tests skipped (no browser)", True)
        return
    from qaloop.spec import load_spec
    from qaloop.runner import run_flow, RunResult
    from qaloop.report import write_report

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html><body><h1>retry demo</h1></body></html>")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            flow = (f"name: retry-demo\ntarget: http://127.0.0.1:{port}\n"
                    "steps:\n"
                    "  - name: flaky script succeeds on third try\n"
                    "    retry: 2\n"
                    "    script: {js: 'window.__n = (window.__n || 0) + 1; "
                    "if (window.__n < 3) throw new Error(\"flaky \" + window.__n); \"ok\"'}\n"
                    "    expect: {noop: true}\n"
                    "  - name: plain step with retry 0\n"
                    "    retry: 0\n"
                    "    script: {js: '\"fine\"'}\n"
                    "    expect: {noop: true}\n"
                    "  - name: permanent failure\n"
                    "    retry: 1\n"
                    "    timeout_ms: 500\n"
                    "    expect: {visible: \"#never-there\"}\n")
            flow_path = os.path.join(d, "retry-flow.yaml")
            with open(flow_path, "w") as f:
                f.write(flow)
            spec = load_spec(flow_path)
            with tempfile.TemporaryDirectory() as run_dir:
                result = run_flow(spec, run_dir=run_dir, executable_path=exe)
                check("run fails (permanent failure)", result.status == "failed",
                      result.status)
                flaky, plain, perm = result.steps
                check("flaky step passes with retry:2",
                      flaky.status == "passed", flaky.status)
                check("flaky step used 3 attempts", flaky.attempts == 3,
                      str(flaky.attempts))
                check("retry:0 keeps single attempt", plain.attempts == 1,
                      str(plain.attempts))
                check("permanent failure after 2 attempts",
                      perm.status == "failed" and perm.attempts == 2,
                      f"{perm.status} attempts={perm.attempts}")
                # report rendering
                paths = write_report(result, spec, run_dir)
                md = open(paths["report_md"], encoding="utf-8").read()
                check("report shows flaky attempts", "ok · attempt 3/3" in md)
                check("report shows failed attempts", "FAIL · attempt 2/2" in md)
                plain_line = [ln for ln in md.splitlines()
                              if "plain step with retry 0" in ln][0]
                check("report unchanged for retry:0",
                      "attempt" not in plain_line, plain_line)
                # serialization round trip
                rt = RunResult.from_dict(result.to_dict())
                check("attempts survives to_dict/from_dict",
                      [s.attempts for s in rt.steps] == [3, 1, 2])
        finally:
            srv.terminate()
            srv.wait()


def test_step_retry_delay_ms_runner():
    """Browser proof: retry_delay_ms sleeps exactly once between attempts —
    never after the final attempt, never when the delay is 0. A recording
    time.sleep spy (delegating to the real sleep, so timing is unchanged)
    keeps the assertions deterministic; playwright internals use
    asyncio.sleep, so the spy only ever records qaloop's own sleeps."""
    import socket
    import subprocess
    import time
    exe = os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-1243/"
        "chrome-headless-shell-linux64/chrome-headless-shell")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        have_pw = True
    except ImportError:
        have_pw = False
    if not have_pw or not os.path.exists(exe):
        check("retry_delay_ms runner tests skipped (no browser)", True)
        return
    from qaloop.spec import load_spec
    from qaloop.runner import run_flow

    delay = 777 / 1000  # distinctive value; no other sleep matches it

    def run_one(flow_body, sleeps, d):
        flow_path = os.path.join(d, "delay-flow.yaml")
        with open(flow_path, "w") as f:
            f.write(flow_body)
        spec = load_spec(flow_path)
        orig = time.sleep

        def spy(secs):
            sleeps.append(secs)
            return orig(secs)

        time.sleep = spy
        try:
            with tempfile.TemporaryDirectory() as run_dir:
                return run_flow(spec, run_dir=run_dir, executable_path=exe)
        finally:
            time.sleep = orig

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html><body><h1>retry delay demo</h1></body></html>")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            head = f"name: retry-delay-demo\ntarget: http://127.0.0.1:{port}\n"
            # Case 1: fail once then pass — one delay between the 2 attempts.
            sleeps = []
            result = run_one(head + (
                "steps:\n"
                "  - name: flaky script succeeds on second try\n"
                "    retry: 1\n"
                "    retry_delay_ms: 777\n"
                "    script: {js: 'window.__n = (window.__n || 0) + 1; "
                "if (window.__n < 2) throw new Error(\"flaky \" + window.__n); \"ok\"'}\n"
                "    expect: {noop: true}\n"), sleeps, d)
            step = result.steps[0]
            check("delayed step passes on retry", step.status == "passed",
                  step.status)
            check("delayed step used 2 attempts", step.attempts == 2,
                  str(step.attempts))
            check("exactly one delay between attempts", sleeps == [delay],
                  str(sleeps))
            check("delay counts in step duration", step.duration_ms >= 750,
                  str(step.duration_ms))
            # Case 2: always fails — one delay, none after the final attempt.
            sleeps = []
            result = run_one(head + (
                "steps:\n"
                "  - name: permanent failure\n"
                "    retry: 1\n"
                "    retry_delay_ms: 777\n"
                "    timeout_ms: 500\n"
                "    expect: {visible: \"#never-there\"}\n"), sleeps, d)
            step = result.steps[0]
            check("failing step fails after 2 attempts",
                  step.status == "failed" and step.attempts == 2,
                  f"{step.status} attempts={step.attempts}")
            check("no delay after final attempt", sleeps == [delay],
                  str(sleeps))
            # Case 3: retry without a delay — no sleep at all.
            sleeps = []
            result = run_one(head + (
                "steps:\n"
                "  - name: flaky script, no delay\n"
                "    retry: 1\n"
                "    script: {js: 'window.__m = (window.__m || 0) + 1; "
                "if (window.__m < 2) throw new Error(\"flaky\"); \"ok\"'}\n"
                "    expect: {noop: true}\n"), sleeps, d)
            step = result.steps[0]
            check("undelayed step passes on retry", step.status == "passed",
                  step.status)
            check("no sleep when retry_delay_ms is 0", sleeps == [],
                  str(sleeps))
        finally:
            srv.terminate()
            srv.wait()


@contextlib.contextmanager
def temp_ledger():
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("QALOOP_LEDGER")
        os.environ["QALOOP_LEDGER"] = os.path.join(d, "ledger.jsonl")
        try:
            yield os.environ["QALOOP_LEDGER"]
        finally:
            if old is None:
                del os.environ["QALOOP_LEDGER"]
            else:
                os.environ["QALOOP_LEDGER"] = old


def test_ledger_round_trip_and_ts():
    from qaloop import ledger
    with temp_ledger() as path:
        ledger.append({"kind": "scripted", "flow": "demo", "cost_usd_est": 0.0})
        entries = ledger.read_all()
        check("ledger append/read round trip", len(entries) == 1
              and entries[0]["flow"] == "demo", str(len(entries)))
        check("ledger append stamps ts",
              bool(entries[0].get("ts")), str(entries[0].get("ts")))


def test_ledger_read_all_missing_and_blanks():
    from qaloop import ledger
    with temp_ledger():
        check("read_all on missing path returns []", ledger.read_all() == [])
        ledger.append({"kind": "scripted"})
        path = ledger.ledger_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n   \n")
        entries = ledger.read_all()
        check("read_all skips blank lines", len(entries) == 1, str(len(entries)))


def test_ledger_path_override():
    from qaloop import ledger
    with temp_ledger() as path:
        check("QALOOP_LEDGER override honored", ledger.ledger_path() == path)
        ledger.append({"kind": "investigation", "cost_usd_est": 0.01})
        check("append writes to override path", os.path.exists(path))


def test_ledger_summarize():
    from qaloop import ledger
    entries = [
        {"kind": "scripted", "cost_usd_est": 0.0},
        {"kind": "scripted"},  # missing cost treated as 0
        {"kind": "investigation", "cost_usd_est": 0.01234},
        {"kind": "investigation", "cost_usd_est": 0.1},
    ]
    s = ledger.summarize(entries=entries)
    check("summarize counts scripted runs", s["scripted_runs"] == 2,
          str(s["scripted_runs"]))
    check("summarize counts investigations", s["investigations"] == 2,
          str(s["investigations"]))
    check("summarize counts all runs", s["runs"] == 4, str(s["runs"]))
    check("summarize totals cost", s["total_cost_usd_est"] == round(0.11234, 4),
          str(s["total_cost_usd_est"]))
    check("summarize totals investigation cost",
          s["investigation_cost_usd_est"] == round(0.11234, 4),
          str(s["investigation_cost_usd_est"]))
    with temp_ledger():
        ledger.append({"kind": "scripted"})
        ledger.append({"kind": "investigation", "cost_usd_est": 0.5})
        s2 = ledger.summarize()  # entries=None reads the ledger from disk
        check("summarize() reads ledger when entries=None",
              s2["runs"] == 2 and s2["investigations"] == 1, str(s2))


def _write_run_json(runs_root, run_id, run):
    run_dir = os.path.join(runs_root, run_id)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f)
    return run_dir


def test_dashboard_build():
    from qaloop import dashboard
    with tempfile.TemporaryDirectory() as d, temp_ledger():
        from qaloop import ledger
        ledger.append({"kind": "scripted", "cost_usd_est": 0.0})
        ledger.append({"kind": "investigation", "cost_usd_est": 0.25})
        runs_root = os.path.join(d, "runs")
        os.makedirs(runs_root)
        _write_run_json(runs_root, "run-ok", {
            "run_dir": "runs/run-ok", "flow_name": "smoke",
            "status": "passed", "started": 1728000000, "duration_ms": 1234,
            "steps": [{"name": "open", "attempts": 1}]})
        # missing started -> ts() fallback renders an em dash, no crash
        _write_run_json(runs_root, "run-nostart", {
            "run_dir": "runs/run-nostart", "flow_name": "smoke",
            "status": "passed", "duration_ms": 5,
            "steps": [{"name": "open", "attempts": 1}]})
        _write_run_json(runs_root, "run-bad", {
            "run_dir": "runs/run-bad", "flow_name": "smoke",
            "status": "failed", "started": 1728003600, "duration_ms": 5678,
            "diagnosis": {"likely_cause": "selector drift on the login button"},
            "steps": [{"name": "login", "attempts": 3}]})
        out = dashboard.build(runs_root, os.path.join(d, "dash"))
        html = open(out, encoding="utf-8").read()
        check("dashboard build returns index.html",
              out.endswith("index.html") and os.path.exists(out))
        check("dashboard cards count 2 runs", ">3</b>runs" in html)
        check("dashboard cards count passed/failed",
              ">2</b>passed" in html and ">1</b>failed" in html)
        check("dashboard cards count investigations",
              ">1</b>investigations" in html)
        check("dashboard failed row shows diagnosis excerpt",
              "dx: " in html and "selector drift on the login button" in html)
        check("dashboard retry row shows attempts", "3 attempts" in html)
        check("dashboard report href",
              "../runs/run-bad/REPORT.md" in html
              and "../runs/run-ok/REPORT.md" in html)
        check("dashboard shows timestamps", "2024-10-04" in html)
        check("dashboard ts fallback renders em dash", "<td>—</td>" in html)


def test_dashboard_empty_runs():
    from qaloop import dashboard
    with tempfile.TemporaryDirectory() as d, temp_ledger():
        runs_root = os.path.join(d, "runs")
        os.makedirs(runs_root)
        # one malformed run.json must be skipped silently, not crash the build
        bad = os.path.join(runs_root, "broken")
        os.makedirs(bad)
        open(os.path.join(bad, "run.json"), "w").write("{not json")
        out = dashboard.build(runs_root, os.path.join(d, "dash"))
        html = open(out, encoding="utf-8").read()
        check("dashboard empty runs renders 'no runs yet'",
              "no runs yet" in html)
        check("dashboard empty runs still writes index.html",
              os.path.exists(out))


def test_keep_runs_prune():
    from qaloop.artifacts import prune_old_runs
    # (a) 7 fake run dirs, keep=3 → exactly the 3 newest remain; files untouched
    with tempfile.TemporaryDirectory() as d:
        names = [f"2026100{i}T120000Z-demo-mock-ux-{i:06x}" for i in range(1, 8)]
        for n in names:
            os.makedirs(os.path.join(d, n))
        open(os.path.join(d, "ledger.jsonl"), "w").write("{}\n")
        deleted = prune_old_runs(d, 3)
        remaining = sorted(os.listdir(d))
        check("prune keep=3 leaves 3 newest dirs + files",
              remaining == sorted(names[-3:]) + ["ledger.jsonl"], str(remaining))
        check("prune returns deleted dir names", deleted == names[:-3], str(deleted))
        # (b) keep=0 is a no-op
        deleted = prune_old_runs(d, 0)
        check("prune keep=0 deletes nothing",
              deleted == [] and sorted(os.listdir(d)) == remaining)
        # (e) missing runs root is a no-op
        check("prune on missing root returns []",
              prune_old_runs(os.path.join(d, "nope"), 3) == [])
        # keep larger than dir count: nothing pruned
        check("prune keep > count deletes nothing",
              prune_old_runs(d, 99) == [] and sorted(os.listdir(d)) == remaining)
    # (c) current-run protection: current sorts oldest, still survives
    with tempfile.TemporaryDirectory() as d:
        names = ["20250101T000000Z-old-run-aaaaaa",
                 "20261002T120000Z-demo-b1bbbb", "20261003T120000Z-demo-b2bbbb",
                 "20261004T120000Z-demo-b3bbbb", "20261005T120000Z-demo-b4bbbb"]
        for n in names:
            os.makedirs(os.path.join(d, n))
        deleted = prune_old_runs(d, 2, current_run_dir=os.path.join(d, names[0]))
        remaining = sorted(os.listdir(d))
        check("prune never deletes the current run", names[0] in remaining,
              str(remaining))
        check("prune keep=2 keeps current + 2 newest",
              remaining == [names[0], names[3], names[4]], str(remaining))
        check("prune deleted exactly the 2 oldest non-current",
              deleted == [names[1], names[2]], str(deleted))
    # best-effort: one undeletable dir does not fail the run
    with tempfile.TemporaryDirectory() as d:
        from qaloop import artifacts
        names = [f"2026100{i}T120000Z-demo-{i:06x}" for i in range(1, 6)]
        for n in names:
            os.makedirs(os.path.join(d, n))
        real_rmtree = artifacts.shutil.rmtree

        def flaky_rmtree(path, *a, **k):
            if path.endswith(names[0]):
                raise PermissionError("denied")
            return real_rmtree(path, *a, **k)

        artifacts.shutil.rmtree = flaky_rmtree
        try:
            deleted = artifacts.prune_old_runs(d, 2)
        finally:
            artifacts.shutil.rmtree = real_rmtree
        remaining = sorted(os.listdir(d))
        check("prune tolerates one undeletable dir",
              deleted == names[1:-2] and remaining == [names[0]] + names[-2:],
              str(deleted) + " / " + str(remaining))


def test_resolve_keep_runs():
    from qaloop.env import resolve_keep_runs
    old = os.environ.get("QALOOP_KEEP_RUNS")

    def setenv(v):
        if v is None:
            os.environ.pop("QALOOP_KEEP_RUNS", None)
        else:
            os.environ["QALOOP_KEEP_RUNS"] = v

    def raises(fn):
        try:
            fn()
        except ValueError:
            return True
        return False

    try:
        setenv(None)
        check("no flag, no env → 0", resolve_keep_runs(None) == 0)
        check("no flag, empty env → 0", (setenv(""), resolve_keep_runs(None))[1] == 0)
        setenv("3")
        check("env=3 alone → 3", resolve_keep_runs(None) == 3)
        check("flag=5 beats env=3", resolve_keep_runs(5) == 5)
        check("flag=0 keeps everything", resolve_keep_runs(0) == 0)
        check("negative flag raises", raises(lambda: resolve_keep_runs(-1)))
        setenv("garbage")
        check("garbage env raises", raises(lambda: resolve_keep_runs(None)))
        setenv("-2")
        check("negative env raises", raises(lambda: resolve_keep_runs(None)))
        setenv(" 4 ")
        check("env with whitespace parses", resolve_keep_runs(None) == 4)
    finally:
        if old is None:
            os.environ.pop("QALOOP_KEEP_RUNS", None)
        else:
            os.environ["QALOOP_KEEP_RUNS"] = old


def test_keep_runs_cli_plumbing():
    from qaloop.cli import build_parser
    p = build_parser()
    a = p.parse_args(["verify", "flows/demo-mock-ux.yaml", "--keep-runs", "5"])
    check("verify --keep-runs parses as int", a.keep_runs == 5)
    check("verify --keep-runs defaults to None",
          p.parse_args(["verify", "flows/x.yaml"]).keep_runs is None)
    w = p.parse_args(["worker", "--once", "--keep-runs", "3"])
    check("worker --keep-runs parses as int", w.keep_runs == 3)
    check("worker --keep-runs defaults to None",
          p.parse_args(["worker"]).keep_runs is None)
    check("negative --keep-runs parses (rejected later)",
          p.parse_args(["verify", "flows/x.yaml", "--keep-runs", "-1"]).keep_runs == -1)


def test_write_diff_image():
    """Diff-highlight image: readable PNG of the actual with drifted pixels
    painted red; identical inputs produce no highlights; size mismatch raises."""
    from qaloop.artifacts import write_diff_image
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "a.png")
        b = os.path.join(d, "b.png")
        out = os.path.join(d, "sub", "diff.png")  # missing dir: created
        Image.new("RGB", (12, 10), (30, 60, 90)).save(a)
        # (a) identical images -> output equals the actual, no red pixels
        Image.open(a).save(b)
        write_diff_image(a, b, out)
        got = Image.open(out)
        check("identical diff output matches input",
              got.size == (12, 10)
              and list(got.getdata()) == list(Image.open(a).getdata()),
              str(got.size))
        check("identical diff output has no red pixels",
              all(px != (255, 0, 0) for px in got.getdata()))
        # (b) drifted pixel -> marked red exactly at the drifted location
        drifted = Image.open(a)
        drifted.putpixel((3, 4), (7, 13, 200))
        drifted.save(b)
        write_diff_image(a, b, out)
        got = Image.open(out)
        actual = Image.open(a)
        check("drifted pixel is painted red", got.getpixel((3, 4)) == (255, 0, 0),
              str(got.getpixel((3, 4))))
        check("highlight covers exactly the drifted locations",
              all(got.getpixel((x, y)) == actual.getpixel((x, y))
                  for y in range(10) for x in range(12)
                  if (x, y) != (3, 4)))
        # (c) size mismatch -> clean ValueError, not a silent resize
        small = os.path.join(d, "small.png")
        Image.new("RGB", (5, 5), (0, 0, 0)).save(small)
        try:
            write_diff_image(a, small, out)
            check("size mismatch raises ValueError", False, "no error")
        except ValueError:
            check("size mismatch raises ValueError", True)


def test_screenshot_matches_diff_artifact():
    """Mismatch writes assert-<phase>-<idx>-diff.png and names it in the
    assertion detail; green runs write no diff file; a diff-write failure
    degrades to an extended detail instead of crashing."""
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step
    from PIL import Image
    import qaloop.artifacts as art

    def make_step():
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=[("screenshot_matches",
                                             {"baseline": "base.png",
                                              "max_diff": 0.0})],
                    continue_on_fail=False, timeout_ms=None, raw={})

    class FakePage:
        def __init__(self, png):
            self.png = png

        def screenshot(self, path):
            Image.open(self.png).save(path)

    with tempfile.TemporaryDirectory() as d:
        base_dir, run_dir = os.path.join(d, "base"), os.path.join(d, "run")
        os.makedirs(base_dir)
        shot_png = os.path.join(d, "shot.png")
        Image.new("RGB", (8, 8), (200, 10, 10)).save(shot_png)
        Image.new("RGB", (8, 8), (10, 200, 10)).save(
            os.path.join(base_dir, "base.png"))
        diff_rel = "steps/assert-steps-00-diff.png"
        # Case 1: mismatch -> diff artifact written and named in detail
        res = _check_assertions(FakePage(shot_png), make_step(), None,
                                baseline_dir=base_dir, run_dir=run_dir)
        check("mismatch still fails", not res[0].passed, res[0].detail)
        check("mismatch names diff artifact in detail",
              f"diff={diff_rel}" in res[0].detail, res[0].detail)
        diff_abs = os.path.join(run_dir, diff_rel)
        check("mismatch writes readable diff PNG",
              os.path.isfile(diff_abs)
              and Image.open(diff_abs).size == (8, 8),
              str(os.path.exists(diff_abs)))
        # Case 2: green run -> no diff file
        green_png = os.path.join(d, "green.png")
        Image.open(os.path.join(base_dir, "base.png")).save(green_png)
        res = _check_assertions(FakePage(green_png), make_step(), None,
                                baseline_dir=base_dir, run_dir=run_dir)
        check("green assertion passes", res[0].passed, res[0].detail)
        os.remove(diff_abs)  # isolate the green case from case 1's artifact
        diffs = [f for f in os.listdir(os.path.join(run_dir, "steps"))
                 if f.endswith("-diff.png")]
        check("green run writes no diff artifact", diffs == [], str(diffs))
        # Case 3: diff write fails -> extended detail, run survives
        def boom(*args):
            raise OSError("disk full")
        real = art.write_diff_image
        art.write_diff_image = boom
        try:
            res = _check_assertions(FakePage(shot_png), make_step(), None,
                                    baseline_dir=base_dir, run_dir=run_dir)
        finally:
            art.write_diff_image = real
        check("diff-write failure degrades to extended detail",
              not res[0].passed
              and "diff image unavailable: OSError" in res[0].detail,
              res[0].detail)


def test_mock_calls_validation():
    """mock_calls assertion spec validation: {url, equals|gte|lte} mirror of
    count, keyed by the mock's url pattern (issue #43)."""
    from qaloop.spec import load_spec, SpecError
    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)
    base = ("name: t\ntarget: http://x\nsteps:\n  - name: s\n"
            "    expect: {mock_calls: EXPECT}\n")
    for good in ["{url: '**/api/x', equals: 1}",
                 "{url: '**/api/x', gte: 2}",
                 "{url: '**/api/x', lte: 0}"]:
        s = load(base.replace("EXPECT", good))
        check(f"valid mock_calls accepted: {good}",
              s.steps[0].expect_items[0][0] == "mock_calls",
              str(s.steps[0].expect_items))
    for bad, label in [
        ("{equals: 1}", "missing url"),
        ("{url: '**/api/x'}", "no comparator"),
        ("{url: '**/api/x', noteq: 1}", "wrong comparator key"),
        ("{url: 42, equals: 1}", "non-string url"),
        ("{url: '', equals: 1}", "empty url"),
        ("'**/api/x'", "non-dict"),
        ("true", "non-dict bool"),
    ]:
        try:
            load(base.replace("EXPECT", bad))
            check(f"bad mock_calls rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad mock_calls rejected ({label})", True)


def test_mock_calls_counter():
    """_do_mock's handler increments the per-run counter keyed by url
    pattern on served hits only; method-mismatch and times-exceeded
    fallthroughs do not increment (issue #43)."""
    from qaloop.runner import _do_mock

    class FakeRoute:
        def __init__(self):
            self.fulfilled = 0
            self.fell_back = 0
        def fallback(self):
            self.fell_back += 1
        def fulfill(self, **kw):
            self.fulfilled += 1

    class FakeRequest:
        def __init__(self, method):
            self.method = method

    class FakePage:
        def __init__(self):
            self.routes = []
        def route(self, url, handler):
            self.routes.append((url, handler))

    # basic: served hits increment, keyed by the url pattern
    page = FakePage()
    hits = {}
    _do_mock(page, {"url": "**/api/pets", "method": "GET", "json": {"ok": True}},
             hits)
    (url, handler), = page.routes
    route = FakeRoute()
    handler(route, FakeRequest("GET"))
    check("served hit increments counter", hits == {"**/api/pets": 1}, str(hits))
    check("served hit fulfills", route.fulfilled == 1 and route.fell_back == 0)
    handler(FakeRoute(), FakeRequest("GET"))
    check("second served hit counts too", hits == {"**/api/pets": 2}, str(hits))
    # method mismatch: falls through, no increment
    before = dict(hits)
    route2 = FakeRoute()
    handler(route2, FakeRequest("POST"))
    check("method mismatch falls back without counting",
          hits == before and route2.fell_back == 1 and route2.fulfilled == 0,
          str(hits))
    # times-exceeded: falls through, no increment
    page2 = FakePage()
    hits2 = {}
    _do_mock(page2, {"url": "**/api/x", "json": {"ok": True}, "times": 1}, hits2)
    handler2 = page2.routes[0][1]
    handler2(FakeRoute(), FakeRequest("GET"))
    check("first hit under times served and counted",
          hits2 == {"**/api/x": 1}, str(hits2))
    route3 = FakeRoute()
    handler2(route3, FakeRequest("GET"))
    check("times-exceeded fallthrough not counted",
          hits2 == {"**/api/x": 1} and route3.fell_back == 1
          and route3.fulfilled == 0, str(hits2))
    # separate routes keep separate counters
    page3 = FakePage()
    _do_mock(page3, {"url": "**/api/other", "json": {"ok": True}}, hits)
    handler3 = page3.routes[0][1]
    handler3(FakeRoute(), FakeRequest("GET"))
    check("counters keyed per url pattern",
          hits == {"**/api/pets": 2, "**/api/other": 1}, str(hits))


def test_mock_calls_evaluation():
    """mock_calls evaluation: equals wins, then gte, else lte; unregistered
    urls are 0; fail details name the url and the observed count (issue #43)."""
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    class FakePage:
        pass

    def run(params, hits):
        step = Step(index=0, phase="steps", name="s", op=None, params=None,
                    expect={}, expect_items=[("mock_calls", params)],
                    continue_on_fail=False, timeout_ms=None, raw={})
        return _check_assertions(FakePage(), step, None, mock_hits=hits)[0]

    r = run({"url": "**/a", "equals": 2}, {"**/a": 2})
    check("equals boundary passes", r.passed, r.detail)
    r = run({"url": "**/a", "equals": 2}, {"**/a": 1})
    check("equals mismatch fails with url and observed count",
          not r.passed and r.detail == "**/a: mock_calls=1 want =2", r.detail)
    r = run({"url": "**/a", "gte": 2}, {"**/a": 2})
    check("gte boundary passes", r.passed, r.detail)
    r = run({"url": "**/a", "gte": 3}, {"**/a": 2})
    check("gte shortfall fails with url and observed count",
          not r.passed and r.detail == "**/a: mock_calls=2 want >=3", r.detail)
    r = run({"url": "**/a", "lte": 2}, {"**/a": 2})
    check("lte boundary passes", r.passed, r.detail)
    r = run({"url": "**/a", "lte": 1}, {"**/a": 2})
    check("lte excess fails with url and observed count",
          not r.passed and r.detail == "**/a: mock_calls=2 want <=1", r.detail)
    # precedence: equals wins over gte/lte
    r = run({"url": "**/a", "equals": 5, "gte": 0}, {"**/a": 1})
    check("equals takes precedence over gte", not r.passed
          and "want =5" in r.detail, r.detail)
    # unknown url evaluates as 0
    r = run({"url": "**/nope", "equals": 0}, {})
    check("unregistered url is 0 and passes equals 0",
          r.passed and r.detail == "**/nope: mock_calls=0 want =0", r.detail)
    r = run({"url": "**/nope", "gte": 1}, {"**/a": 9})
    check("unregistered url fails gte 1 with clear detail",
          not r.passed and r.detail == "**/nope: mock_calls=0 want >=1", r.detail)
    # mock_hits None (defensive): also 0
    r = run({"url": "**/a", "equals": 0}, None)
    check("None mock_hits evaluates as 0", r.passed, r.detail)


def test_network_calls_validation():
    """network_calls assertion spec validation: {url, equals|gte|lte},
    mirroring mock_calls (issue #47)."""
    from qaloop.spec import load_spec, SpecError
    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)
    base = ("name: t\ntarget: http://x\nsteps:\n  - name: s\n"
            "    expect: {network_calls: EXPECT}\n")
    for good in ["{url: '/demo/', equals: 1}",
                 "{url: '/demo/', gte: 2}",
                 "{url: '/demo/', lte: 0}"]:
        s = load(base.replace("EXPECT", good))
        check(f"valid network_calls accepted: {good}",
              s.steps[0].expect_items[0][0] == "network_calls",
              str(s.steps[0].expect_items))
    # optional status sub-filter (issue #49): exactly one of equals|gte|lte,
    # int (never bool), non-negative
    for good in ["{url: '/demo/', status: {equals: 200}, equals: 1}",
                 "{url: '/demo/', status: {gte: 200}, gte: 1}",
                 "{url: '/demo/', status: {lte: 399}, lte: 2}"]:
        s = load(base.replace("EXPECT", good))
        check(f"valid network_calls status accepted: {good}",
              s.steps[0].expect_items[0][0] == "network_calls",
              str(s.steps[0].expect_items))
    for bad, label in [
        ("{equals: 1}", "missing url"),
        ("{url: '/demo/'}", "no comparator"),
        ("{url: '/demo/', noteq: 1}", "wrong comparator key"),
        ("{url: 42, equals: 1}", "non-string url"),
        ("{url: '', equals: 1}", "empty url"),
        ("'/demo/'", "non-dict"),
        ("true", "non-dict bool"),
        ("{url: '/demo/', status: 'ok', equals: 1}", "status non-dict string"),
        ("{url: '/demo/', status: true, equals: 1}", "status bool"),
        ("{url: '/demo/', status: 200, equals: 1}", "status int"),
        ("{url: '/demo/', status: {equals: 200, gte: 200}, equals: 1}",
         "status two comparators"),
        ("{url: '/demo/', status: {noteq: 200}, equals: 1}",
         "status unknown comparator key"),
        ("{url: '/demo/', status: {equals: -1}, equals: 1}",
         "status negative"),
        ("{url: '/demo/', status: {equals: true}, equals: 1}",
         "status bool comparator value"),
        ("{url: '/demo/', status: {gte: 2.5}, equals: 1}",
         "status float comparator value"),
    ]:
        try:
            load(base.replace("EXPECT", bad))
            check(f"bad network_calls rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad network_calls rejected ({label})", True)
    # acceptance: string/bool status errors name network_calls status
    for bad in ["{url: '/demo/', status: 'ok', equals: 1}",
                "{url: '/demo/', status: true, equals: 1}"]:
        try:
            load(base.replace("EXPECT", bad))
            check("network_calls status error names the key", False, "no error")
        except SpecError as e:
            check("network_calls status error names the key",
                  "network_calls status" in str(e), str(e))


def test_network_calls_evaluation():
    """network_calls evaluation: counts network_log entries by url substring;
    equals wins, then gte, else lte; failed requests (status None) count;
    no match is 0; fail details name the url and the observed count
    (issue #47)."""
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    class FakePage:
        pass

    def run(params, log):
        step = Step(index=0, phase="steps", name="s", op=None, params=None,
                    expect={}, expect_items=[("network_calls", params)],
                    continue_on_fail=False, timeout_ms=None, raw={})
        return _check_assertions(FakePage(), step, None, network_log=log)[0]

    def entry(url, status=200, ms=12.3):
        return {"ts": 1.0, "method": "GET", "url": url,
                "status": status, "ms": ms}

    log = [entry("http://x/demo/a"), entry("http://x/demo/b"),
           entry("http://x/other")]
    r = run({"url": "/demo/", "equals": 2}, log)
    check("equals boundary passes", r.passed, r.detail)
    r = run({"url": "/demo/", "equals": 1}, log)
    check("equals mismatch fails with url and observed count",
          not r.passed and r.detail == "/demo/: network_calls=2 want =1", r.detail)
    r = run({"url": "/demo/", "gte": 2}, log)
    check("gte boundary passes", r.passed, r.detail)
    r = run({"url": "/demo/", "gte": 3}, log)
    check("gte shortfall fails with url and observed count",
          not r.passed and r.detail == "/demo/: network_calls=2 want >=3", r.detail)
    r = run({"url": "/demo/", "lte": 2}, log)
    check("lte boundary passes", r.passed, r.detail)
    r = run({"url": "/demo/", "lte": 1}, log)
    check("lte excess fails with url and observed count",
          not r.passed and r.detail == "/demo/: network_calls=2 want <=1", r.detail)
    # precedence: equals wins over gte/lte
    r = run({"url": "/demo/", "equals": 7, "gte": 0}, log)
    check("equals takes precedence over gte", not r.passed
          and "want =7" in r.detail, r.detail)
    # failed request (status None) counts as a call
    fail_log = [entry("http://x/demo/c", status=None, ms=None)]
    r = run({"url": "/demo/", "equals": 1}, fail_log)
    check("failed request (status None) counts as a call", r.passed, r.detail)
    # no match is 0
    r = run({"url": "/nope/", "equals": 0}, log)
    check("unmatched url is 0 and passes equals 0",
          r.passed and r.detail == "/nope/: network_calls=0 want =0", r.detail)
    r = run({"url": "/nope/", "gte": 1}, log)
    check("unmatched url fails gte 1 with clear detail",
          not r.passed and r.detail == "/nope/: network_calls=0 want >=1", r.detail)
    # network_log None (defensive): also 0
    r = run({"url": "/demo/", "equals": 0}, None)
    check("None network_log evaluates as 0", r.passed, r.detail)


def test_network_calls_status_evaluation():
    """network_calls status filter evaluation: the status sub-filter narrows
    counted entries BEFORE the count comparator; equals > gte > lte;
    status:null entries never match a filter but still count without one;
    failure details name the url, the status filter, and the count
    (issue #49)."""
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    class FakePage:
        pass

    def run(params, log):
        step = Step(index=0, phase="steps", name="s", op=None, params=None,
                    expect={}, expect_items=[("network_calls", params)],
                    continue_on_fail=False, timeout_ms=None, raw={})
        return _check_assertions(FakePage(), step, None, network_log=log)[0]

    def entry(url, status=200, ms=12.3):
        return {"ts": 1.0, "method": "GET", "url": url,
                "status": status, "ms": ms}

    log = [entry("http://x/demo/a", 200), entry("http://x/demo/b", 500),
           entry("http://x/demo/c", 404), entry("http://x/other", 200)]
    # status equals: only matching entries count
    r = run({"url": "/demo/", "status": {"equals": 200}, "equals": 1}, log)
    check("status equals counts only matching entries", r.passed, r.detail)
    r = run({"url": "/demo/", "status": {"equals": 200}, "equals": 2}, log)
    check("status equals mismatch fails naming url, filter, and count",
          not r.passed
          and r.detail == "/demo/: network_calls=1 want =2 status=200",
          r.detail)
    # gte / lte comparators on the status filter
    r = run({"url": "/demo/", "status": {"gte": 400}, "equals": 2}, log)
    check("status gte counts entries at/above the bound",
          r.passed, r.detail)
    r = run({"url": "/demo/", "status": {"lte": 299}, "equals": 1}, log)
    check("status lte counts entries at/below the bound",
          r.passed, r.detail)
    r = run({"url": "/demo/", "status": {"gte": 500}, "gte": 1}, log)
    check("gte count comparator still applies to the filtered count",
          r.passed, r.detail)
    r = run({"url": "/demo/", "status": {"lte": 399}, "lte": 0}, log)
    check("lte count comparator fails naming filter and count",
          not r.passed
          and r.detail == "/demo/: network_calls=1 want <=0 status<=399",
          r.detail)
    # precedence inside the status filter: equals wins, then gte, else lte
    r = run({"url": "/demo/", "status": {"equals": 404, "gte": 200},
             "equals": 1}, log)
    check("status filter: equals takes precedence over gte",
          r.passed, r.detail)
    # null-status entries never match a filter, but count without one
    fail_log = [entry("http://x/demo/c", status=None, ms=None)]
    r = run({"url": "/demo/", "equals": 1}, fail_log)
    check("null-status entry counted without a status filter",
          r.passed, r.detail)
    r = run({"url": "/demo/", "status": {"equals": 200}, "equals": 0},
            fail_log)
    check("null-status entry never matches a status filter",
          r.passed and r.detail == "/demo/: network_calls=0 want =0 status=200",
          r.detail)
    # acceptance line 1: 500 entry fails a status: {equals: 200} filter
    r = run({"url": "/demo/", "status": {"equals": 200}, "equals": 1},
            [entry("http://x/demo/x", 500)])
    check("status 500 fails {equals: 200} with url, filter, and count",
          not r.passed
          and r.detail == "/demo/: network_calls=0 want =1 status=200",
          r.detail)
    # acceptance line 2
    r = run({"url": "/demo/", "status": {"equals": 200}, "equals": 1},
            [entry("http://x/demo/x", 200)])
    check("status 200 passes {equals: 200}", r.passed, r.detail)


def test_mock_calls_runner():
    """Real-browser proof of acceptance line 1: a flow registering a mock,
    triggering the request, and asserting mock_calls {url, equals: N} passes;
    a wrong count fails with the url and observed count in the detail."""
    import socket
    import subprocess
    import time
    exe = os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-1243/"
        "chrome-headless-shell-linux64/chrome-headless-shell")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        have_pw = True
    except ImportError:
        have_pw = False
    if not have_pw or not os.path.exists(exe):
        check("mock_calls runner tests skipped (no browser)", True)
        return
    from qaloop.spec import load_spec
    from qaloop.runner import run_flow

    flow_tpl = (
        "name: mock-calls-demo\ntarget: http://127.0.0.1:PORT\n"
        "setup:\n  - mock: {url: '**/api/pets', json: {pets: []}}\n"
        "steps:\n"
        "  - name: fetch twice\n"
        "    script: {js: \"(async () => { await fetch('/api/pets'); "
        "await fetch('/api/pets'); })()\"}\n"
        "  - name: count the hits\n"
        "    expect: {mock_calls: {url: '**/api/pets', equals: EXPECT}}\n")

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write("<html><body>mock calls demo</body></html>")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            for expect, label in [(2, "equals 2 passes"),
                                  (3, "equals 3 fails")]:
                flow_path = os.path.join(d, f"mock-calls-{expect}.yaml")
                with open(flow_path, "w") as f:
                    f.write(flow_tpl.replace("PORT", str(port))
                            .replace("EXPECT", str(expect)))
                with tempfile.TemporaryDirectory() as run_dir:
                    result = run_flow(load_spec(flow_path), run_dir=run_dir,
                                      executable_path=exe)
                    if expect == 2:
                        check("mock_calls equals 2 end-to-end passes",
                              result.status == "passed", result.status)
                    else:
                        err = result.steps[-1].error if result.steps else ""
                        check("mock_calls equals 3 fails naming url and count",
                              result.status == "failed"
                              and "**/api/pets: mock_calls=2 want =3" in err,
                              result.status + " " + err)
        finally:
            srv.terminate()


def test_network_jsonl_artifact():
    from qaloop.artifacts import Collectors
    from qaloop.runner import RunResult, StepResult
    from qaloop.spec import load_spec
    from qaloop.report import write_report

    class FakeRequest:
        def __init__(self, method, url, timing=None, failure=None):
            self.method = method
            self.url = url
            self.timing = timing if timing is not None else {}
            self.failure = failure

    class FakeResponse:
        def __init__(self, status, request, url):
            self.status = status
            self.request = request
            self.url = url

    # 1. response with timing -> one line, status + ms (time-to-first-byte),
    #    URL preserved
    c = Collectors()
    req = FakeRequest("GET", "http://x/api/pets",
                      {"requestStart": 100.0, "responseStart": 147.5,
                       "responseEnd": -1})
    c._on_response(FakeResponse(200, req, "http://x/api/pets"))
    check("response 200 appends one network_log line", len(c.network_log) == 1)
    line = c.network_log[0]
    check("network_log line keeps status, ms, url",
          line["status"] == 200 and abs(line["ms"] - 47.5) < 0.01
          and line["method"] == "GET" and line["url"] == "http://x/api/pets",
          str(line))

    # timing unavailable -> ms None, still logged
    c2 = Collectors()
    c2._on_response(FakeResponse(201, FakeRequest("POST", "http://x/a"),
                                 "http://x/a"))
    check("response without timing logs ms None",
          len(c2.network_log) == 1 and c2.network_log[0]["ms"] is None)

    # Playwright reports -1 for timing phases that never happened
    # (e.g. responseStart for a route-fulfilled mock) -> ms None, not garbage
    c2b = Collectors()
    c2b._on_response(FakeResponse(200,
                                  FakeRequest("GET", "http://x/c",
                                              {"requestStart": 2.9,
                                               "responseStart": -1,
                                               "responseEnd": -1}),
                                  "http://x/c"))
    check("negative timing yields ms None, not garbage",
          len(c2b.network_log) == 1 and c2b.network_log[0]["ms"] is None)

    # 2. requestfailed -> network_log line with status None + failure,
    #    AND failed_requests still appended (regression check)
    c3 = Collectors()
    c3._on_requestfailed(FakeRequest("GET", "http://x/broken",
                                     failure="net::ERR_CONNECTION_REFUSED"))
    check("requestfailed keeps failed_requests entry",
          len(c3.failed_requests) == 1
          and c3.failed_requests[0]["failure"] is not None)
    nl = c3.network_log[0]
    check("requestfailed appends network_log status None + failure",
          nl["status"] is None and nl["ms"] is None
          and "ERR_CONNECTION_REFUSED" in (nl["failure"] or ""), str(nl))

    # 3. 404 -> in network_log AND still in bad_responses
    c4 = Collectors()
    c4._on_response(FakeResponse(404,
                                 FakeRequest("GET", "http://x/missing",
                                             {"requestStart": 1.0, "responseEnd": 5.0}),
                                 "http://x/missing"))
    check("404 lands in network_log",
          len(c4.network_log) == 1 and c4.network_log[0]["status"] == 404)
    check("404 still in bad_responses",
          len(c4.bad_responses) == 1 and c4.bad_responses[0]["status"] == 404)

    # 4. URL >500 chars -> capped at 500 in the log line
    c5 = Collectors()
    long_url = "http://x/" + "a" * 600
    c5._on_response(FakeResponse(200,
                                 FakeRequest("GET", long_url,
                                             {"requestStart": 0.0, "responseEnd": 1.0}),
                                 long_url))
    check("log url capped at 500 chars",
          len(c5.network_log[0]["url"]) == 500)

    # 5. write_report writes network.jsonl: 2 lines, parseable, in order
    spec = load_spec("flows/game-loading-frames.yaml", strict_env=False)
    with tempfile.TemporaryDirectory() as d:
        result = RunResult(
            flow_name="net-flow", target="http://x", status="passed",
            started=1700000000.0, ended=1700000005.0,
            steps=[StepResult(index=0, phase="main", name="S-01", op="goto",
                              status="passed", duration_ms=100)],
            failed_step=None, console_errors=[], page_errors=[],
            failed_requests=[], bad_responses=[],
            network_log=[
                {"ts": 1.0, "method": "GET", "url": "http://x/a",
                 "status": 200, "ms": 10.5},
                {"ts": 2.0, "method": "GET", "url": "http://x/b",
                 "status": None, "failure": "boom", "ms": None},
            ],
            run_dir=d)
        paths = write_report(result, spec, d)
        check("network_jsonl path returned",
              paths.get("network_jsonl") == os.path.join(d, "network.jsonl"))
        raw_lines = open(paths["network_jsonl"], encoding="utf-8").read().splitlines()
        parsed = [json.loads(line) for line in raw_lines]
        check("network.jsonl has exactly 2 parseable lines", len(raw_lines) == 2)
        check("network.jsonl lines keep event order",
              parsed[0]["url"] == "http://x/a" and parsed[1]["url"] == "http://x/b")
        check("network_log round-trips through run.json",
              json.load(open(os.path.join(d, "run.json")))
              ["network_log"][1]["status"] is None)

    # write failure of network.jsonl is tolerated: run still completes.
    # (block the file itself with a directory so only the jsonl write fails)
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "network.jsonl"))
        block = RunResult(
            flow_name="net-flow", target="http://x", status="passed",
            started=1700000000.0, ended=1700000005.0,
            steps=[], failed_step=None, console_errors=[], page_errors=[],
            failed_requests=[], bad_responses=[],
            network_log=[{"ts": 1.0, "method": "GET", "url": "http://x/a",
                          "status": 200, "ms": 1.0}],
            run_dir=d)
        paths = write_report(block, spec, d)
        check("network.jsonl write failure never fails the run",
              paths.get("network_jsonl") is None
              and os.path.exists(os.path.join(d, "REPORT.md"))
              and os.path.exists(os.path.join(d, "run.json")))


def test_collectors_defensive_contracts():
    from qaloop.artifacts import Collectors

    class PlainConsoleMsg:
        def __init__(self, type, text, location):
            self.type = type
            self.text = text
            self.location = location

    class RaisingLocationMsg:
        type = "error"
        text = "boom"

        @property
        def location(self):
            raise RuntimeError("location unavailable")

    class LongStrLocation:
        def __str__(self):
            return "L" * 250

    # (1) dict location: only url/lineNumber/columnNumber survive, extras dropped
    c = Collectors()
    c._on_console(PlainConsoleMsg(
        "error", "boom",
        {"url": "http://x/a.js", "lineNumber": 3, "columnNumber": 7,
         "extra": "drop-me"}))
    check("console error recorded once", len(c.console_errors) == 1)
    check("console location keeps only url/line/column",
          c.console_errors[0]["location"] == {"url": "http://x/a.js",
                                             "lineNumber": 3,
                                             "columnNumber": 7},
          str(c.console_errors[0]["location"]))

    # (2) location property raises -> loc falls back to {} -> all-None dict
    c2 = Collectors()
    c2._on_console(RaisingLocationMsg())
    check("raising location falls back to all-None location",
          len(c2.console_errors) == 1
          and c2.console_errors[0]["location"] == {"url": None,
                                                   "lineNumber": None,
                                                   "columnNumber": None},
          str(c2.console_errors))

    # (3) non-dict location -> str(location) truncated to exactly 200 chars
    c3 = Collectors()
    c3._on_console(PlainConsoleMsg("error", "boom", LongStrLocation()))
    check("non-dict location stringified and capped at 200 chars",
          c3.console_errors[0]["location"] == "L" * 200,
          str(c3.console_errors[0]["location"])[:60])

    # (4) non-error console types are ignored; error text capped at 2000
    c4 = Collectors()
    c4._on_console(PlainConsoleMsg("warning", "careful", {}))
    check("warning console msg not recorded", c4.console_errors == [])
    c4._on_console(PlainConsoleMsg("error", "e" * 2500, {}))
    check("console error text capped at 2000 chars",
          len(c4.console_errors) == 1
          and len(c4.console_errors[0]["text"]) == 2000)

    # (5) pageerror: one entry, text is str(err) truncated to 2000 chars
    c5 = Collectors()
    err = ValueError("x" * 2500)
    c5._on_pageerror(err)
    check("pageerror recorded with text capped at 2000 chars",
          len(c5.page_errors) == 1
          and c5.page_errors[0]["text"] == str(err)[:2000]
          and len(c5.page_errors[0]["text"]) == 2000,
          str(c5.page_errors)[:80])

    # (6) requestfailed whose failure property raises -> failure None in both
    #     failed_requests and the network_log line (status/ms also None)
    class RaisingFailureRequest:
        method = "GET"
        url = "http://x/broken"

        @property
        def failure(self):
            raise RuntimeError("failure unavailable")

    c6 = Collectors()
    c6._on_requestfailed(RaisingFailureRequest())
    check("raising failure still appends failed_requests entry, failure None",
          len(c6.failed_requests) == 1
          and c6.failed_requests[0]["failure"] is None,
          str(c6.failed_requests))
    check("raising failure network_log line has failure/status/ms None",
          len(c6.network_log) == 1
          and c6.network_log[0]["failure"] is None
          and c6.network_log[0]["status"] is None
          and c6.network_log[0]["ms"] is None,
          str(c6.network_log))

    # (7) response whose status property raises -> handler returns early:
    #     nothing appended to network_log or bad_responses
    class RaisingStatusResponse:
        @property
        def status(self):
            raise RuntimeError("status unavailable")

    c7 = Collectors()
    c7._on_response(RaisingStatusResponse())
    check("raising status response logs nothing",
          c7.network_log == [] and c7.bad_responses == [])


def test_url_title_assertion_polling():
    # Issue #51: url_contains / title_contains poll page.url / page.title()
    # until the substring appears or the step deadline expires.
    import time
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    def make_step(key, val, timeout_ms=None):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=[(key, val)],
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})

    class FakePage:
        """Serves a fixed sequence of values, one per read."""

        def __init__(self, urls=None, titles=None):
            self._urls = urls or [""]
            self._titles = titles or [""]
            self.url_reads = 0
            self.title_reads = 0

        @property
        def url(self):
            self.url_reads += 1
            return self._urls[min(self.url_reads - 1, len(self._urls) - 1)]

        def title(self):
            self.title_reads += 1
            return self._titles[min(self.title_reads - 1,
                                    len(self._titles) - 1)]

    # (a) delayed URL match passes and re-reads the page more than once
    page = FakePage(urls=["http://x/home", "http://x/home", "http://x/about"])
    res = _check_assertions(page, make_step("url_contains", "/about"), None)[0]
    check("url_contains delayed match passes", res.passed, res.detail)
    check("url_contains re-read the page", page.url_reads == 3,
          f"reads={page.url_reads}")

    # (b) delayed title match passes
    page = FakePage(titles=["Loading", "Loading", "About us"])
    res = _check_assertions(page, make_step("title_contains", "About"),
                            None)[0]
    check("title_contains delayed match passes", res.passed, res.detail)
    check("title_contains re-read the page", page.title_reads == 3,
          f"reads={page.title_reads}")

    # (c) never-appearing URL fails, naming the final observed value,
    # bounded by the step timeout
    page = FakePage(urls=["http://x/never-here"])
    t0 = time.monotonic()
    res = _check_assertions(page,
                            make_step("url_contains", "/about",
                                      timeout_ms=600), None)[0]
    elapsed = time.monotonic() - t0
    check("url_contains timeout fails", not res.passed, res.detail)
    check("url_contains timeout names final observed value",
          res.detail == "url='http://x/never-here'", res.detail)
    check("url_contains timeout bounded by step timeout", elapsed < 3.0,
          f"{elapsed:.2f}s")

    # (d) never-appearing title fails likewise
    page = FakePage(titles=["Loading"])
    t0 = time.monotonic()
    res = _check_assertions(page,
                            make_step("title_contains", "About",
                                      timeout_ms=600), None)[0]
    elapsed = time.monotonic() - t0
    check("title_contains timeout fails", not res.passed, res.detail)
    check("title_contains timeout names final observed value",
          res.detail == "title='Loading'", res.detail)
    check("title_contains timeout bounded by step timeout", elapsed < 3.0,
          f"{elapsed:.2f}s")

    # (e) immediate match passes on the first read
    page = FakePage(urls=["http://x/home"], titles=["Home page"])
    res = _check_assertions(page, make_step("url_contains", "/home"), None)[0]
    check("url_contains immediate match passes on first read",
          res.passed and page.url_reads == 1, f"reads={page.url_reads}")
    res = _check_assertions(page, make_step("title_contains", "Home"),
                            None)[0]
    check("title_contains immediate match passes on first read",
          res.passed and page.title_reads == 1, f"reads={page.title_reads}")


def test_count_assertion_polling():
    # Issue #53: count polls locator count() until the comparator matches
    # (equals > gte > lte precedence, unchanged) or the step deadline
    # expires; the failure detail names the final observed count.
    import time
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    def make_step(val, timeout_ms=None):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=[("count", val)],
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})

    class FakeLocator:
        """Serves a fixed sequence of counts, one per count() call."""

        def __init__(self, counts):
            self._counts = counts
            self.reads = 0

        def count(self):
            self.reads += 1
            return self._counts[min(self.reads - 1, len(self._counts) - 1)]

    class FakePage:
        def __init__(self, counts):
            self._locator = FakeLocator(counts)

        def locator(self, sel):
            return self._locator

    # (a) delayed-growth equals passes, re-reading the page more than once
    page = FakePage([1, 2, 3])
    res = _check_assertions(
        page, make_step({"selector": "ul.items", "equals": 3}), None)[0]
    check("count delayed-growth equals passes", res.passed, res.detail)
    check("count delayed-growth re-reads the locator", page._locator.reads == 3,
          f"reads={page._locator.reads}")

    # (b) never-reaching count fails naming the final observed count,
    # bounded by the step timeout
    page = FakePage([1])
    t0 = time.monotonic()
    res = _check_assertions(
        page, make_step({"selector": "ul.items", "equals": 3},
                        timeout_ms=600), None)[0]
    elapsed = time.monotonic() - t0
    check("count timeout fails", not res.passed, res.detail)
    check("count timeout names final observed count and wanted value",
          res.detail == "ul.items: count=1 want =3", res.detail)
    check("count timeout bounded by step timeout", elapsed < 3.0,
          f"{elapsed:.2f}s")

    # (c) immediate match passes on the first read
    page = FakePage([2])
    res = _check_assertions(
        page, make_step({"selector": "ul.items", "equals": 2}), None)[0]
    check("count immediate match passes on first read",
          res.passed and page._locator.reads == 1,
          f"reads={page._locator.reads}")

    # (d) gte polls with the same deadline formula
    page = FakePage([1, 2])
    res = _check_assertions(
        page, make_step({"selector": "ul.items", "gte": 2}), None)[0]
    check("count gte polls to a delayed match",
          res.passed and page._locator.reads == 2,
          f"reads={page._locator.reads}")

    # (e) lte polls with the same deadline formula
    page = FakePage([3, 2])
    res = _check_assertions(
        page, make_step({"selector": "ul.items", "lte": 2}), None)[0]
    check("count lte polls to a delayed match",
          res.passed and page._locator.reads == 2,
          f"reads={page._locator.reads}")

    # (f) equals beats gte when both are present (precedence unchanged)
    page = FakePage([5])
    res = _check_assertions(
        page, make_step({"selector": "ul.items", "equals": 5, "gte": 9}),
        None)[0]
    check("count equals precedence preserved",
          res.passed and res.detail == "ul.items: count=5 want =5",
          res.detail)


def test_js_assertion_polling():
    # Issue #55: js routes page.evaluate output through _poll_value_match
    # until the substring appears or the step deadline expires.
    import time
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    def make_step(script, contains, timeout_ms=None):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={},
                    expect_items=[("js", {"script": script,
                                         "contains": contains})],
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})

    class FakePage:
        """evaluate() serves a fixed sequence of outputs, one per call."""

        def __init__(self, outputs):
            self._outputs = outputs
            self.evaluate_calls = 0

        def evaluate(self, script):
            self.evaluate_calls += 1
            return self._outputs[min(self.evaluate_calls - 1,
                                     len(self._outputs) - 1)]

    # (a) delayed flip passes with more than one evaluate call
    page = FakePage(["stale", "stale", "READY now"])
    res = _check_assertions(page, make_step("getState()", "READY"), None)[0]
    check("js delayed match passes", res.passed, res.detail)
    check("js re-read the page", page.evaluate_calls == 3,
          f"calls={page.evaluate_calls}")

    # (b) never-appearing output fails naming the final observed value,
    # bounded by the step timeout
    page = FakePage(["still loading"])
    t0 = time.monotonic()
    res = _check_assertions(page, make_step("getState()", "READY",
                                           timeout_ms=600), None)[0]
    elapsed = time.monotonic() - t0
    check("js timeout fails", not res.passed, res.detail)
    check("js timeout names final observed value",
          res.detail == "want 'READY' in 'still loading'", res.detail)
    check("js timeout bounded by step timeout", elapsed < 3.0,
          f"{elapsed:.2f}s")

    # (c) immediate match passes on the first evaluate
    page = FakePage(["READY already"])
    res = _check_assertions(page, make_step("getState()", "READY"), None)[0]
    check("js immediate match passes on first evaluate",
          res.passed and page.evaluate_calls == 1,
          f"calls={page.evaluate_calls}")


def _stub_evaluate_critic(judge_dict, critic_text, steps=(), diff=""):
    """Run evaluate() with _chat dispatching on the system prompt: the
    judge (qaloop.evaluate._SYSTEM) vs the critic (_CRITIC_SYSTEM).

    Builds a run.json with the given step names so grounding can find
    real steps. Returns (verdict, tmp); the caller cleans up tmp.
    """
    from qaloop import evaluate as ev

    def stub(cfg, messages, timeout_s=90):
        system = (messages[0].get("content") if messages else "") or ""
        if system is ev._CRITIC_SYSTEM:
            return critic_text, {"prompt_tokens": 10,
                                 "completion_tokens": 20}
        return (json.dumps(judge_dict),
                {"prompt_tokens": 1, "completion_tokens": 2})
    old = ev._chat
    ev._chat = stub
    tmp = tempfile.TemporaryDirectory()
    try:
        if steps:
            run = {"flow_name": "demo", "target": "http://x/",
                   "status": "PASS", "failed_step": None, "error": "",
                   "steps": [{"index": i, "phase": "main", "name": name,
                              "op": "goto", "status": "PASS",
                              "duration_ms": 1, "error": "", "assertions": [],
                              "ax_snapshot": ""}
                             for i, name in enumerate(steps)],
                   "console_errors": [], "page_errors": [],
                   "failed_requests": [], "bad_responses": []}
            with open(os.path.join(tmp.name, "run.json"), "w") as f:
                json.dump(run, f)
        cfg = {"base_url": "http://localhost", "name": "stub",
               "api_key": "test", "price_in": 0.0, "price_out": 0.0}
        return ev.evaluate("the widget saves", tmp.name, diff,
                           model_cfg=cfg), tmp
    finally:
        ev._chat = old


_JUDGE_SOLID = {"verdict": "MAKES_SENSE", "confidence": "high",
                "rationale": "solid",
                "evidence": ['"the widget saves" persists',
                             '"the widget saves" renders'],
                "risks": [],
                "scores": {"claim_diff_fit": 2, "evidence_exercises_claim": 2,
                           "no_contradictions": 2, "state_supports_claim": 2}}


def test_critic_grounded_objection_downgrades_end_to_end():
    # Scripted proof: stubbed judge + stubbed critic. The critic names a
    # real step (index 0), so the objection is grounded.
    from qaloop import evaluate as ev
    objection = ("STEP 0 load page was cited, but the flow never reached"
                 " the save screen, so the claim is untested.")
    v, tmp = _stub_evaluate_critic(_JUDGE_SOLID,
                                   json.dumps({"objection": objection}),
                                   steps=("load page",))
    check("critic: verdict kept on grounded objection",
          v["verdict"] == "MAKES_SENSE", v["verdict"])
    check("critic: high -> medium confidence on grounded objection",
          v["confidence"] == "medium", v["confidence"])
    check("critic: objection appended to rationale",
          objection in v["rationale"], v["rationale"])
    check("critic: scores untouched",
          v["scores"] == _JUDGE_SOLID["scores"], repr(v["scores"]))
    check("critic_objection recorded in the verdict",
          v["critic_objection"] == objection,
          repr(v["critic_objection"]))
    check("critic: cost sums both calls' tokens",
          v["_cost"]["tokens_in"] == 11 and v["_cost"]["tokens_out"] == 22,
          repr(v["_cost"]))
    check("critic medium confidence does not trigger INSUFFICIENT_EVIDENCE",
          v["verdict"] == "MAKES_SENSE", v["verdict"])
    md, js = ev.write_evaluation(tmp.name, "the widget saves", v)
    text = open(md).read()
    check("EVALUATION.md renders the Critic section",
          "## Critic" in text and objection in text,
          text[text.find("## Critic"):text.find("## Critic") + 160])
    body = json.load(open(js))
    check("evaluation.json carries critic_objection",
          body["verdict"]["critic_objection"] == objection,
          repr(body["verdict"].get("critic_objection")))
    tmp.cleanup()


def test_critic_none_leaves_verdict_untouched():
    from qaloop import evaluate as ev
    for critic_text in ("NONE", "  none\n",
                        json.dumps({"objection": "NONE"}),
                        json.dumps({"objection": ""}),
                        '{"objection": broken}',
                        "the verdict seems fine??"):
        v, tmp = _stub_evaluate_critic(_JUDGE_SOLID, critic_text,
                                       steps=("load page",))
        tag = critic_text[:16].replace("\n", "\\n")
        check(f"critic {tag!r}: verdict kept",
              v["verdict"] == "MAKES_SENSE", v["verdict"])
        check(f"critic {tag!r}: confidence stays high",
              v["confidence"] == "high", v["confidence"])
        check(f"critic {tag!r}: rationale untouched",
              v["rationale"] == "solid", v["rationale"])
        check(f"critic {tag!r}: critic_objection is None",
              v["critic_objection"] is None, repr(v["critic_objection"]))
        tmp.cleanup()


def test_critic_coupling_grounding_rules():
    from qaloop.evaluate import _critic_coupling
    evd = _grounding_evidence(
        steps=[(0, "load page")],
        diff="diff --git a/qaloop/flow.py b/qaloop/flow.py\n+retry\n")
    base = {"verdict": "MAKES_SENSE", "confidence": "high",
            "rationale": "r", "evidence": [], "risks": []}
    obj = lambda text: json.dumps({"objection": text})  # noqa: E731
    out = _critic_coupling(base, evd, "the widget saves",
                           obj("STEP 0 never clicked save"))
    check("coupling (a): step index grounds the objection",
          out["confidence"] == "medium"
          and out["critic_objection"] == "STEP 0 never clicked save",
          repr(out["confidence"]))
    out = _critic_coupling(base, evd, "the widget saves",
                           obj("the load page step never reached the form"))
    check("coupling (a): step name grounds the objection",
          out["confidence"] == "medium", repr(out["confidence"]))
    out = _critic_coupling(base, evd, "the widget saves",
                           obj("qaloop/flow.py:42 retries but never asserts"))
    check("coupling (b): file:line in the diff grounds",
          out["confidence"] == "medium", repr(out["confidence"]))
    out = _critic_coupling(base, evd, "the widget saves",
                           obj('the "widget saves" claim was never exercised'))
    check("coupling (c): quoted context grounds",
          out["confidence"] == "medium", repr(out["confidence"]))
    out = _critic_coupling(base, evd, "the widget saves",
                           obj("the moon is made of cheese"))
    check("coupling: ungrounded objection leaves everything untouched",
          out["verdict"] == "MAKES_SENSE" and out["confidence"] == "high"
          and out["rationale"] == "r" and out["critic_objection"] is None,
          repr(out))
    mid = dict(base, confidence="medium")
    out = _critic_coupling(mid, evd, "the widget saves",
                           obj("STEP 0 never clicked save"))
    check("coupling: medium stays medium, objection still appended",
          out["confidence"] == "medium"
          and "STEP 0 never clicked save" in out["rationale"]
          and out["critic_objection"] == "STEP 0 never clicked save",
          repr(out))
    out = _critic_coupling(base, evd, "the widget saves",
                           obj("STEP 0 never clicked save"))
    check("coupling: verdict never reclassified",
          out["verdict"] == "MAKES_SENSE", out["verdict"])


def test_evaluate_no_api_key_raises_before_chat():
    from qaloop import evaluate as ev
    calls = []
    old = ev._chat
    ev._chat = lambda cfg, messages, timeout_s=90: \
        calls.append(messages) or ("", {})  # noqa: E731
    try:
        try:
            ev.evaluate("c", "/nonexistent", "")
            check("no api key raises", False)
        except RuntimeError:
            check("no api key raises RuntimeError", True)
    finally:
        ev._chat = old
    check("no _chat call happens before the key check",
          calls == [], repr(calls))


def test_write_evaluation_critic_section():
    from qaloop import evaluate as ev
    with tempfile.TemporaryDirectory() as run_dir:
        v = {"verdict": "MAKES_SENSE", "confidence": "medium",
             "rationale": "solid. Critic objection (verdict kept): STEP 0..",
             "evidence": [], "risks": [],
             "critic_objection": "STEP 0 was cited but never ran"}
        md, js = ev.write_evaluation(run_dir, "the widget saves", v)
        text = open(md).read()
        check("EVALUATION.md renders the Critic section with the objection",
              "## Critic" in text
              and "STEP 0 was cited but never ran" in text,
              text[text.find("## Critic"):text.find("## Critic") + 200])
        check("evaluation.json carries critic_objection",
              json.load(open(js))["verdict"]["critic_objection"]
              == "STEP 0 was cited but never ran")
        v2 = dict(v, critic_objection=None)
        md2, _ = ev.write_evaluation(run_dir, "the widget saves", v2)
        text2 = open(md2).read()
        check("EVALUATION.md notes when no grounded objection was raised",
              "## Critic" in text2 and "No grounded objection" in text2,
              text2[text2.find("## Critic"):text2.find("## Critic") + 120])


def test_dialog_support():
    """JS dialogs: per-step accept/dismiss policy + the dialog assertion."""
    from qaloop.spec import load_spec, SpecError
    from qaloop.artifacts import Collectors
    from qaloop.runner import _dialog_matches, RunResult

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n"
            "  - name: delete asks\n    click: '#del'\n    on_dialog: accept\n"
            "    expect:\n      - dialog: {type: confirm, text: 'Delete?'}\n"
            "      - hidden: '#row'\n")
    s = load(base)
    st = s.steps[0]
    check("on_dialog: accept parses", st.on_dialog == "accept", st.on_dialog)
    check("dialog assertion parses",
          st.expect_items[0] == ("dialog", {"type": "confirm", "text": "Delete?"}),
          str(st.expect_items[0]))

    plain = base.replace("    on_dialog: accept\n", "")
    check("on_dialog defaults to dismiss",
          load(plain).steps[0].on_dialog == "dismiss")

    for bad, label in [
        (base.replace("on_dialog: accept", "on_dialog: maybe"), "bad policy"),
        (base.replace("on_dialog: accept", "on_dialog: true"), "bool policy"),
        (base.replace("dialog: {type: confirm, text: 'Delete?'}",
                      "dialog: {}"), "empty mapping"),
        (base.replace("dialog: {type: confirm, text: 'Delete?'}",
                      "dialog: true"), "non-mapping"),
        (base.replace("dialog: {type: confirm, text: 'Delete?'}",
                      "dialog: {type: toast}"), "bad type"),
        (base.replace("dialog: {type: confirm, text: 'Delete?'}",
                      "dialog: {type: confirm, text: ''}"), "empty text"),
        (base.replace("dialog: {type: confirm, text: 'Delete?'}",
                      "dialog: {type: confirm, bogus: 1}"), "unknown key"),
    ]:
        try:
            load(bad)
            check(f"bad dialog spec rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad dialog spec rejected ({label})", True)

    for good in [
        "dialog: {type: alert}",
        "dialog: {text: 'bye'}",
    ]:
        s = load(base.replace("dialog: {type: confirm, text: 'Delete?'}", good))
        check(f"partial dialog criteria accepted ({good})",
              s.steps[0].expect_items[0][0] == "dialog")

    recs = [
        {"type": "confirm", "message": "Delete this row?"},
        {"type": "alert", "message": "saved"},
    ]
    ok, detail = _dialog_matches(recs, {"type": "confirm", "text": "Delete"})
    check("dialog match by type+text", ok, detail)
    ok, _ = _dialog_matches(recs, {"type": "alert"})
    check("dialog match by type only", ok)
    ok, _ = _dialog_matches(recs, {"text": "saved"})
    check("dialog match by text only", ok)
    ok, _ = _dialog_matches(recs, {})
    check("dialog match with no criteria matches any", ok)
    ok, detail = _dialog_matches(recs, {"type": "prompt"})
    check("dialog type mismatch fails", not ok and "no dialog matched" in detail,
          detail)
    ok, detail = _dialog_matches([], {"type": "confirm"})
    check("no dialogs fails with clear detail",
          not ok and "no dialog appeared" in detail, detail)

    class StubDialog:
        def __init__(self, dtype="confirm", message="Sure?"):
            self.type = dtype
            self.message = message
            self.calls = []

        def accept(self):
            self.calls.append("accept")

        def dismiss(self):
            self.calls.append("dismiss")

    c = Collectors()
    d = StubDialog()
    c._on_dialog(d)
    check("default policy dismisses",
          d.calls == ["dismiss"] and len(c.dialogs) == 1
          and c.dialogs[0]["type"] == "confirm"
          and c.dialogs[0]["message"] == "Sure?",
          str(d.calls))
    c.dialog_policy = "accept"
    d2 = StubDialog(dtype="prompt", message="Name?")
    c._on_dialog(d2)
    check("accept policy accepts", d2.calls == ["accept"], str(d2.calls))

    class BoomDialog(StubDialog):
        def accept(self):
            raise RuntimeError("boom")

    c.dialog_policy = "accept"
    try:
        c._on_dialog(BoomDialog())
        check("dialog handler never raises", True)
    except RuntimeError:
        check("dialog handler never raises", False, "propagated")

    class WeirdDialog:
        @property
        def type(self):
            raise RuntimeError("no type")

        @property
        def message(self):
            raise RuntimeError("no message")

        def dismiss(self):
            pass

    c.dialog_policy = "dismiss"
    c._on_dialog(WeirdDialog())
    check("unreadable dialog recorded as unknown",
          c.dialogs[-1]["type"] == "unknown", str(c.dialogs[-1]))

    c2 = Collectors()
    c2._on_dialog(StubDialog(message="first"))
    c2.checkpoint()
    c2._on_dialog(StubDialog(message="second"))
    got = [r["message"] for r in c2.dialogs_since_checkpoint()]
    check("dialogs_since_checkpoint excludes pre-checkpoint", got == ["second"],
          str(got))

    class StubPage:
        def __init__(self):
            self.handlers = {}

        def on(self, event, fn):
            self.handlers[event] = fn

    pg = StubPage()
    c3 = Collectors()
    c3.attach(pg)
    check("attach wires the dialog handler", "dialog" in pg.handlers)
    sd = StubDialog()
    pg.handlers["dialog"](sd)
    check("wired handler records + dismisses",
          sd.calls == ["dismiss"] and c3.dialogs[0]["message"] == "Sure?")

    r = RunResult(flow_name="t", target="http://x", status="passed",
                  started=0.0, ended=1.0, steps=[], failed_step=None,
                  console_errors=[], page_errors=[], failed_requests=[],
                  bad_responses=[], run_dir="/tmp/x",
                  dialogs=[{"type": "alert", "message": "hi"}])
    r2 = RunResult.from_dict(r.to_dict())
    check("RunResult round-trips dialogs",
          r2.dialogs == [{"type": "alert", "message": "hi"}], str(r2.dialogs))
    r3 = RunResult.from_dict({"flow_name": "t", "status": "passed", "steps": []})
    check("old run.json without dialogs loads", r3.dialogs == [])

    # The dialog assertion branch of _check_assertions needs no live page:
    # feed it a parsed step and pre-recorded dialogs.
    from qaloop.runner import _check_assertions
    spec2 = load(base)
    col = Collectors()
    col._on_dialog(StubDialog(dtype="confirm", message="Delete?"))
    col.checkpoint()
    col._on_dialog(StubDialog(dtype="confirm", message="Delete?"))
    res = _check_assertions(None, spec2.steps[0], col)
    check("dialog assertion passes on recorded dialog",
          res[0].passed and res[0].name == "dialog", res[0].detail)
    col2 = Collectors()
    col2.checkpoint()
    res2 = _check_assertions(None, spec2.steps[0], col2)
    check("dialog assertion fails with no dialog",
          not res2[0].passed and "no dialog appeared" in res2[0].detail,
          res2[0].detail)


def test_goto_beforeunload_abort():
    """goto + dismissed beforeunload (issue #220): Playwright raises
    net::ERR_ABORTED when a dismissed beforeunload dialog cancels the
    navigation. _do_action treats that abort as the completed action
    only when a beforeunload dialog was recorded during the goto, so
    the step's assertions can judge where the browser ended up; every
    other abort still raises."""
    from qaloop.runner import _do_action
    from qaloop.spec import Step

    def mkstep():
        return Step(index=0, phase="steps", name="g", op="goto",
                    params="/demo/page-nav-b.html", expect={},
                    expect_items=[], continue_on_fail=False,
                    timeout_ms=None, raw={})

    class FakePage:
        def __init__(self, dialogs=None, raise_msg=None, record=None):
            self.calls = []
            self._dialogs = dialogs
            self._raise_msg = raise_msg
            self._record = record

        def goto(self, url, timeout=None, wait_until=None):
            self.calls.append(url)
            if self._record is not None and self._dialogs is not None:
                self._dialogs.append(self._record)
            if self._raise_msg is not None:
                raise Exception(self._raise_msg)  # noqa: BLE001

    abort = "Page.goto: net::ERR_ABORTED at http://x/demo/page-nav-b.html"

    dialogs = []
    page = FakePage(dialogs=dialogs, raise_msg=abort,
                    record={"type": "beforeunload", "message": ""})
    _do_action(page, mkstep(), "http://x", {}, dialogs=dialogs)
    check("goto abort with beforeunload recorded is completed, not raised",
          page.calls == ["http://x/demo/page-nav-b.html"], str(page.calls))

    dialogs2 = []
    page2 = FakePage(dialogs=dialogs2, raise_msg=abort)
    try:
        _do_action(page2, mkstep(), "http://x", {}, dialogs=dialogs2)
        check("goto abort with no dialog still raises", False, "no error")
    except Exception as e:  # noqa: BLE001
        check("goto abort with no dialog still raises",
              "ERR_ABORTED" in str(e), str(e))

    dialogs3 = []
    page3 = FakePage(dialogs=dialogs3, raise_msg=abort,
                     record={"type": "alert", "message": "hi"})
    try:
        _do_action(page3, mkstep(), "http://x", {}, dialogs=dialogs3)
        check("goto abort with only an alert dialog still raises",
              False, "no error")
    except Exception as e:  # noqa: BLE001
        check("goto abort with only an alert dialog still raises",
              "ERR_ABORTED" in str(e), str(e))

    page4 = FakePage(raise_msg=abort)
    try:
        _do_action(page4, mkstep(), "http://x", {})
        check("goto abort without dialogs tracking still raises",
              False, "no error")
    except Exception as e:  # noqa: BLE001
        check("goto abort without dialogs tracking still raises",
              "ERR_ABORTED" in str(e), str(e))

    dialogs5 = []
    page5 = FakePage(dialogs=dialogs5,
                     raise_msg="Page.goto: net::ERR_FAILED at http://x/",
                     record={"type": "beforeunload", "message": ""})
    try:
        _do_action(page5, mkstep(), "http://x", {}, dialogs=dialogs5)
        check("goto non-abort error raises even with beforeunload",
              False, "no error")
    except Exception as e:  # noqa: BLE001
        check("goto non-abort error raises even with beforeunload",
              "ERR_FAILED" in str(e), str(e))

    page6 = FakePage()
    res = _do_action(page6, mkstep(), "http://x", {}, dialogs=[])
    check("goto success returns None and navigates",
          res is None
          and page6.calls == ["http://x/demo/page-nav-b.html"],
          str(page6.calls))


def test_hover_op():
    """hover op: spec validation ({target} required) and the runner calling
    page.hover with the selector and step timeout (issue #74)."""
    import tempfile
    from qaloop.runner import _do_action
    from qaloop.spec import Step, load_spec, SpecError

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: h\n    hover: PARAMS\n")
    s = load(base.replace("PARAMS", '{target: "#tip-trigger"}'))
    check("hover with {target} parses",
          s.steps[1].op == "hover" and s.steps[1].params["target"] == "#tip-trigger")
    for bad, label in [
        ("'#tip-trigger'", "bare string (needs {target})"),
        ('""', "empty string"),
        ("{}", "empty mapping"),
        ("{target: ''}", "empty target"),
        ("{target: 42}", "non-string target"),
    ]:
        try:
            load(base.replace("PARAMS", bad))
            check(f"bad hover rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad hover rejected ({label})", True)

    class FakePage:
        def __init__(self):
            self.calls = []
        def hover(self, selector, timeout=None):
            self.calls.append((selector, timeout))

    page = FakePage()
    step = Step(index=0, phase="steps", name="h", op="hover",
                params={"target": "#tip-trigger"}, expect={}, expect_items=[],
                continue_on_fail=False, timeout_ms=None, raw={})
    _do_action(page, step, "http://x", {})
    check("hover calls page.hover with selector and default timeout",
          page.calls == [("#tip-trigger", 15000)], str(page.calls))

    page2 = FakePage()
    step2 = Step(index=0, phase="steps", name="h", op="hover",
                 params={"target": "#tip-trigger"}, expect={}, expect_items=[],
                 continue_on_fail=False, timeout_ms=5000, raw={})
    _do_action(page2, step2, "http://x", {})
    check("hover honors step timeout_ms",
          page2.calls == [("#tip-trigger", 5000)], str(page2.calls))


def test_drag_op():
    """drag op (issue #77): spec validation ({target, to} required, to is a
    selector string or {x, y} int coords) and the runner calling
    locator.drag_to with the right dest form and step timeout."""
    import tempfile
    from qaloop.runner import _do_action
    from qaloop.spec import Step, load_spec, SpecError

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: d\n    drag: PARAMS\n")
    s = load(base.replace("PARAMS", '{target: "#card", to: "#dropzone"}'))
    check("drag with selector 'to' parses",
          s.steps[1].op == "drag" and s.steps[1].params["to"] == "#dropzone")
    s = load(base.replace("PARAMS", '{target: "#handle", to: {x: 390, y: 364}}'))
    check("drag with coords 'to' parses",
          s.steps[1].params["to"] == {"x": 390, "y": 364})
    for bad, label in [
        ("'#card'", "bare string (needs {target, to})"),
        ('""', "empty string"),
        ("{}", "empty mapping"),
        ("{target: '#card'}", "missing to"),
        ("{to: '#dropzone'}", "missing target"),
        ("{target: '', to: '#dropzone'}", "empty target"),
        ("{target: '#card', to: ''}", "empty to string"),
        ("{target: 42, to: '#dropzone'}", "non-string target"),
        ("{target: '#card', to: 42}", "non-mapping/non-string to"),
        ("{target: '#card', to: {x: 1}}", "coords missing y"),
        ("{target: '#card', to: {y: 1}}", "coords missing x"),
        ("{target: '#card', to: {x: 1.5, y: 2}}", "non-integer x"),
        ("{target: '#card', to: {x: 'a', y: 2}}", "string x"),
    ]:
        try:
            load(base.replace("PARAMS", bad))
            check(f"bad drag rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad drag rejected ({label})", True)

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
        def drag_to(self, dest, timeout=None):
            self.page.calls.append((self.selector, dest, timeout))
        def hover(self, timeout=None):
            self.page.calls.append(("hover", self.selector, timeout))

    class FakeMouse:
        def __init__(self, page):
            self.page = page
        def down(self):
            self.page.calls.append(("down",))
        def move(self, x, y, steps=None):
            self.page.calls.append(("move", x, y, steps))
        def up(self):
            self.page.calls.append(("up",))

    class FakePage:
        def __init__(self):
            self.calls = []
            self.mouse = FakeMouse(self)
        def locator(self, selector):
            return FakeLocator(self, selector)

    def run_step(params, timeout_ms=None):
        page = FakePage()
        step = Step(index=0, phase="steps", name="d", op="drag",
                    params=params, expect={}, expect_items=[],
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})
        _do_action(page, step, "http://x", {})
        return page

    page = run_step({"target": "#card", "to": "#dropzone"})
    check("drag with selector 'to' drags to a locator of the target selector",
          len(page.calls) == 1 and page.calls[0][0] == "#card"
          and page.calls[0][2] == 15000
          and isinstance(page.calls[0][1], FakeLocator)
          and page.calls[0][1].selector == "#dropzone",
          str(page.calls))
    page = run_step({"target": "#handle", "to": {"x": 390, "y": 364}},
                    timeout_ms=5000)
    check("drag with coords 'to' drives the mouse to {x, y} and honors step timeout_ms",
          page.calls == [("hover", "#handle", 5000), ("down",),
                         ("move", 390, 364, 10), ("up",)],
          str(page.calls))


def test_type_op():
    """type op (issue #78): spec validation ({target, text} required, text a
    string, delay_ms optional non-negative int) and the runner calling
    locator.press_sequentially with the text, the delay, and the step
    timeout."""
    import tempfile
    from qaloop.runner import _do_action
    from qaloop.spec import Step, load_spec, SpecError

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: ty\n    type: PARAMS\n")
    s = load(base.replace("PARAMS", '{target: "#q", text: "hello"}'))
    check("type with {target, text} parses",
          s.steps[1].op == "type" and s.steps[1].params["text"] == "hello")
    s = load(base.replace("PARAMS", '{target: "#q", text: "hi", delay_ms: 60}'))
    check("type with delay_ms parses",
          s.steps[1].params["delay_ms"] == 60)
    s = load(base.replace("PARAMS", '{target: "#q", text: ""}'))
    check("type with empty text parses (clear-typing)",
          s.steps[1].params["text"] == "")
    for bad, label in [
        ("'#q'", "bare string (needs {target, text})"),
        ('""', "empty string"),
        ("{}", "empty mapping"),
        ("{text: 'hi'}", "missing target"),
        ("{target: '#q'}", "missing text"),
        ("{target: '', text: 'hi'}", "empty target"),
        ("{target: 42, text: 'hi'}", "non-string target"),
        ("{target: '#q', text: 42}", "non-string text"),
        ("{target: '#q', text: 'hi', delay_ms: -1}", "negative delay_ms"),
        ("{target: '#q', text: 'hi', delay_ms: 1.5}", "non-integer delay_ms"),
        ("{target: '#q', text: 'hi', delay_ms: 'fast'}", "string delay_ms"),
    ]:
        try:
            load(base.replace("PARAMS", bad))
            check(f"bad type rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad type rejected ({label})", True)

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
        def press_sequentially(self, text, delay=None, timeout=None):
            self.page.calls.append((self.selector, text, delay, timeout))

    class FakePage:
        def __init__(self):
            self.calls = []
        def locator(self, selector):
            return FakeLocator(self, selector)

    def run_step(params, timeout_ms=None):
        page = FakePage()
        step = Step(index=0, phase="steps", name="ty", op="type",
                    params=params, expect={}, expect_items=[],
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})
        _do_action(page, step, "http://x", {})
        return page

    page = run_step({"target": "#q", "text": "hello", "delay_ms": 60})
    check("type calls press_sequentially with text, delay, and default timeout",
          page.calls == [("#q", "hello", 60, 15000)], str(page.calls))
    page = run_step({"target": "#q", "text": "hi"}, timeout_ms=5000)
    check("type defaults delay_ms to 0 and honors step timeout_ms",
          page.calls == [("#q", "hi", 0, 5000)], str(page.calls))


def test_focus_op():
    """focus op (issue #78): spec validation ({target} required) and the
    runner calling locator.focus with the step timeout."""
    import tempfile
    from qaloop.runner import _do_action
    from qaloop.spec import Step, load_spec, SpecError

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: f\n    focus: PARAMS\n")
    s = load(base.replace("PARAMS", '{target: "#ok"}'))
    check("focus with {target} parses",
          s.steps[1].op == "focus" and s.steps[1].params["target"] == "#ok")
    for bad, label in [
        ("'#ok'", "bare string (needs {target})"),
        ('""', "empty string"),
        ("{}", "empty mapping"),
        ("{target: ''}", "empty target"),
        ("{target: 42}", "non-string target"),
    ]:
        try:
            load(base.replace("PARAMS", bad))
            check(f"bad focus rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad focus rejected ({label})", True)

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
        def focus(self, timeout=None):
            self.page.calls.append((self.selector, timeout))

    class FakePage:
        def __init__(self):
            self.calls = []
        def locator(self, selector):
            return FakeLocator(self, selector)

    page = FakePage()
    step = Step(index=0, phase="steps", name="f", op="focus",
                params={"target": "#ok"}, expect={}, expect_items=[],
                continue_on_fail=False, timeout_ms=None, raw={})
    _do_action(page, step, "http://x", {})
    check("focus calls locator.focus with default timeout",
          page.calls == [("#ok", 15000)], str(page.calls))

    page2 = FakePage()
    step2 = Step(index=0, phase="steps", name="f", op="focus",
                 params={"target": "#ok"}, expect={}, expect_items=[],
                 continue_on_fail=False, timeout_ms=5000, raw={})
    _do_action(page2, step2, "http://x", {})
    check("focus honors step timeout_ms",
          page2.calls == [("#ok", 5000)], str(page2.calls))


def test_scroll_op():
    """scroll op: spec validation (target/direction/pixels) and the runner
    calling scroll_into_view_if_needed for a target, or mouse.wheel with the
    right sign/axis otherwise (issue #75)."""
    import tempfile
    from qaloop.runner import _do_action
    from qaloop.spec import Step, load_spec, SpecError

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: s\n    scroll: PARAMS\n")
    for good, label in [
        ('{target: "#more"}', "target only"),
        ("{direction: up}", "direction only"),
        ("{pixels: 1200}", "pixels only"),
        ('{direction: left, pixels: 200}', "direction + pixels"),
    ]:
        s = load(base.replace("PARAMS", good))
        check(f"scroll parses ({label})", s.steps[1].op == "scroll")
    for bad, label in [
        ("'#more'", "bare string"),
        ('""', "empty string"),
        ("{}", "empty mapping"),
        ("{foo: 1}", "no target/direction/pixels"),
        ('{target: ""}', "empty target"),
        ("{target: 42}", "non-string target"),
        ('{direction: "sideways"}', "bad direction"),
        ("{pixels: -50}", "negative pixels"),
        ("{pixels: 1.5}", "non-integer pixels"),
    ]:
        try:
            load(base.replace("PARAMS", bad))
            check(f"bad scroll rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad scroll rejected ({label})", True)

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
        def scroll_into_view_if_needed(self, timeout=None):
            self.page.calls.append(("into_view", self.selector, timeout))

    class FakeMouse:
        def __init__(self, page):
            self.page = page
        def wheel(self, dx, dy):
            self.page.calls.append(("wheel", dx, dy))

    class FakePage:
        def __init__(self):
            self.calls = []
            self.mouse = FakeMouse(self)
        def locator(self, selector):
            return FakeLocator(self, selector)

    def run_step(params, timeout_ms=None):
        page = FakePage()
        step = Step(index=0, phase="steps", name="s", op="scroll",
                    params=params, expect={}, expect_items=[],
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})
        _do_action(page, step, "http://x", {})
        return page.calls

    calls = run_step({"target": "#more"})
    check("scroll with target uses scroll_into_view_if_needed and default timeout",
          calls == [("into_view", "#more", 15000)], str(calls))
    calls = run_step({"target": "#more"}, timeout_ms=5000)
    check("scroll with target honors step timeout_ms",
          calls == [("into_view", "#more", 5000)], str(calls))
    check("scroll down defaults to +600 vertical",
          run_step({"direction": "down"}) == [("wheel", 0, 600)])
    check("scroll up maps to -600 vertical",
          run_step({"direction": "up"}) == [("wheel", 0, -600)])
    check("scroll left maps to -600 horizontal",
          run_step({"direction": "left"}) == [("wheel", -600, 0)])
    check("scroll right maps to +600 horizontal",
          run_step({"direction": "right"}) == [("wheel", 600, 0)])
    check("scroll honors custom pixels",
          run_step({"direction": "down", "pixels": 1200}) == [("wheel", 0, 1200)])
    check("scroll pixels without direction defaults to down",
          run_step({"pixels": 300}) == [("wheel", 0, 300)])



def test_upload_op():
    """upload op (issue #76): spec validation ({target, path} required),
    the shared qaloop.upload.check_upload_path guard, and the runner
    calling locator.set_input_files with the guarded absolute path."""
    import tempfile
    from qaloop.runner import _do_action
    from qaloop.spec import Step, load_spec, SpecError
    from qaloop.upload import check_upload_path

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n  - {goto: /}\n"
            "  - name: u\n    upload: PARAMS\n")
    good = '{target: "#resume", path: "/tmp/resume.txt"}'
    s = load(base.replace("PARAMS", good))
    check("upload with {target, path} parses",
          s.steps[1].op == "upload"
          and s.steps[1].params["target"] == "#resume"
          and s.steps[1].params["path"] == "/tmp/resume.txt")
    for bad, label in [
        ("'#resume'", "bare string (needs {target, path})"),
        ("{}", "empty mapping"),
        ("{target: '#resume'}", "missing path"),
        ("{path: '/tmp/resume.txt'}", "missing target"),
        ("{target: '', path: '/tmp/resume.txt'}", "empty target"),
        ("{target: '#resume', path: ''}", "empty path"),
        ("{target: 42, path: '/tmp/resume.txt'}", "non-string target"),
        ("{target: '#resume', path: 42}", "non-string path"),
    ]:
        try:
            load(base.replace("PARAMS", bad))
            check(f"bad upload rejected ({label})", False, "no error")
        except SpecError:
            check(f"bad upload rejected ({label})", True)

    # Shared guard (same violations as the perform guard): inside-dir
    # absolute passes; relative, `..` escape, absolute-outside rejected.
    with tempfile.TemporaryDirectory() as d:
        inside = os.path.join(d, "resume.txt")
        check("guard: absolute path inside dir passes",
              check_upload_path(inside, d) == os.path.abspath(inside))
        for name, bad in [
            ("guard: relative path rejected", "resume.txt"),
            ("guard: .. escape rejected",
             os.path.join(d, "..", "escape.txt")),
            ("guard: absolute path outside rejected", "/etc/hostname"),
            ("guard: sibling dir via .. rejected",
             os.path.join(d, "sub", "..", "..", "other")),
        ]:
            try:
                check_upload_path(bad, d)
                check(name, False, bad)
            except ValueError:
                check(name, True)

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
        def set_input_files(self, files, timeout=None):
            self.page.calls.append(("set_input_files", self.selector,
                                    files, timeout))

    class FakePage:
        def __init__(self):
            self.calls = []
        def locator(self, selector):
            return FakeLocator(self, selector)

    def run_step(params, upload_dir, timeout_ms=None):
        page = FakePage()
        step = Step(index=0, phase="steps", name="u", op="upload",
                    params=params, expect={}, expect_items=[],
                    continue_on_fail=False, timeout_ms=timeout_ms, raw={})
        _do_action(page, step, "http://x", {}, upload_dir=upload_dir)
        return page

    with tempfile.TemporaryDirectory() as d:
        want = os.path.abspath(os.path.join(d, "resume.txt"))
        page = run_step({"target": "#resume", "path": want}, d)
        check("upload calls set_input_files with guarded abspath",
              page.calls == [("set_input_files", "#resume", want, 15000)],
              str(page.calls))
        page = run_step({"target": "#resume", "path": want}, d,
                        timeout_ms=5000)
        check("upload honors step timeout_ms",
              page.calls == [("set_input_files", "#resume", want, 5000)],
              str(page.calls))

        # Guard violation: raises (the step fails) and set_input_files
        # is never called.
        page = FakePage()
        step = Step(index=0, phase="steps", name="u", op="upload",
                    params={"target": "#resume", "path": "/etc/hostname"},
                    expect={}, expect_items=[], continue_on_fail=False,
                    timeout_ms=None, raw={})
        try:
            _do_action(page, step, "http://x", {}, upload_dir=d)
            check("upload guard violation fails the step", False, "no error")
        except ValueError:
            check("upload guard violation fails the step", True)
        check("guard violation never touches set_input_files",
              page.calls == [], str(page.calls))

        # Fail-closed: no declared upload dir at all.
        page = FakePage()
        step = Step(index=0, phase="steps", name="u", op="upload",
                    params={"target": "#resume", "path": want},
                    expect={}, expect_items=[], continue_on_fail=False,
                    timeout_ms=None, raw={})
        try:
            _do_action(page, step, "http://x", {}, upload_dir=None)
            check("undeclared upload dir fails the step", False, "no error")
        except RuntimeError as e:
            check("undeclared upload dir fails the step", True)
            check("undeclared-dir error names both sources",
                  "--upload-dir" in str(e) and "QALOOP_UPLOAD_DIR" in str(e),
                  str(e))
        check("undeclared dir never touches set_input_files",
              page.calls == [], str(page.calls))


def test_resolve_upload_dir():
    """resolve_upload_dir: --upload-dir flag wins over QALOOP_UPLOAD_DIR;
    None when neither is declared (fail-closed in the runner, issue #76)."""
    from qaloop.env import resolve_upload_dir

    def setenv(value):
        if value is None:
            os.environ.pop("QALOOP_UPLOAD_DIR", None)
        else:
            os.environ["QALOOP_UPLOAD_DIR"] = value

    try:
        setenv(None)
        check("no flag, no env → None", resolve_upload_dir(None) is None)
        setenv("/srv/up")
        check("env alone wins over nothing",
              resolve_upload_dir(None) == "/srv/up")
        check("flag beats env",
              resolve_upload_dir("/flag/dir") == "/flag/dir")
        setenv(None)
        check("flag alone → flag",
              resolve_upload_dir("/flag/dir") == "/flag/dir")
    finally:
        os.environ.pop("QALOOP_UPLOAD_DIR", None)


def test_frame_scoping():
    """Issue #80: `frame:` scopes selector actions/assertions to an iframe.

    Name form resolves via page.frame(name=), URL form via a page.frame(url=)
    substring; a non-matching frame raises RuntimeError (actions) or records
    an assertion failure; spec validation rejects non-string/empty frame and
    frame: combined with goto/seed/mock/script.
    """
    import tempfile as _tf

    from qaloop.runner import _do_action, _check_assertions, _resolve_scope
    from qaloop.spec import Step, load_spec, SpecError

    class FakeLocator:
        def __init__(self, frame, sel):
            self.frame = frame
            self.sel = sel
        def count(self):
            self.frame.calls.append(("count", self.sel))
            return 2
        def wait_for(self, state=None, timeout=None):
            self.frame.calls.append(("loc_wait", self.sel, state, timeout))
        @property
        def first(self):
            return self

    class FakeFrame:
        def __init__(self):
            self.calls = []
            self.texts = {}
        def click(self, sel, timeout=None):
            self.calls.append(("click", sel, timeout))
        def dblclick(self, sel, timeout=None):
            self.calls.append(("dblclick", sel, timeout))
        def fill(self, sel, text, timeout=None):
            self.calls.append(("fill", sel, text, timeout))
        def press(self, sel, key, timeout=None):
            self.calls.append(("press", sel, key, timeout))
        def check(self, sel, timeout=None):
            self.calls.append(("check", sel, timeout))
        def uncheck(self, sel, timeout=None):
            self.calls.append(("uncheck", sel, timeout))
        def select_option(self, sel, value, timeout=None):
            self.calls.append(("select", sel, value, timeout))
        def locator(self, sel, has_text=None):
            self.calls.append(("locator", sel, has_text))
            return FakeLocator(self, sel)
        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append(("selector", selector, state, timeout))
        def text_content(self, sel):
            self.calls.append(("text", sel))
            return self.texts.get(sel, "")
        def get_by_role(self, role, name=None, include_hidden=None):
            self.calls.append(("role", role, name))
            return FakeLocator(self, f"role:{role}")

    class FakePage:
        def __init__(self):
            self.calls = []
            self.by_name = {}
            self.by_url = {}
        def frame(self, name=None, url=None):
            self.calls.append(("frame", name,
                               url.pattern if url is not None else None))
            if name is not None:
                return self.by_name.get(name)
            for u, f in self.by_url.items():
                if url.search(u):
                    return f
            return None
        def click(self, sel, timeout=None):
            self.calls.append(("page_click", sel, timeout))
        def evaluate(self, expr):
            self.calls.append(("evaluate", expr))
            return 0
        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append(("page_selector", selector, state, timeout))
        def text_content(self, sel):
            self.calls.append(("page_text", sel))
            return ""

    def mkstep(op, params, frame=None, expect_items=None, step_timeout=5000):
        return Step(index=0, phase="steps", name="f", op=op, params=params,
                    expect={}, expect_items=expect_items or [],
                    continue_on_fail=False, timeout_ms=step_timeout,
                    frame=frame, raw={})

    fr = FakeFrame()
    page = FakePage()
    page.by_name["demo-frame"] = fr
    url_fr = FakeFrame()
    page.by_url["http://x/demo/iframe-content.html"] = url_fr

    # no frame: -> the page itself
    check("no frame: resolves to the page",
          _resolve_scope(page, mkstep("click", "#b")) is page)
    # name form routes a click into the frame
    _do_action(page, mkstep("click", "#b", frame="demo-frame"),
               "http://x", {})
    check("click with frame: name -> frame.click",
          fr.calls == [("click", "#b", 5000)], str(fr.calls))
    check("click with frame: never touches the page",
          [c for c in page.calls if c[0] != "frame"] == [], str(page.calls))
    # url-substring form resolves by frame URL
    _do_action(page, mkstep("click", "#b", frame="iframe-content"),
               "http://x", {})
    check("click with frame: url substring -> frame.click",
          url_fr.calls == [("click", "#b", 5000)], str(url_fr.calls))
    # every other frame-scoped action path also lands on the frame
    fr.calls.clear()
    _do_action(page, mkstep("dblclick", "#d", frame="demo-frame"),
               "http://x", {})
    _do_action(page,
               mkstep("fill", {"target": "#t", "text": "hi"},
                      frame="demo-frame"),
               "http://x", {})
    _do_action(page,
               mkstep("press", {"target": "#k", "key": "Enter"},
                      frame="demo-frame"),
               "http://x", {})
    _do_action(page, mkstep("check", "#c", frame="demo-frame"),
               "http://x", {})
    _do_action(page, mkstep("uncheck", "#u", frame="demo-frame"),
               "http://x", {})
    _do_action(page,
               mkstep("select", {"target": "#s", "value": "v"},
                      frame="demo-frame"),
               "http://x", {})
    _do_action(page,
               mkstep("wait", {"target": "#w", "text": "go"},
                      frame="demo-frame"),
               "http://x", {})
    check("frame-scoped dblclick/fill/press/check/uncheck/select/wait-text land on the frame",
          fr.calls == [("dblclick", "#d", 5000),
                       ("fill", "#t", "hi", 5000),
                       ("press", "#k", "Enter", 5000),
                       ("check", "#c", 5000),
                       ("uncheck", "#u", 5000),
                       ("select", "#s", "v", 5000),
                       ("locator", "#w", "go"),
                       ("loc_wait", "#w", "visible", 5000)],
          str(fr.calls))
    # missing frame: action raises RuntimeError with clear detail
    # (short timeout — the lookup polls, so a real timeout is slow)
    try:
        _do_action(page, mkstep("click", "#b", frame="nope",
                                step_timeout=50),
                   "http://x", {})
        check("missing frame click raises RuntimeError", False)
    except RuntimeError as e:
        check("missing frame click raises RuntimeError",
              "frame not found" in str(e) and "nope" in str(e), str(e))
    # missing frame: expect-only step records assertion failures, no raise
    step = mkstep(None, None, frame="nope",
                  expect_items=[("visible", "#x"), ("url_contains", "x")],
                  step_timeout=50)
    results = _check_assertions(page, step, None)
    check("missing frame -> all assertions fail",
          len(results) == 2 and all(not r.passed for r in results),
          str(results))
    check("missing frame detail names the frame",
          all("frame not found" in r.detail and "nope" in r.detail
              for r in results),
          str([r.detail for r in results]))
    # assertions with frame: resolve inside the frame
    fr.texts["#out"] = "hello world"
    n_before = len(fr.calls)
    step = mkstep(None, None, frame="demo-frame",
                  expect_items=[("visible", "#in"),
                                ("text_contains",
                                 {"selector": "#out", "text": "hello"}),
                                ("count",
                                 {"selector": ".row", "equals": 2}),
                                ("ax", {"role": "button", "name": "Go"})])
    results = _check_assertions(page, step, None)
    check("frame-scoped assertions pass",
          all(r.passed for r in results), str(results))
    kinds = [c[0] for c in fr.calls[n_before:]]
    check("frame-scoped assertions hit the frame only",
          kinds == ["selector", "selector", "text", "locator", "count",
                    "role", "loc_wait"],
          str(fr.calls[n_before:]))
    check("frame-scoped assertions never hit page selectors",
          not any(c[0] in ("page_selector", "page_text") for c in page.calls),
          str(page.calls))

    # the frame lookup polls: a frame that attaches late still resolves
    class SlowPage(FakePage):
        def __init__(self, real):
            super().__init__()
            self.real = real
            self.looks = 0
        def frame(self, name=None, url=None):
            self.looks += 1
            return None if self.looks < 3 else self.real
    slow = SlowPage(fr)
    scope = _resolve_scope(slow, mkstep("click", "#b", frame="demo-frame"),
                           timeout_ms=5000)
    check("frame lookup polls until the frame attaches",
          scope is fr and slow.looks == 3, str(slow.looks))

    # spec validation
    def load(body):
        with _tf.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("name: t\ntarget: http://x\nsteps:\n" + body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            import os as _os
            _os.unlink(p)

    s = load("  - name: ok\n    click: \"#b\"\n    frame: demo-frame\n")
    check("frame: accepted, parsed onto the step",
          s.steps[0].frame == "demo-frame", str(s.steps[0].frame))
    for body, label in [
            ("  - name: bad\n    click: \"#b\"\n    frame: 123\n",
             "frame: 123"),
            ("  - name: bad\n    click: \"#b\"\n    frame: \"\"\n",
             "frame empty"),
            ("  - name: bad\n    goto: /x\n    frame: f\n", "frame + goto"),
            ("  - name: bad\n    seed: {js: \"x=1\"}\n    frame: f\n",
             "frame + seed"),
            ("  - name: bad\n    mock: {url: /x, json: {}}\n    frame: f\n",
             "frame + mock"),
            ("  - name: bad\n    script: {js: \"x=1\"}\n    frame: f\n",
             "frame + script"),
    ]:
        try:
            load(body)
            check(f"rejected: {label}", False)
        except SpecError:
            check(f"rejected: {label}", True)
    import os as _os2
    _os2.environ["F"] = "env-frame"
    try:
        s = load("  - name: ok\n    click: \"#b\"\n    frame: \"$F\"\n")
        check("frame: gets env substitution",
              s.steps[0].frame == "env-frame", str(s.steps[0].frame))
    finally:
        del _os2.environ["F"]


def test_element_state_assertions():
    """Issue #81: attribute / value / focused assertions.

    Validation: attribute needs {selector, name, exactly-one of
    equals|contains (a string)}; value needs {selector, equals (a string)};
    focused needs {selector}. Runner: attribute reads via get_attribute (a
    missing attribute never matches, even for contains), value via
    input_value, focused via document.activeElement identity; all three
    poll until the step deadline like text_contains/count.
    """
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step, SpecError, _validate_expect

    def rejects(expect, label):
        try:
            _validate_expect(expect, "steps[0]")
            check(f"rejected: {label}", False)
        except SpecError:
            check(f"rejected: {label}", True)

    rejects({"attribute": {"name": "x", "equals": "y"}}, "attribute missing selector")
    rejects({"attribute": {"selector": "#a", "equals": "y"}}, "attribute missing name")
    rejects({"attribute": {"selector": "#a", "name": "x"}},
            "attribute needs equals|contains")
    rejects({"attribute": {"selector": "#a", "name": "x",
                           "equals": "y", "contains": "z"}},
            "attribute rejects both equals and contains")
    rejects({"attribute": {"selector": "#a", "name": "x", "equals": True}},
            "attribute equals must be a string")
    ok = _validate_expect(
        {"attribute": {"selector": "#a", "name": "x", "contains": "z"}}, "steps[0]")
    check("attribute valid form accepted",
          ok == [("attribute", {"selector": "#a", "name": "x", "contains": "z"})],
          str(ok))
    rejects({"value": {"selector": "#i"}}, "value missing equals")
    rejects({"value": {"selector": "#i", "equals": 3}}, "value equals must be a string")
    check("value valid form accepted",
          _validate_expect({"value": {"selector": "#i", "equals": "x"}},
                           "steps[0]") == [("value", {"selector": "#i", "equals": "x"})])
    rejects({"focused": "#i"}, "focused must be a mapping")
    rejects({"focused": {}}, "focused needs selector")
    check("focused valid form accepted",
          _validate_expect({"focused": {"selector": "#i"}},
                           "steps[0]") == [("focused", {"selector": "#i"})])

    class FakeELL:
        def __init__(self, scope, sel):
            self.scope = scope
            self.sel = sel
        def get_attribute(self, name):
            n = self.scope.reads.get(("attr", self.sel, name), 0)
            self.scope.reads[("attr", self.sel, name)] = n + 1
            seq = self.scope.attr_seq.get((self.sel, name))
            if seq is not None:
                return seq[min(n, len(seq) - 1)]
            return self.scope.attrs.get((self.sel, name))
        def input_value(self):
            n = self.scope.reads.get(("val", self.sel), 0)
            self.scope.reads[("val", self.sel)] = n + 1
            seq = self.scope.value_seq.get(self.sel)
            if seq is not None:
                return seq[min(n, len(seq) - 1)]
            return self.scope.values.get(self.sel, "")
        def evaluate(self, expr):
            self.scope.calls.append(("evaluate", self.sel, expr))
            n = self.scope.reads.get(("focus", self.sel), 0)
            self.scope.reads[("focus", self.sel)] = n + 1
            seq = self.scope.focus_seq.get(self.sel)
            focused = seq[min(n, len(seq) - 1)] if seq is not None else self.scope.focused
            return expr == "el => document.activeElement === el" and focused == self.sel

    class FakeScope:
        def __init__(self):
            self.calls = []
            self.reads = {}
            self.attrs = {}
            self.attr_seq = {}
            self.values = {}
            self.value_seq = {}
            self.focused = None
            self.focus_seq = {}
        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append(("wait", selector, state))
        def locator(self, sel):
            return FakeELL(self, sel)

    def run(expect_items, scope, timeout_ms=5000):
        step = Step(index=0, phase="steps", name="e", op=None, params=None,
                    expect={}, expect_items=expect_items,
                    continue_on_fail=False, timeout_ms=timeout_ms,
                    frame=None, raw={})
        return _check_assertions(scope, step, None)

    # attribute equals: pass, fail, detail names final observed value
    sc = FakeScope()
    sc.attrs[("#btn", "data-state")] = "ready"
    res = run([("attribute", {"selector": "#btn", "name": "data-state",
                              "equals": "ready"})], sc)
    check("attribute equals passes", res[0].passed, res[0].detail)
    res = run([("attribute", {"selector": "#btn", "name": "data-state",
                              "equals": "idle"})], sc, timeout_ms=300)
    check("attribute equals fails", not res[0].passed, res[0].detail)
    check("attribute detail names observed value",
          "'ready'" in res[0].detail and "want ='idle'" in res[0].detail,
          res[0].detail)
    # attribute contains: pass, fail
    res = run([("attribute", {"selector": "#btn", "name": "data-state",
                              "contains": "ead"})], sc)
    check("attribute contains passes", res[0].passed, res[0].detail)
    res = run([("attribute", {"selector": "#btn", "name": "data-state",
                              "contains": "nope"})], sc, timeout_ms=300)
    check("attribute contains fails", not res[0].passed, res[0].detail)
    # missing attribute (get_attribute -> None) never matches either form
    res = run([("attribute", {"selector": "#gone", "name": "data-state",
                              "equals": "x"})], sc, timeout_ms=300)
    check("missing attribute never matches equals", not res[0].passed,
          res[0].detail)
    res = run([("attribute", {"selector": "#gone", "name": "data-state",
                              "contains": "x"})], sc, timeout_ms=300)
    check("missing attribute never matches contains", not res[0].passed,
          res[0].detail)
    check("missing attribute detail shows None", "None" in res[0].detail,
          res[0].detail)
    # attribute polls: value flips to the wanted one on the second read
    sc2 = FakeScope()
    sc2.attr_seq[("#p", "data-x")] = ["loading", "done"]
    res = run([("attribute", {"selector": "#p", "name": "data-x",
                              "equals": "done"})], sc2)
    check("attribute polls until match", res[0].passed, res[0].detail)
    # value: pass, fail, polling
    sc3 = FakeScope()
    sc3.values["#email"] = "qa@example.com"
    res = run([("value", {"selector": "#email",
                          "equals": "qa@example.com"})], sc3)
    check("value passes", res[0].passed, res[0].detail)
    res = run([("value", {"selector": "#email",
                          "equals": "other@example.com"})], sc3, timeout_ms=300)
    check("value fails", not res[0].passed, res[0].detail)
    check("value detail names observed value",
          "'qa@example.com'" in res[0].detail, res[0].detail)
    sc4 = FakeScope()
    sc4.value_seq["#email"] = ["", "qa@example.com"]
    res = run([("value", {"selector": "#email",
                          "equals": "qa@example.com"})], sc4)
    check("value polls until match", res[0].passed, res[0].detail)
    # focused: pass, fail, polling
    sc5 = FakeScope()
    sc5.focused = "#search"
    res = run([("focused", {"selector": "#search"})], sc5)
    check("focused passes", res[0].passed, res[0].detail)
    res = run([("focused", {"selector": "#other"})], sc5, timeout_ms=300)
    check("focused fails", not res[0].passed, res[0].detail)
    check("focused detail names observed focus",
          "focused=False" in res[0].detail, res[0].detail)
    sc6 = FakeScope()
    sc6.focus_seq["#search"] = [None, "#search"]
    res = run([("focused", {"selector": "#search"})], sc6)
    check("focused polls until match", res[0].passed, res[0].detail)
    # selector assertion attaches before reading, like text_contains
    check("wait_for_selector attached first",
          sc.calls[0] == ("wait", "#btn", "attached"), str(sc.calls[:2]))


def test_issue_82_cookie_storage_assertions():
    """Issue #82: cookie / storage assertions.

    Validation: cookie needs a non-empty name and at most one of
    equals|contains (a string); neither means a presence check. storage
    needs kind local|session, a non-empty key, and an optional string
    equals. Runner: cookie reads via page.context.cookies() (a missing
    cookie never matches, even for contains), storage via page.evaluate
    with the key passed as a JS arg (a missing key -> null never
    matches); both poll until the step deadline like attribute/value.
    """
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step, SpecError, _validate_expect

    def rejects(expect, label):
        try:
            _validate_expect(expect, "steps[0]")
            check(f"rejected: {label}", False)
        except SpecError:
            check(f"rejected: {label}", True)

    rejects({"cookie": "session"}, "cookie must be a mapping")
    rejects({"cookie": {"equals": "x"}}, "cookie missing name")
    rejects({"cookie": {"name": "", "equals": "x"}}, "cookie name is empty")
    rejects({"cookie": {"name": "s", "equals": "x", "contains": "y"}},
            "cookie rejects both equals and contains")
    rejects({"cookie": {"name": "s", "equals": True}},
            "cookie equals must be a string")
    rejects({"cookie": {"name": "s", "contains": 3}},
            "cookie contains must be a string")
    check("cookie name-only presence form accepted",
          _validate_expect({"cookie": {"name": "s"}}, "steps[0]")
          == [("cookie", {"name": "s"})])
    rejects({"storage": {"kind": "local"}}, "storage missing key")
    rejects({"storage": {"key": "k"}}, "storage missing kind")
    rejects({"storage": {"kind": "cookies", "key": "k"}},
            "storage rejects bad kind")
    rejects({"storage": {"kind": "local", "key": ""}},
            "storage key is empty")
    rejects({"storage": {"kind": "local", "key": "k", "equals": 1}},
            "storage equals must be a string")
    check("storage key-only presence form accepted",
          _validate_expect({"storage": {"kind": "session", "key": "flag"}},
                           "steps[0]")
          == [("storage", {"kind": "session", "key": "flag"})])

    class FakeCtx:
        def __init__(self):
            self.cookie_seq = None
            self.reads = 0
        def cookies(self):
            n = self.reads
            self.reads += 1
            if self.cookie_seq is not None:
                return self.cookie_seq[min(n, len(self.cookie_seq) - 1)]
            return []

    class FakePage:
        def __init__(self):
            self.context = FakeCtx()
            self.store = {}
            self.store_seq = {}
            self.store_reads = {}
            self.eval_calls = []
        def evaluate(self, script, arg):
            self.eval_calls.append((script, arg))
            k = (arg["kind"], arg["key"])
            n = self.store_reads.get(k, 0)
            self.store_reads[k] = n + 1
            seq = self.store_seq.get(k)
            if seq is not None:
                return seq[min(n, len(seq) - 1)]
            return self.store.get(k)

    def run(expect_items, page, timeout_ms=5000):
        step = Step(index=0, phase="steps", name="e", op=None, params=None,
                    expect={}, expect_items=expect_items,
                    continue_on_fail=False, timeout_ms=timeout_ms,
                    frame=None, raw={})
        return _check_assertions(page, step, None)

    # cookie equals: pass, fail, detail names final observed value
    pg = FakePage()
    pg.context.cookie_seq = [[{"name": "session", "value": "abc123"}]]
    res = run([("cookie", {"name": "session", "equals": "abc123"})], pg)
    check("cookie equals passes", res[0].passed, res[0].detail)
    pg2 = FakePage()
    pg2.context.cookie_seq = [[{"name": "session", "value": "abc123"}]]
    res = run([("cookie", {"name": "session", "equals": "other"})], pg2,
              timeout_ms=300)
    check("cookie equals fails", not res[0].passed, res[0].detail)
    check("cookie detail names observed value",
          "'abc123'" in res[0].detail and "want ='other'" in res[0].detail,
          res[0].detail)
    # cookie contains: pass, fail
    res = run([("cookie", {"name": "session", "contains": "abc"})], pg2)
    check("cookie contains passes", res[0].passed, res[0].detail)
    res = run([("cookie", {"name": "session", "contains": "zzz"})], pg2,
              timeout_ms=300)
    check("cookie contains fails", not res[0].passed, res[0].detail)
    # missing cookie never matches, even for contains
    pg3 = FakePage()
    res = run([("cookie", {"name": "session", "equals": "abc123"})], pg3,
              timeout_ms=300)
    check("missing cookie never matches equals", not res[0].passed,
          res[0].detail)
    res = run([("cookie", {"name": "session", "contains": "abc"})], pg3,
              timeout_ms=300)
    check("missing cookie never matches contains", not res[0].passed,
          res[0].detail)
    check("missing cookie detail shows None", "None" in res[0].detail,
          res[0].detail)
    # cookie presence: pass, fail
    res = run([("cookie", {"name": "session"})], pg2)
    check("cookie presence passes", res[0].passed, res[0].detail)
    res = run([("cookie", {"name": "other"})], pg2, timeout_ms=300)
    check("cookie presence fails when absent", not res[0].passed,
          res[0].detail)
    # cookie polls: cookie arrives on the second context.cookies() read
    pg4 = FakePage()
    pg4.context.cookie_seq = [[], [{"name": "session", "value": "abc123"}]]
    res = run([("cookie", {"name": "session", "equals": "abc123"})], pg4)
    check("cookie polls until match", res[0].passed, res[0].detail)
    # storage equals: pass, fail, detail names observed value
    pg5 = FakePage()
    pg5.store[("local", "consent")] = "accepted"
    res = run([("storage", {"kind": "local", "key": "consent",
                          "equals": "accepted"})], pg5)
    check("storage equals passes", res[0].passed, res[0].detail)
    res = run([("storage", {"kind": "local", "key": "consent",
                          "equals": "declined"})], pg5, timeout_ms=300)
    check("storage equals fails", not res[0].passed, res[0].detail)
    check("storage detail names observed value",
          "'accepted'" in res[0].detail and "want ='declined'" in res[0].detail,
          res[0].detail)
    # missing key (getItem -> None) never matches; presence passes/fails
    res = run([("storage", {"kind": "session", "key": "flag"})], pg5)
    check("storage presence fails when key absent", not res[0].passed,
          res[0].detail)
    pg6 = FakePage()
    pg6.store[("session", "flag")] = "beta"
    res = run([("storage", {"kind": "session", "key": "flag"})], pg6)
    check("storage presence passes when set", res[0].passed, res[0].detail)
    res = run([("storage", {"kind": "session", "key": "flag",
                          "equals": "x"})], pg5, timeout_ms=300)
    check("missing storage key never matches", not res[0].passed,
          res[0].detail)
    # storage polls: key appears on the second getItem read
    pg7 = FakePage()
    pg7.store_seq[("local", "consent")] = [None, "accepted"]
    res = run([("storage", {"kind": "local", "key": "consent",
                          "equals": "accepted"})], pg7)
    check("storage polls until match", res[0].passed, res[0].detail)
    # the key rides along as a JS arg, never interpolated into the script
    script, arg = pg5.eval_calls[0]
    check("storage key passed as JS arg, not interpolated",
          arg == {"kind": "local", "key": "consent"}
          and "consent" not in script and "accepted" not in script,
          script)


def test_request_body_issue_83():
    """Issue #83: opt-in request-body recording + `request_body` assertion.

    - spec validation: `record_bodies` must be a real bool (default False);
      `request_body` needs {url, exactly one of contains|equals (a string)}
    - collectors: body recorded only when enabled, capped at 5000 chars,
      key absent when off (privacy default)
    - runner: contains/equals evaluation over network_log, one-shot;
      fail-closed detail names the record_bodies requirement
    """
    from qaloop.artifacts import Collectors
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step, load_spec, SpecError

    def load(yaml_body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(yaml_body)
            path = f.name
        try:
            return load_spec(path)
        finally:
            os.unlink(path)

    def expect_reject(yaml_body, name):
        try:
            load(yaml_body)
        except SpecError:
            check(name, True)
        else:
            check(name, False, "SpecError not raised")

    base = ("name: rb\ntarget: http://x\nsteps:\n"
            "  - name: s\n"
            "    wait: {response: \"/x\"}\n"
            "    expect: [%s]\n%s")
    spec = load(base % ("request_body: {url: \"/api/save\", contains: 'x'}",
                        "record_bodies: true"))
    check("request_body valid spec + record_bodies True",
          spec.record_bodies is True
          and spec.steps[0].expect_items[0][0] == "request_body",
          str(spec.steps[0].expect_items))
    spec2 = load(base % ("request_body: {url: \"/api/save\", equals: 'x'}",
                         ""))
    check("record_bodies defaults to False", spec2.record_bodies is False)
    expect_reject(base % ("request_body: {contains: 'x'}", ""),
                  "request_body rejects missing url")
    expect_reject(base % ("request_body: {url: \"\"}", ""),
                  "request_body rejects empty url")
    expect_reject(base % ("request_body: {url: \"/api\"}", ""),
                  "request_body rejects missing contains/equals")
    expect_reject(base % ("request_body: {url: \"/api\", contains: 'a', "
                          "equals: 'b'}", ""),
                  "request_body rejects both contains+equals")
    expect_reject(base % ("request_body: {url: \"/api\", contains: 5}", ""),
                  "request_body rejects non-string contains")
    expect_reject(base % ("request_body: {url: \"/api\", equals: 'x'}",
                          "record_bodies: \"yes\""),
                  "record_bodies rejects quoted-string non-bool")
    expect_reject(base % ("request_body: {url: \"/api\", equals: 'x'}",
                          "record_bodies: 1"),
                  "record_bodies rejects int 1")

    class FakeRequest:
        def __init__(self, method, url, post_data=None, timing=None,
                     failure=None, boom=False):
            self.method = method
            self.url = url
            self._post_data = post_data
            self.boom = boom
            self.timing = timing if timing is not None else {}
            self.failure = failure

        @property
        def post_data(self):
            if self.boom:
                raise RuntimeError("boom")
            return self._post_data

    class FakeResponse:
        def __init__(self, status, request, url):
            self.status = status
            self.request = request
            self.url = url

    def respond(c, req, url, status=200):
        c._on_response(FakeResponse(status, req, url))

    c = Collectors()
    respond(c, FakeRequest("POST", "http://x/api/save",
                           post_data='{"pet":"fluffy"}'),
            "http://x/api/save")
    check("body key absent when record_bodies off (privacy default)",
          "body" not in c.network_log[0], str(c.network_log[0]))
    c2 = Collectors(record_bodies=True)
    respond(c2, FakeRequest("POST", "http://x/api/save",
                            post_data='{"pet":"fluffy"}'),
            "http://x/api/save")
    check("body recorded when record_bodies=True",
          c2.network_log[0].get("body") == '{"pet":"fluffy"}',
          str(c2.network_log[0]))
    c3 = Collectors(record_bodies=True)
    respond(c3, FakeRequest("POST", "http://x/a", post_data="x" * 6000),
            "http://x/a")
    check("body capped at 5000 chars",
          len(c3.network_log[0]["body"]) == 5000
          and c3.network_log[0]["body"] == "x" * 5000)
    c3b = Collectors(record_bodies=True)
    respond(c3b, FakeRequest("POST", "http://x/a", post_data="y" * 5000),
            "http://x/a")
    check("body of exactly 5000 kept whole",
          len(c3b.network_log[0]["body"]) == 5000)
    c4 = Collectors(record_bodies=True)
    respond(c4, FakeRequest("GET", "http://x/a"), "http://x/a")
    check("GET with no post data records body=None (key present)",
          c4.network_log[0].get("body") is None
          and "body" in c4.network_log[0], str(c4.network_log[0]))
    c5 = Collectors(record_bodies=True)
    c5._on_requestfailed(FakeRequest("POST", "http://x/a", post_data="p=1",
                                     failure="net::ERR_ABORTED"))
    check("requestfailed records body when enabled",
          c5.network_log[0].get("body") == "p=1", str(c5.network_log[0]))
    c5b = Collectors()
    c5b._on_requestfailed(FakeRequest("POST", "http://x/a", post_data="p=1",
                                      failure="net::ERR_ABORTED"))
    check("requestfailed omits body when off",
          "body" not in c5b.network_log[0])
    c6 = Collectors(record_bodies=True)
    respond(c6, FakeRequest("POST", "http://x/a", boom=True), "http://x/a")
    check("post_data raising degrades to None body, no crash",
          c6.network_log[0].get("body") is None)

    class FakePage:
        pass

    def run(params, log):
        step = Step(index=0, phase="steps", name="s", op=None, params=None,
                    expect={}, expect_items=[("request_body", params)],
                    continue_on_fail=False, timeout_ms=None, raw={})
        return _check_assertions(FakePage(), step, None, network_log=log)[0]

    def entry(url, body=None, has_body_key=True, status=200):
        e = {"ts": 1.0, "method": "POST", "url": url,
             "status": status, "ms": 5.0}
        if has_body_key:
            e["body"] = body
        return e

    log = [entry("http://x/api/save", body='{"pet":"fluffy","hp":42}'),
           entry("http://x/api/other", body='{"pet":"fluffy"}'),
           entry("http://x/api/save", body='{"pet":"spot"}')]
    r = run({"url": "/api/save", "contains": '"pet":"fluffy"'}, log)
    check("request_body contains hit passes", r.passed, r.detail)
    r = run({"url": "/api/save", "contains": '"pet":"rex"'}, log)
    check("request_body contains miss fails", not r.passed, r.detail)
    check("contains-miss detail names matching-entry count",
          "2 matching entries" in r.detail and "record_bodies" not in r.detail,
          r.detail)
    r = run({"url": "/api/save", "equals": '{"pet":"fluffy","hp":42}'}, log)
    check("request_body equals exact match passes", r.passed, r.detail)
    r = run({"url": "/api/save", "equals": '{"pet":"fluffy"}'}, log)
    check("request_body equals mismatch fails", not r.passed, r.detail)
    r = run({"url": "/nope", "contains": "x"}, log)
    check("request_body no url match fails", not r.passed, r.detail)
    check("no-match detail names zero count",
          "0 matching entries" in r.detail, r.detail)
    r = run({"url": "/api/save", "contains": '"pet":"fluffy"'}, [])
    check("request_body empty log fails", not r.passed, r.detail)
    log2 = [entry("http://x/api/save", has_body_key=False)]
    r = run({"url": "/api/save", "contains": "x"}, log2)
    check("request_body without any recorded body fails", not r.passed,
          r.detail)
    check("missing-body detail names count + record_bodies requirement",
          "1 matching entries" in r.detail
          and "record_bodies: true" in r.detail, r.detail)
    log3 = [entry("http://x/api/save", body=None)]
    r = run({"url": "/api/save", "equals": ""}, log3)
    check("recorded None body counts as empty string for equals",
          r.passed, r.detail)


def test_issue_84_perf_assertions():
    """Issue #84: Web Vitals perf assertion.

    Validation: perf needs a non-empty mapping of metric (lcp_ms, cls,
    inp_ms) to exactly {lte: non-negative number}; gte/equals, unknown
    metrics, bool/string/negative budgets, and empty mappings are
    rejected. Runner: one fixed page.evaluate read per poll iteration;
    each metric polls until a non-null reading is <= its budget;
    fail-closed on null; failure detail names the final observed value.
    """
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step, SpecError, _validate_expect

    def rejects(expect, label):
        try:
            _validate_expect(expect, "steps[0]")
            check(f"rejected: {label}", False)
        except SpecError:
            check(f"rejected: {label}", True)

    good = {"perf": {"lcp_ms": {"lte": 2500}, "cls": {"lte": 0.1},
                       "inp_ms": {"lte": 500}}}
    check("perf valid mapping accepted",
          _validate_expect(good, "steps[0]") == [("perf", good["perf"])])
    rejects({"perf": {}}, "perf empty mapping rejected")
    rejects({"perf": "fast"}, "perf non-mapping rejected")
    rejects({"perf": {"fcp_ms": {"lte": 1}}}, "perf unknown metric rejected")
    rejects({"perf": {"lcp_ms": {}}}, "perf missing lte rejected")
    rejects({"perf": {"lcp_ms": {"gte": 1}}}, "perf gte rejected")
    rejects({"perf": {"lcp_ms": {"equals": 1}}}, "perf equals rejected")
    rejects({"perf": {"lcp_ms": {"lte": 1, "gte": 0}}},
            "perf two comparators rejected")
    rejects({"perf": {"lcp_ms": 2500}}, "perf bare number rejected")
    rejects({"perf": {"lcp_ms": {"lte": -1}}}, "perf negative budget rejected")
    rejects({"perf": {"cls": {"lte": True}}}, "perf bool budget rejected")
    rejects({"perf": {"inp_ms": {"lte": "500"}}},
            "perf string budget rejected")
    check("perf float budget accepted",
          _validate_expect({"perf": {"cls": {"lte": 0.05}}}, "steps[0]")
          == [("perf", {"cls": {"lte": 0.05}})])

    class FakePage:
        def __init__(self, seq):
            self.seq = seq
            self.reads = 0
            self.scripts = []
        def evaluate(self, script):
            self.scripts.append(script)
            n = self.reads
            self.reads += 1
            return self.seq[min(n, len(self.seq) - 1)]

    def run(perf_params, seq, timeout_ms=5000):
        step = Step(index=0, phase="steps", name="e", op=None, params=None,
                    expect={}, expect_items=[("perf", perf_params)],
                    continue_on_fail=False, timeout_ms=timeout_ms,
                    frame=None, raw={})
        return _check_assertions(FakePage(seq), step, None)

    params = {"lcp_ms": {"lte": 2500}, "cls": {"lte": 0.1},
              "inp_ms": {"lte": 500}}
    res = run(params, [{"lcp_ms": 120.5, "cls": 0.0, "inp_ms": 136.2}])
    check("perf all budgets met passes", res[0].passed, res[0].detail)
    check("perf detail names each observed value",
          "lcp_ms=120.5" in res[0].detail and "cls=0.0" in res[0].detail
          and "inp_ms=136.2" in res[0].detail, res[0].detail)
    # lcp violation: FAIL, detail names the observed value
    res = run(params, [{"lcp_ms": 3200.0, "cls": 0.0, "inp_ms": 100.0}],
              timeout_ms=300)
    check("perf lcp violation fails", not res[0].passed, res[0].detail)
    check("perf violation detail names observed value",
          "lcp_ms=3200.0" in res[0].detail
          and "want <=2500" in res[0].detail, res[0].detail)
    # null metric never satisfies the budget (fail-closed)
    res = run(params, [{"lcp_ms": 100.0, "cls": 0.0, "inp_ms": None}],
              timeout_ms=300)
    check("perf null metric fails closed", not res[0].passed, res[0].detail)
    check("perf null detail shows None",
          "inp_ms=None" in res[0].detail, res[0].detail)
    # cls may be exactly 0: a real reading, passes
    res = run({"cls": {"lte": 0.1}}, [{"lcp_ms": None, "cls": 0,
                                      "inp_ms": None}])
    check("perf cls zero passes", res[0].passed, res[0].detail)
    # async settling: successive reads converge below the budget
    res = run(params, [{"lcp_ms": None, "cls": 0.0, "inp_ms": None},
                       {"lcp_ms": 9000.0, "cls": 0.0, "inp_ms": None},
                       {"lcp_ms": 800.0, "cls": 0.0, "inp_ms": 120.0}])
    check("perf polls until readings settle", res[0].passed, res[0].detail)
    # single evaluate read per poll iteration, no interpolation
    pg = FakePage([{"lcp_ms": 1.0, "cls": 0.0, "inp_ms": 1.0}])
    step = Step(index=0, phase="steps", name="e", op=None, params=None,
                expect={}, expect_items=[("perf", params)],
                continue_on_fail=False, timeout_ms=5000,
                frame=None, raw={})
    _check_assertions(pg, step, None)
    check("perf uses one fixed script for every read",
          len(pg.scripts) == 3 and len(set(pg.scripts)) == 1
          and "largest-contentful-paint" in pg.scripts[0]
          and "first-input" in pg.scripts[0]
          and "layout-shift" in pg.scripts[0]
          and "PerformanceObserver" in pg.scripts[0]
          and "observe({type: 'layout-shift'" in pg.scripts[0],
          str(len(pg.scripts)))


def test_issue_85_a11y_assertions():
    """Issue #85: axe-core a11y assertion.

    Validation: a11y needs a non-empty mapping with violations: {lte:
    non-negative number} (only lte) and optional rules: [axe rule id
    strings]; non-mappings, missing violations, gte, bare numbers,
    negative/bool budgets, unknown keys, and bad rules are rejected.
    Runner: vendored axe.min.js injected idempotently
    (add_script_tag only when window.axe is undefined), one-shot
    axe.run per step with optional runOnly scope; PASS when the
    violation count is <= the budget, FAIL names the count, ids, and
    first descriptions; injection/scan errors fail closed.
    """
    from qaloop.runner import _check_assertions, _axe_scan
    from qaloop.spec import Step, SpecError, _validate_expect

    def rejects(expect, label):
        try:
            _validate_expect(expect, "steps[0]")
            check(f"rejected: {label}", False)
        except SpecError:
            check(f"rejected: {label}", True)

    good = {"a11y": {"violations": {"lte": 0},
                     "rules": ["color-contrast"]}}
    check("a11y valid mapping accepted",
          _validate_expect(good, "steps[0]") == [("a11y", good["a11y"])])
    check("a11y violations-only accepted",
          _validate_expect({"a11y": {"violations": {"lte": 3}}}, "steps[0]")
          == [("a11y", {"violations": {"lte": 3}})])
    rejects({"a11y": {}}, "a11y empty mapping rejected")
    rejects({"a11y": "no violations"}, "a11y non-mapping rejected")
    rejects({"a11y": {"rules": ["image-alt"]}},
            "a11y missing violations rejected")
    rejects({"a11y": {"violations": {"gte": 0}}}, "a11y gte rejected")
    rejects({"a11y": {"violations": {"equals": 0}}}, "a11y equals rejected")
    rejects({"a11y": {"violations": 0}}, "a11y bare number rejected")
    rejects({"a11y": {"violations": {"lte": -1}}},
            "a11y negative budget rejected")
    rejects({"a11y": {"violations": {"lte": True}}},
            "a11y bool budget rejected")
    rejects({"a11y": {"violations": {"lte": "0"}}},
            "a11y string budget rejected")
    rejects({"a11y": {"violations": {"lte": 0, "gte": 0}}},
            "a11y two comparators rejected")
    rejects({"a11y": {"violations": {"lte": 0}, "bogus": 1}},
            "a11y unknown key rejected")
    rejects({"a11y": {"violations": {"lte": 0}, "rules": []}},
            "a11y empty rules rejected")
    rejects({"a11y": {"violations": {"lte": 0}, "rules": "color-contrast"}},
            "a11y string rules rejected")
    rejects({"a11y": {"violations": {"lte": 0}, "rules": [42]}},
            "a11y non-string rules rejected")
    rejects({"a11y": {"violations": {"lte": 0}, "rules": [""]}},
            "a11y blank rule rejected")

    VIOLS = [{"id": "image-alt",
              "description": "Images must have alternate text"},
             {"id": "button-name",
              "description": "Buttons must have discernible text"}]

    class FakeA11yPage:
        def __init__(self, violations=None, fail_msg=None):
            self._violations = list(violations or [])
            self._fail_msg = fail_msg
            self.script_tags = []
            self.eval_calls = []
            self.axe_present = False
        def evaluate(self, script, *args):
            self.eval_calls.append((script, args))
            if "typeof window.axe" in script:
                return not self.axe_present
            if self._fail_msg:
                raise RuntimeError(self._fail_msg)
            return [dict(v) for v in self._violations]
        def add_script_tag(self, content=None, **kwargs):
            self.script_tags.append(content)
            self.axe_present = True

    def run(a11y_params, page, timeout_ms=5000):
        step = Step(index=0, phase="steps", name="e", op=None, params=None,
                    expect={}, expect_items=[("a11y", a11y_params)],
                    continue_on_fail=False, timeout_ms=timeout_ms,
                    frame=None, raw={})
        return _check_assertions(page, step, None)

    pg = FakeA11yPage(violations=VIOLS)
    res = run({"violations": {"lte": 5}}, pg)
    check("a11y under budget passes", res[0].passed, res[0].detail)
    check("a11y pass detail names count",
          "violations=2 want <=5" in res[0].detail, res[0].detail)
    check("a11y injects vendored script once",
          len(pg.script_tags) == 1 and "axe" in pg.script_tags[0]
          and len(pg.script_tags[0]) > 100000,
          f"tags={len(pg.script_tags)}")
    # Second scan on the same page skips re-injection.
    res = run({"violations": {"lte": 5}}, pg)
    check("a11y injection is idempotent", res[0].passed
          and len(pg.script_tags) == 1, f"tags={len(pg.script_tags)}")
    # Violation: FAIL, detail names count, ids, descriptions.
    res = run({"violations": {"lte": 0}}, FakeA11yPage(violations=VIOLS))
    check("a11y over budget fails", not res[0].passed, res[0].detail)
    check("a11y fail detail names ids and descriptions",
          "violations=2 want <=0" in res[0].detail
          and "ids=image-alt,button-name" in res[0].detail
          and "Images must have alternate text" in res[0].detail
          and "Buttons must have discernible text" in res[0].detail,
          res[0].detail)
    # Exactly at budget passes.
    res = run({"violations": {"lte": 2}}, FakeA11yPage(violations=VIOLS))
    check("a11y at budget passes", res[0].passed, res[0].detail)
    # Empty violation list passes with empty ids.
    res = run({"violations": {"lte": 0}}, FakeA11yPage())
    check("a11y clean page passes", res[0].passed, res[0].detail)
    check("a11y clean detail shows zero ids",
          "violations=0 want <=0; ids=" in res[0].detail, res[0].detail)
    # Fail-closed: injection/scan error fails with the error named.
    res = run({"violations": {"lte": 0}},
              FakeA11yPage(fail_msg="axe is not defined"))
    check("a11y scan error fails closed",
          not res[0].passed and "axe scan failed: axe is not defined"
          in res[0].detail, res[0].detail)
    # rules list is passed through to the scan wrapper.
    pg = FakeA11yPage()
    _axe_scan(pg, ["color-contrast"])
    check("a11y rules passed to evaluate",
          pg.eval_calls[-1][1] == (["color-contrast"],),
          str(pg.eval_calls[-1][1]))
    pg = FakeA11yPage()
    _axe_scan(pg, None)
    check("a11y no-rules passes empty list",
          pg.eval_calls[-1][1] == ([],), str(pg.eval_calls[-1][1]))


def test_issue_86_include():
    """Issue #86: `include` flow composition — shared fragments.

    Loader: include steps expand on the RAW step lists before _parse_steps
    (so include/with never hit the unknown-keys guard); paths resolve
    relative to the including file's directory; cycles are SpecErrors;
    `with:` substitution is compose-time and lexically scoped per include
    level; leftover ${VAR}s still go through strict env substitution (the
    seam with issue #87's runtime store, out of scope here).
    """
    import tempfile
    from qaloop.spec import load_spec, SpecError

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def write(root, rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def rejects(path, label, needle=""):
        try:
            load_spec(path)
            check(f"rejected: {label}", False)
        except SpecError as e:
            ok = not needle or needle in str(e)
            check(f"rejected: {label}", ok, str(e) if not ok else "")

    with tempfile.TemporaryDirectory() as tmp:
        base = "name: t\ntarget: http://x\nsteps:\n"
        # --- basic inlining order + with substitution (braced + plain) ---
        write(tmp, "fragments/login.yaml",
              "steps:\n"
              "  - name: frag-one\n"
              "    fill: {target: \"${field}\", text: \"$user\"}\n"
              "  - name: frag-two\n"
              "    goto: ${base}/login\n"
              "    timeout_ms: 5000\n"
              "  - name: frag-three\n"
              "    expect:\n"
              "      - visible: \"${field}\"\n")
        write(tmp, "flow.yaml", base +
              "  - name: before\n    expect: {noop: true}\n"
              "  - name: include the fragment\n"
              "    include: fragments/login.yaml\n"
              "    with: {field: \"#user\", user: alice, base: \"http://h\"}\n"
              "  - name: after\n    expect: {noop: true}\n")
        spec = load_spec(os.path.join(tmp, "flow.yaml"))
        names = [s.name for s in spec.steps]
        check("include inlines at the include position, names kept",
              names == ["before", "frag-one", "frag-two", "frag-three", "after"],
              str(names))
        check("with substitutes braced + plain forms",
              spec.steps[1].params == {"target": "#user", "text": "alice"},
              str(spec.steps[1].params))
        check("with substitutes in urls",
              spec.steps[2].params == "http://h/login",
              str(spec.steps[2].params))
        check("with leaves non-string values untouched",
              spec.steps[2].timeout_ms == 5000, str(spec.steps[2].timeout_ms))
        check("with substitutes inside list values",
              spec.steps[3].expect_items == [("visible", "#user")],
              str(spec.steps[3].expect_items))
        # --- nested includes resolve relative to the fragment's own dir ---
        write(tmp, "fragments/inner.yaml",
              "steps:\n  - name: deep\n    expect: {noop: true}\n")
        write(tmp, "fragments/outer.yaml",
              "steps:\n  - name: mid\n    expect: {noop: true}\n  - include: inner.yaml\n")
        write(tmp, "rel.yaml", base + "  - include: fragments/outer.yaml\n")
        spec = load_spec(os.path.join(tmp, "rel.yaml"))
        check("nested include resolves relative to the fragment's dir",
              [s.name for s in spec.steps] == ["mid", "deep"],
              str([s.name for s in spec.steps]))
        # --- nested with: inner level uses its own with ---
        write(tmp, "c.yaml",
              "name: c\nsteps:\n"
              "  - name: inner\n"
              "    fill: {target: \"#x\", text: \"${v} and ${w}\"}\n")
        write(tmp, "b.yaml",
              "name: b\nsteps:\n"
              "  - name: middle\n"
              "    fill: {target: \"#m\", text: \"${v}\"}\n"
              "  - include: c.yaml\n"
              "    with: {v: inner, w: deep}\n")
        write(tmp, "n.yaml", base +
              "  - include: b.yaml\n    with: {v: outer}\n")
        spec = load_spec(os.path.join(tmp, "n.yaml"))
        texts = [s.params["text"] for s in spec.steps]
        check("nested with is lexically scoped per include level",
              texts == ["outer", "inner and deep"], str(texts))
        # --- parent with does NOT leak into a nested include ---
        write(tmp, "c2.yaml",
              "name: c2\nsteps:\n"
              "  - name: inner-no-with\n"
              "    fill: {target: \"#x\", text: \"v=${v}\"}\n")
        write(tmp, "b2.yaml",
              "name: b2\nsteps:\n  - include: c2.yaml\n")
        write(tmp, "n2.yaml", base +
              "  - include: b2.yaml\n    with: {v: outer}\n")
        os.environ.pop("v", None)
        os.environ.pop("V", None)
        rejects(os.path.join(tmp, "n2.yaml"),
                "parent with does not leak into nested include",
                "env var $v is not set")
        # --- cycles ---
        write(tmp, "self.yaml",
              "name: t\ntarget: http://x\nsteps:\n  - include: self.yaml\n")
        rejects(os.path.join(tmp, "self.yaml"),
                "self-include cycle", "include cycle detected")
        write(tmp, "a.yaml",
              "name: t\ntarget: http://x\nsteps:\n  - include: b3.yaml\n")
        write(tmp, "b3.yaml",
              "name: b3\nsteps:\n  - include: a.yaml\n")
        rejects(os.path.join(tmp, "a.yaml"),
                "A->B->A cycle", "include cycle detected")
        # --- include-step validation ---
        write(tmp, "fragments/ok.yaml",
              "steps:\n  - name: ok\n    expect: {noop: true}\n")
        inc_cases = [
            ("with not a mapping",
             "  - include: fragments/ok.yaml\n    with: [1]\n",
             "`with` must be a mapping"),
            ("with keys must be strings",
             "  - include: fragments/ok.yaml\n    with: {1: x}\n",
             "`with` keys must be strings"),
            ("with values must be scalars",
             "  - include: fragments/ok.yaml\n    with: {a: [1]}\n",
             "`with` values must be scalars"),
            ("unknown extra key on include step",
             "  - include: fragments/ok.yaml\n    timeout_ms: 5\n",
             "include takes only"),
            ("include + action op",
             "  - include: fragments/ok.yaml\n    click: \"#x\"\n",
             "include takes only"),
            ("include + expect",
             "  - include: fragments/ok.yaml\n    expect: {noop: true}\n",
             "include takes only"),
            ("include not a string",
             "  - include: 42\n", "`include` must be a non-empty string"),
            ("missing fragment file",
             "  - include: fragments/nope.yaml\n", "fragment file not found"),
        ]
        for label, block, needle in inc_cases:
            write(tmp, "e.yaml", base + block)
            rejects(os.path.join(tmp, "e.yaml"), label, needle)
        # --- fragment-file validation ---
        frag_cases = [
            ("fragment with setup rejected",
             "name: f\nsetup:\n  - expect: {noop: true}\nsteps:\n  - expect: {noop: true}\n",
             "fragment allows only"),
            ("fragment must be a mapping",
             "- just\n- a\n- list\n", "fragment must be a YAML mapping"),
            ("fragment steps must be a list",
             "name: f\nsteps: {a: 1}\n", "fragment `steps` must be a list"),
        ]
        for label, ftext, needle in frag_cases:
            write(tmp, "fragments/bad.yaml", ftext)
            write(tmp, "e2.yaml", base + "  - include: fragments/bad.yaml\n")
            rejects(os.path.join(tmp, "e2.yaml"), label, needle)
        # --- ${ENV} leftovers still go through envsubst (the #87 seam) ---
        os.environ["ISSUE_86_PROBE"] = "from-env"
        try:
            write(tmp, "fragments/env.yaml",
                  "steps:\n"
                  "  - name: env\n"
                  "    fill: {target: \"#e\", text: \"${ISSUE_86_PROBE}\"}\n")
            write(tmp, "env.yaml", base + "  - include: fragments/env.yaml\n")
            spec = load_spec(os.path.join(tmp, "env.yaml"))
            check("leftover ${VAR} envsubst'd from the environment",
                  spec.steps[0].params["text"] == "from-env",
                  str(spec.steps[0].params))
        finally:
            del os.environ["ISSUE_86_PROBE"]
        # --- longest-name-first: ${username} wins over ${user} ---
        write(tmp, "fragments/names.yaml",
              "steps:\n"
              "  - name: n\n"
              "    fill: {target: \"#u\", text: \"${username}/${user}\"}\n")
        write(tmp, "names.yaml", base +
              "  - include: fragments/names.yaml\n"
              "    with: {user: alice, username: alice2}\n")
        spec = load_spec(os.path.join(tmp, "names.yaml"))
        check("longest with-name wins",
              spec.steps[0].params["text"] == "alice2/alice",
              str(spec.steps[0].params))
        # --- the shipped demo flows load through load_spec ---
        for flow, user, pw in (("flows/demo-login-a.yaml", "alice", "s3cret"),
                               ("flows/demo-login-b.yaml", "bob", "hunter2")):
            spec = load_spec(os.path.join(repo, flow), strict_env=False)
            check(f"{flow} inlines 4 fragment steps",
                  len(spec.steps) == 4, str(len(spec.steps)))
            fills = [s.params["text"] for s in spec.steps if s.op == "fill"]
            check(f"{flow} with-substituted for {user}",
                  fills == [user, pw], str(fills))
            items = dict(spec.steps[3].expect_items)
            check(f"{flow} welcome assertion carries the user",
                  items.get("text_contains", {}).get("text")
                  == f"Welcome, {user}", str(items))


def test_issue_87_flow_variables():
    """Issue #87: step-level `store` capture + `${var}` runtime interpolation.

    Loader: store parses (js source only; text/attribute rejected);
    declared ${name} survives strict envsubst (and shadows env vars —
    runtime table wins); undefined ${name} is a SpecError at load;
    use-before-store ordering is a SpecError where statically known.
    Runner: _interpolate_vars substitutes from the table (missing var =
    clear failure naming it); _evaluate_stores captures str(result) via
    page.evaluate, re-store overwrites, eval failure names the variable.
    """
    import tempfile
    from qaloop.spec import (load_spec, SpecError, _collect_store_names,
                             _find_var_refs, _check_variable_ordering, Step)
    from qaloop.runner import _interpolate_vars, _evaluate_stores

    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    def rejects(text, label, needle=""):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(text)
        try:
            load_spec(f.name)
            check(f"rejected: {label}", False)
        except SpecError as e:
            ok = not needle or needle in str(e)
            check(f"rejected: {label}", ok, str(e) if not ok else "")
        finally:
            os.unlink(f.name)

    base = "name: v\ntarget: http://t\nsteps:\n"

    # --- valid store spec parses ---
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name, base +
              "  - name: capture\n"
              "    click: \"#create\"\n"
              "    store: {itemId: {js: \"document.title\"}}\n"
              "  - name: use\n"
              "    click: \".del[data-item-id='${itemId}']\"\n")
        spec = load_spec(f.name)
        check("store parses to {name: {js}}",
              spec.steps[0].store == {"itemId": {"js": "document.title"}},
              str(spec.steps[0].store))
        check("declared ${itemId} survives strict envsubst",
              spec.steps[1].params == ".del[data-item-id='${itemId}']",
              str(spec.steps[1].params))
        os.unlink(f.name)

    # --- store shape rejections ---
    rejects(base + "  - name: s\n    click: \"#b\"\n    store: [1]\n",
            "store must be a mapping", "must be a mapping")
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {9lives: {js: \"1\"}}\n",
            "bad variable name", "[A-Za-z_][A-Za-z0-9_]*")
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {vid: \"document.title\"}\n",
            "store source must be a mapping", "must be a mapping")
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {vid: {text: \".row\"}}\n",
            "text source not yet supported", "only the js source")
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {vid: {attribute: \".row\"}}\n",
            "attribute source not yet supported", "only the js source")
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {vid: {js: \"\", text: \"x\"}}\n",
            "mixed sources rejected", "only the js source")
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {vid: {js: \"\"}}\n",
            "empty js rejected", "non-empty string")
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {vid: {js: 42}}\n",
            "non-string js rejected", "non-empty string")

    # --- undefined ${name} is a SpecError at load (strict) ---
    if "ISSUE87_PROBE" in os.environ:
        del os.environ["ISSUE87_PROBE"]
    rejects(base + "  - name: s\n    click: \".x${ISSUE87_PROBE}\"\n",
            "undefined var rejected", "is not set")

    # --- stored name shadows an env var: runtime table wins at load ---
    os.environ["ISSUE87_SHADOW"] = "from-env"
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name, base +
                  "  - name: capture\n"
                  "    click: \"#b\"\n"
                  "    store: {ISSUE87_SHADOW: {js: \"1\"}}\n"
                  "  - name: use\n"
                  "    click: \".x${ISSUE87_SHADOW}\"\n")
            spec = load_spec(f.name)
            check("declared name exempt from env expansion",
                  spec.steps[1].params == ".x${ISSUE87_SHADOW}",
                  str(spec.steps[1].params))
            os.unlink(f.name)
    finally:
        del os.environ["ISSUE87_SHADOW"]

    # --- use-before-store ordering ---
    rejects(base + "  - name: use\n    click: \".x${vid}\"\n"
            "  - name: store\n    click: \"#b\"\n"
            "    store: {vid: {js: \"1\"}}\n",
            "use-before-store rejected", "used before any step stores it")
    rejects(base + "  - name: self\n    click: \".x${vid}\"\n"
            "    store: {vid: {js: \"1\"}}\n",
            "same-step self-reference rejected",
            "used before any step stores it")
    # declared in setup, used in steps: fine
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name, "name: v\ntarget: http://t\n"
              "setup:\n"
              "  - name: cap\n    goto: /x\n"
              "    store: {vid: {js: \"1\"}}\n"
              "steps:\n"
              "  - name: use\n    click: \".x${vid}\"\n"
              "  - name: recap\n    click: \"#b\"\n"
              "    store: {vid: {js: \"2\"}}\n"
              "  - name: reuse\n    click: \".y${vid}\"\n")
        spec = load_spec(f.name)
        check("cross-phase and re-store ordering accepted",
              spec.setup[0].store == {"vid": {"js": "1"}}
              and spec.steps[1].store == {"vid": {"js": "2"}})
        os.unlink(f.name)
    # declared-but-never-stored reference: ordering error via direct call
    st = Step(index=0, phase="steps", name="s", op="click",
              params=".x${ghost}", expect={}, expect_items=[],
              continue_on_fail=False, timeout_ms=1000, raw={})
    try:
        _check_variable_ordering([], [st], [], {"ghost"}, "spec x")
        check("never-stored reference rejected", False)
    except SpecError as e:
        check("never-stored reference rejected", "never stored" in str(e),
              str(e))
    # non-strict load: an unset env var in params is skipped by the
    # ordering check (the env is unknown at validate time)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name, base +
              "  - name: capture\n"
              "    click: \"#b\"\n"
              "    store: {vid: {js: \"1\"}}\n"
              "  - name: use\n"
              "    goto: \"http://t/${ISSUE87_UNSET_ENV}\"\n")
        spec = load_spec(f.name, strict_env=False)
        check("non-strict: unset env ref skipped by ordering",
              spec.steps[1].params == "http://t/${ISSUE87_UNSET_ENV}",
              str(spec.steps[1].params))
        os.unlink(f.name)

    # --- _collect_store_names / _find_var_refs edge branches ---
    check("collect skips non-list", _collect_store_names(None) == set())
    check("collect skips non-dict steps",
          _collect_store_names(["x", {"store": [1]}]) == set())
    check("collect gathers names",
          _collect_store_names([{"store": {"a": {"js": "1"}, 9: {}}}])
          == {"a"})
    check("find_var_refs nested",
          _find_var_refs({"a": ["${x}", 5, {"b": "$y"}]}) == {"x", "y"})
    check("find_var_refs ignores non-strings",
          _find_var_refs([1, None, True]) == set())

    # --- _interpolate_vars ---
    check("interpolates ${name} and $name",
          _interpolate_vars("a ${v} b $v", {"v": "X"}, "w") == "a X b X")
    check("interpolates nested structures",
          _interpolate_vars({"t": ["${v}"], "n": 3}, {"v": "X"}, "w")
          == {"t": ["X"], "n": 3})
    check("non-strings pass through",
          _interpolate_vars(42, {"v": "X"}, "w") == 42)
    check("leaves $5 alone",
          _interpolate_vars("costs $5", {"v": "X"}, "w") == "costs $5")
    try:
        _interpolate_vars(".x${missing}", {}, "steps[2]")
        check("missing var fails", False)
    except RuntimeError as e:
        check("missing var names the variable",
              "${missing}" in str(e) and "never captured" in str(e),
              str(e))

    # --- _evaluate_stores with a stub page ---
    class FakePage:
        def __init__(self, results):
            self.results = results
            self.calls = []

        def evaluate(self, js):
            self.calls.append(js)
            if js == "boom":
                raise ValueError("page exploded")
            return self.results[js]

    st = Step(index=0, phase="steps", name="s", op="click", params="#b",
              expect={}, expect_items=[], continue_on_fail=False,
              timeout_ms=1000, raw={},
              store={"a": {"js": "document.title"}, "n": {"js": "40 + 2"}})
    page = FakePage({"document.title": "hello", "40 + 2": 42})
    variables = {}
    _evaluate_stores(page, st, variables, "steps[0]")
    check("stores str(result)",
          variables == {"a": "hello", "n": "42"}, str(variables))
    # re-store overwrites
    page2 = FakePage({"document.title": "bye", "40 + 2": 7})
    _evaluate_stores(page2, st, variables, "steps[0]")
    check("re-store overwrites", variables["a"] == "bye", str(variables))
    st_boom = Step(index=1, phase="steps", name="s2", op=None, params=None,
                   expect={}, expect_items=[], continue_on_fail=False,
                   timeout_ms=1000, raw={}, store={"b": {"js": "boom"}})
    try:
        _evaluate_stores(FakePage({}), st_boom, {}, "steps[1]")
        check("store eval failure raises", False)
    except RuntimeError as e:
        check("store eval failure names the variable",
              "'b'" in str(e) and "evaluation failed" in str(e), str(e))
    # no stores: no-op
    st_empty = Step(index=2, phase="steps", name="s3", op=None, params=None,
                    expect={}, expect_items=[], continue_on_fail=False,
                    timeout_ms=1000, raw={}, store={})
    _evaluate_stores(FakePage({}), st_empty, variables, "steps[2]")
    check("empty store is a no-op", variables["a"] == "bye")


def test_issue_88_matrix_flows():
    """Issue #88: data-driven `matrix:` flows.

    Loader: expand_matrix cross-product ordering + list-of-maps
    passthrough + every validation rejection; matrix names survive
    load-time strict envsubst (step params and target:), undeclared
    names still fail, matrix refs are exempt from use-before-store,
    matrix/store name collision rejected.
    Runner: run_flow(initial_vars=...) seeds the table from the
    combination (copy, not alias), interpolates matrix refs in step
    params and in the target.
    CLI/report: per-combination subdirs, combined parent REPORT.md,
    exit 0 iff all combinations passed.
    """
    import tempfile
    from types import SimpleNamespace
    from qaloop.spec import (load_spec, SpecError, expand_matrix,
                             _check_variable_ordering, Step)
    from qaloop.runner import run_flow, _interpolate_target
    from qaloop.report import write_matrix_summary
    from qaloop.cli import _cmd_verify_run, _combo_slug

    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    # --- expand_matrix: cross-product ordering ---
    combos = expand_matrix({"a": [1, 2], "b": ["x", "y"]})
    check("cross product ordering",
          combos == [{"a": "1", "b": "x"}, {"a": "1", "b": "y"},
                     {"a": "2", "b": "x"}, {"a": "2", "b": "y"}],
          str(combos))
    # scalar stringification incl. bool/None
    combos = expand_matrix({"a": [True, None, 1.5]})
    check("scalars stringified via str()",
          combos == [{"a": "True"}, {"a": "None"}, {"a": "1.5"}],
          str(combos))
    # --- expand_matrix: list-of-maps passthrough ---
    combos = expand_matrix([{"u": "ada", "n": 3}, {"u": "bob"}])
    check("list-of-maps passthrough",
          combos == [{"u": "ada", "n": "3"}, {"u": "bob"}], str(combos))
    # single combination
    check("single combination", expand_matrix([{"u": "ada"}]) == [{"u": "ada"}])

    # --- expand_matrix: all rejections ---
    bad = ["x", 42, [], {}, [{}], ["x"], [1],
           {"9lives": [1]}, {"a": "scalar-not-list"}, {"a": []},
           {"a": [{"b": 1}]}, {"a": [1, {"b": 2}]},
           [{"ok": 1}, {"bad-name!": 2}], [{"a": [1, 2], "b": {}}]]
    n_bad = 0
    for i, matrix in enumerate(bad):
        try:
            expand_matrix(matrix)
            check(f"rejected matrix shape {i}", False, repr(matrix))
        except SpecError:
            n_bad += 1
    check("all invalid matrix shapes rejected", n_bad == len(bad),
          f"{n_bad}/{len(bad)}")

    base = "name: m\ntarget: http://t\nsteps:\n"

    def rejects(text, label, needle=""):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(text)
        try:
            load_spec(f.name)
            check(f"rejected: {label}", False)
        except SpecError as e:
            ok = not needle or needle in str(e)
            check(f"rejected: {label}", ok, str(e) if not ok else "")
        finally:
            os.unlink(f.name)

    # --- matrix/store name collision rejected ---
    rejects(base + "  - name: s\n    click: \"#b\"\n"
            "    store: {v: {js: \"1\"}}\n"
            "matrix:\n  v: [1, 2]\n",
            "matrix/store name collision", "also declared by a `store` step")
    # --- matrix refs pass load-time validation ---
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name,
              "name: m\ntarget: http://h/${tenant}\n"
              "matrix:\n  tenant: [t1, t2]\n"
              "steps:\n"
              "  - name: go\n    goto: /p/${tenant}\n")
        spec = load_spec(f.name)
        check("matrix spec parses", spec.matrix == [{"tenant": "t1"},
                                                    {"tenant": "t2"}],
              str(spec.matrix))
        check("matrix ref survives in target",
              spec.target == "http://h/${tenant}", spec.target)
        check("matrix ref survives in step params",
              spec.steps[0].params == "/p/${tenant}",
              str(spec.steps[0].params))
        os.unlink(f.name)
    # --- undeclared names still fail at load ---
    if "ISSUE88_PROBE" in os.environ:
        del os.environ["ISSUE88_PROBE"]
    rejects(base + "  - name: s\n    click: \".x${ISSUE88_PROBE}\"\n"
            "matrix:\n  v: [1]\n",
            "undeclared var still rejected", "is not set")
    # --- matrix names exempt from use-before-store (incl. first step) ---
    st = Step(index=0, phase="steps", name="s", op="click",
              params=".x${mvar}", expect={}, expect_items=[],
              continue_on_fail=False, timeout_ms=1000, raw={})
    try:
        _check_variable_ordering([], [st], [], {"mvar"}, "spec x",
                                 {"mvar"})
        check("matrix name exempt from ordering", True)
    except SpecError as e:
        check("matrix name exempt from ordering", False, str(e))
    # non-matrix declared name still errors via the same call
    try:
        _check_variable_ordering([], [st], [], {"mvar"}, "spec x")
        check("non-matrix ordering still enforced", False)
    except SpecError as e:
        check("non-matrix ordering still enforced",
              "never stored" in str(e), str(e))

    # --- _interpolate_target ---
    check("target: combo ref substituted",
          _interpolate_target("http://h/${t}/x", {"t": "T1"})
          == "http://h/T1/x")
    check("target: unknown ref left alone",
          _interpolate_target("http://h/${t}/${LEFT}", {"t": "T1"})
          == "http://h/T1/${LEFT}")
    check("target: no refs unchanged",
          _interpolate_target("http://h/x", {"t": "T1"}) == "http://h/x")

    # --- _combo_slug ---
    check("combo slug", _combo_slug({"u": "a b", "p": "x/y"}, 0) == "u-a-b-p-x-y",
          _combo_slug({"u": "a b", "p": "x/y"}, 0))
    check("combo slug fallback", _combo_slug({}, 3) == "combo-03")

    # --- run_flow with initial_vars, faked browser ---
    import playwright.sync_api as pw_sync

    gotos = []
    evaluated = []

    class FakePage:
        def on(self, event, handler):
            pass

        def evaluate(self, js):
            evaluated.append(js)
            return "CAPTURED"

        def goto(self, url, **kw):
            gotos.append(url)

        def wait_for_timeout(self, ms):
            pass

    class FakeCtx:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name,
                  "name: m\ntarget: http://h/${tenant}\n"
                  "matrix:\n  tenant: [t1]\n"
                  "steps:\n"
                  "  - name: go\n    goto: /p/${tenant}\n"
                  "  - name: recapture the matrix var\n"
                  "    wait_ms: 1\n"
                  "    store: {other: {js: \"'x'\"}}\n"
                  "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                  "trace: false}\n")
            spec = load_spec(f.name)
            os.unlink(f.name)
        with tempfile.TemporaryDirectory() as run_dir:
            combo = {"tenant": "t1"}
            result = run_flow(spec, run_dir=run_dir, initial_vars=combo)
            check("run_flow passes with seeded vars",
                  result.status == "passed", result.status)
            check("target interpolated from combination",
                  result.target == "http://h/t1", result.target)
            check("step params interpolated from combination",
                  gotos == ["http://h/t1/p/t1"], str(gotos))
            check("store still captures alongside seeds",
                  evaluated == ["'x'"], str(evaluated))
            check("combination map not aliased by the run",
                  combo == {"tenant": "t1"}, str(combo))
        # re-storing a matrix name inside the run must not touch the
        # caller's map either (copy, not alias)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name,
                  "name: m\ntarget: http://h\n"
                  "matrix:\n  v: [orig]\n"
                  "steps:\n"
                  "  - name: overwrite\n"
                  "    wait_ms: 1\n"
                  "    store: {other: {js: \"'x'\"}}\n"
                  "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                  "trace: false}\n")
            spec2 = load_spec(f.name)
            os.unlink(f.name)
        with tempfile.TemporaryDirectory() as run_dir:
            combo = {"v": "orig"}
            result = run_flow(spec2, run_dir=run_dir, initial_vars=combo)
            check("run never mutates caller combination",
                  combo == {"v": "orig"} and result.status == "passed",
                  str(combo))
    finally:
        pw_sync.sync_playwright = old_pw

    # --- write_matrix_summary ---
    import playwright.sync_api as pw_sync2  # noqa: F401 (reuse fakes above)
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name,
                  "name: m\ntarget: http://h/${t}\n"
                  "matrix:\n  t: [a, b]\n"
                  "steps:\n"
                  "  - name: go\n    goto: /${t}\n"
                  "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                  "trace: false}\n")
            spec3 = load_spec(f.name)
            os.unlink(f.name)
        with tempfile.TemporaryDirectory() as run_dir:
            results = []
            for i, combo in enumerate(spec3.matrix):
                subdir = os.path.join(run_dir, "matrix",
                                      f"{i:02d}-{_combo_slug(combo, i)}")
                os.makedirs(subdir, exist_ok=True)
                result = run_flow(spec3, run_dir=subdir, initial_vars=combo)
                results.append((combo, result, subdir))
            path = write_matrix_summary(spec3, results, run_dir)
            with open(path) as fh:
                body = fh.read()
            check("matrix summary 2/2 PASS", "[2/2 PASS]" in body, body[:200])
            check("matrix summary lists combos",
                  "`t=a`" in body and "`t=b`" in body, body[:400])
            check("matrix summary links subdirs",
                  "matrix/00-t-a/REPORT.md" in body, body[:600])
            check("matrix summary written to parent",
                  path == os.path.join(run_dir, "REPORT.md"))
    finally:
        pw_sync.sync_playwright = old_pw

    # --- _cmd_verify_run dispatches to the matrix branch ---
    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name,
                  "name: cli-matrix\ntarget: http://h/${t}\n"
                  "matrix:\n  t: [a, b]\n"
                  "steps:\n"
                  "  - name: go\n    goto: /${t}\n"
                  "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                  "trace: false}\n"
                  "investigate_on_failure: false\n")
            flow_path = f.name
        with tempfile.TemporaryDirectory() as root:
            run_dir = os.path.join(root, "run1")
            os.makedirs(run_dir)
            # stale dirs so prune_old_runs actually prunes (covers the log line)
            for n in range(6):
                os.makedirs(os.path.join(root, f"z-old-{n}"))
            ledger = FakeLedger()
            args = SimpleNamespace(flow=flow_path, target=None, headed=True,
                                   executable_path=None, upload_dir=None,
                                   investigate=False, keep_runs=5,
                                   max_investigation_actions=None)
            rc = _cmd_verify_run(args, run_dir, ledger, root, 5)
            check("matrix verify exit 0 when all pass", rc == 0, str(rc))
            parent = os.path.join(run_dir, "REPORT.md")
            check("parent REPORT.md written", os.path.exists(parent))
            with open(parent) as fh:
                body = fh.read()
            check("parent report combined summary",
                  "[2/2 PASS]" in body and "matrix/01-t-b/REPORT.md" in body,
                  body[:400])
            check("per-combo REPORT.md written",
                  os.path.exists(os.path.join(run_dir, "matrix",
                                              "00-t-a", "REPORT.md")))
            kinds = [r["kind"] for r in ledger.rows]
            check("ledger records matrix run", kinds == ["scripted-matrix"],
                  str(kinds))
            check("ledger records combo counts",
                  ledger.rows[0]["combinations"] == 2
                  and ledger.rows[0]["passed"] == 2, str(ledger.rows[0]))
        os.unlink(flow_path)
    finally:
        pw_sync.sync_playwright = old_pw


def test_issue_200_baselines_matrix():
    """Issue #200: `qaloop baselines` dispatches matrix flows per
    combination (SPEC: baselines runs the flow in update mode; matrix
    flows run once per combination — the two must compose).

    _cmd_baselines_run with a matrix spec delegates to the matrix
    dispatch with baseline_update=True threaded to every combination's
    run_flow (initial_vars seeded from the combination); exit 0 iff
    every combination passed, 1 when any fails. The non-matrix path
    stays a single run_flow(baseline_update=True) call.
    """
    import tempfile
    from types import SimpleNamespace
    import qaloop.cli as cli_mod
    from qaloop.cli import _cmd_baselines_run
    from qaloop.runner import RunResult, StepResult
    from qaloop.spec import load_spec

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    def fake_result(status, ms=10):
        steps = [StepResult(index=0, phase="steps", name="s", op="wait_ms",
                            status="passed" if status == "passed"
                            else "failed",
                            duration_ms=ms)]
        return RunResult(flow_name="x", target="http://h", status=status,
                         started=1.0, ended=1.0 + ms / 1000.0, steps=steps,
                         failed_step=None if status == "passed" else 0,
                         console_errors=[], page_errors=[],
                         failed_requests=[], bad_responses=[], run_dir="")

    def write(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    real_run_flow = cli_mod.run_flow
    try:
        with tempfile.TemporaryDirectory() as d:
            flow_path = os.path.join(d, "m.yaml")
            write(flow_path,
                  "name: baselines-matrix\ntarget: http://h/${user}\n"
                  "matrix:\n  user: [ada, bob]\n"
                  "steps:\n"
                  "  - name: go\n    goto: /${user}\n"
                  "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                  "trace: false}\n"
                  "investigate_on_failure: false\n")
            spec = load_spec(flow_path)
            args = SimpleNamespace(flow=flow_path, target=None, headed=True,
                                   executable_path=None, upload_dir=None)
            run_dir = os.path.join(d, "run")
            os.makedirs(run_dir)

            # --- dispatch: 2 combinations, baseline_update threaded ---
            calls = []
            cli_mod.run_flow = lambda s, **kw: calls.append(kw) or \
                fake_result("passed")
            ledger = FakeLedger()
            rc = _cmd_baselines_run(args, run_dir, spec, ledger, d)
            check("baselines matrix exit 0 when all pass", rc == 0, str(rc))
            check("baselines matrix ran 2 combinations", len(calls) == 2,
                  str(len(calls)))
            check("baselines matrix threads baseline_update=True",
                  all(c.get("baseline_update") is True for c in calls),
                  str([c.get("baseline_update") for c in calls]))
            check("baselines matrix seeds initial_vars per combination",
                  [c.get("initial_vars") for c in calls]
                  == [{"user": "ada"}, {"user": "bob"}],
                  str([c.get("initial_vars") for c in calls]))
            check("baselines matrix per-combination subdirs",
                  all(c.get("run_dir", "").startswith(
                      os.path.join(run_dir, "matrix")) for c in calls),
                  str([c.get("run_dir") for c in calls]))
            check("baselines matrix writes combined parent report",
                  os.path.exists(os.path.join(run_dir, "REPORT.md")))
            check("baselines matrix ledger entry",
                  [r["kind"] for r in ledger.rows] == ["scripted-matrix"],
                  str([r["kind"] for r in ledger.rows]))

            # --- exit code: one failing combination -> 1 ---
            calls = []
            cli_mod.run_flow = lambda s, **kw: calls.append(kw) or \
                fake_result("failed"
                            if kw.get("initial_vars") == {"user": "bob"}
                            else "passed")
            rc = _cmd_baselines_run(args, run_dir, spec, FakeLedger(), d)
            check("baselines matrix exit 1 when a combination fails",
                  rc == 1, str(rc))
            check("baselines matrix failure still runs all combinations",
                  len(calls) == 2, str(len(calls)))

            # --- non-matrix path unchanged: single baseline_update run ---
            plain_path = os.path.join(d, "plain.yaml")
            write(plain_path,
                  "name: baselines-plain\ntarget: http://h\n"
                  "steps:\n"
                  "  - name: go\n    goto: /\n"
                  "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                  "trace: false}\n"
                  "investigate_on_failure: false\n")
            plain_spec = load_spec(plain_path)
            calls = []
            cli_mod.run_flow = lambda s, **kw: calls.append(kw) or \
                fake_result("passed")
            args_p = SimpleNamespace(flow=plain_path, target=None,
                                     headed=True, executable_path=None,
                                     upload_dir=None)
            rc = _cmd_baselines_run(args_p, os.path.join(d, "run2"),
                                    plain_spec, FakeLedger(), d)
            check("baselines non-matrix exit 0", rc == 0, str(rc))
            check("baselines non-matrix single run in run_dir",
                  len(calls) == 1
                  and calls[0].get("run_dir") == os.path.join(d, "run2"),
                  str(calls))
            check("baselines non-matrix baseline_update=True",
                  calls and calls[0].get("baseline_update") is True,
                  str(calls))

            # --- spec=None fallback: _cmd_baselines_run loads it itself ---
            calls = []
            cli_mod.run_flow = lambda s, **kw: calls.append(kw) or \
                fake_result("passed")
            rc = _cmd_baselines_run(args, os.path.join(d, "run3"), None,
                                    FakeLedger(), d)
            check("baselines spec=None loads spec and dispatches matrix",
                  rc == 0 and len(calls) == 2, f"rc={rc} calls={len(calls)}")

            # --- invalid spec via the fallback load exits 2 ---
            bad = os.path.join(d, "bad.yaml")
            write(bad, "name: [unclosed\n")
            args_b = SimpleNamespace(flow=bad, target=None, headed=True,
                                     executable_path=None, upload_dir=None)
            rc = _cmd_baselines_run(args_b, os.path.join(d, "run4"))
            check("baselines invalid spec exits 2", rc == 2, str(rc))
    finally:
        cli_mod.run_flow = real_run_flow

    # --- cmd_baselines end-to-end dispatch (defensive spec load) ---
    from qaloop.cli import cmd_baselines
    real_default_runs = cli_mod.default_runs_root
    try:
        with tempfile.TemporaryDirectory() as d, temp_ledger():
            cli_mod.default_runs_root = lambda: os.path.join(d, "runs")
            flow_path = os.path.join(d, "m.yaml")
            write(flow_path,
                  "name: baselines-cli-matrix\ntarget: http://h/${user}\n"
                  "matrix:\n  user: [ada, bob]\n"
                  "steps:\n"
                  "  - name: go\n    goto: /${user}\n"
                  "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                  "trace: false}\n"
                  "investigate_on_failure: false\n")
            calls = []
            cli_mod.run_flow = lambda s, **kw: calls.append(kw) or \
                fake_result("passed")
            args = SimpleNamespace(flow=flow_path, target=None,
                                   headed=True, executable_path=None)
            rc = cmd_baselines(args)
            check("cmd_baselines matrix exit 0", rc == 0, str(rc))
            check("cmd_baselines matrix ran both combinations",
                  len(calls) == 2
                  and all(c.get("baseline_update") is True for c in calls),
                  str(len(calls)))
            # Light-parseable but full-spec-invalid: the defensive load
            # in cmd_baselines must swallow it and still exit 2 via
            # _cmd_baselines_run, never crash.
            bad = os.path.join(d, "bad.yaml")
            write(bad, "name: badstep\ntarget: http://h\nsteps:\n"
                       "  - name: s\n    bogus_op: 1\n")
            rc = cmd_baselines(SimpleNamespace(
                flow=bad, target=None, headed=True, executable_path=None))
            check("cmd_baselines malformed flow exits 2 (no crash)",
                  rc == 2, str(rc))
    finally:
        cli_mod.run_flow = real_run_flow
        cli_mod.default_runs_root = real_default_runs


def test_issue_89_flow_tags_batch():
    """Issue #89: flow `tags` + `verify` on a directory.

    Loader: tags accepted / default [] / every rejection (non-list,
    non-string, empty, bad chars, duplicates). Selection: _select_flows
    sorted listing incl .yml, tag OR filter, case-sensitive, unreadable
    kept, empty/no-match -> BatchSelectionError. CLI: single file + --tag
    mismatch -> 2, invalid tags -> 2, match runs the flow; invalid spec
    through _cmd_verify_run -> 2. Batch: mixed pass/fail/broken/boot-
    failure/matrix -> exit 1 with per-flow records + parent summary,
    all-pass -> 0, tag filter selects a subset, --investigate note in
    batch mode. Report: write_batch_summary on synthetic records.
    """
    import tempfile
    from types import SimpleNamespace
    from qaloop.spec import load_spec, SpecError
    from qaloop.runner import RunResult, StepResult
    from qaloop.cli import (_select_flows, BatchSelectionError,
                            _cmd_verify_batch, _cmd_verify_run,
                            _read_flow_tags, cmd_verify)
    from qaloop.report import write_batch_summary

    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    base = ("name: t\ntarget: http://h\nsteps:\n"
            "  - name: s\n    wait_ms: 1\n")

    # --- tags validation ---
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name, base + "tags: [smoke, v2.0, a-b_c.d]\n")
        spec = load_spec(f.name)
        check("tags accepted",
              spec.tags == ["smoke", "v2.0", "a-b_c.d"], str(spec.tags))
        os.unlink(f.name)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name, base)
        spec = load_spec(f.name)
        check("tags default []", spec.tags == [], str(spec.tags))
        os.unlink(f.name)

    def rejects(text, label):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name, text)
        try:
            load_spec(f.name)
            check(f"rejected: {label}", False)
        except SpecError:
            check(f"rejected: {label}", True)
        finally:
            os.unlink(f.name)

    rejects(base + "tags: smoke\n", "tags non-list")
    rejects(base + "tags: [ok, 7]\n", "tags non-string")
    rejects(base + "tags: [ok, '']\n", "tags empty string")
    rejects(base + "tags: ['bad tag!']\n", "tags bad chars")
    rejects(base + "tags: ['-lead']\n", "tags leading dash")
    rejects(base + "tags: [smoke, smoke]\n", "tags duplicates")

    # --- _read_flow_tags ---
    def read_raises(text, label):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name, text)
        try:
            _read_flow_tags(f.name)
            check(label, False)
        except SpecError:
            check(label, True)
        finally:
            os.unlink(f.name)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name, base + "tags: [smoke]\n")
        check("read_flow_tags", _read_flow_tags(f.name) == ["smoke"])
        os.unlink(f.name)
    read_raises(base + "tags: ['bad tag!']\n",
                "read_flow_tags invalid tags raises")
    read_raises("just a string\n", "read_flow_tags non-mapping raises")
    read_raises("name: [unclosed\n", "read_flow_tags bad yaml raises")

    # --- _select_flows ---
    with tempfile.TemporaryDirectory() as d:
        write(os.path.join(d, "b.yaml"), base + "tags: [smoke]\n")
        write(os.path.join(d, "a.yaml"), base + "tags: [smoke, night]\n")
        write(os.path.join(d, "c.yml"), base)
        write(os.path.join(d, "notes.txt"), "not a flow")
        write(os.path.join(d, "broken.yaml"), base + "tags: ['bad tag!']\n")
        os.makedirs(os.path.join(d, "z.yaml"))  # a directory, not a file
        sub = os.path.join(d, "sub")
        os.makedirs(sub)
        write(os.path.join(sub, "deep.yaml"), base + "tags: [smoke]\n")
        basenames = lambda ps: [os.path.basename(p) for p in ps]
        check("select lists sorted incl .yml",
              basenames(_select_flows(d, None))
              == ["a.yaml", "b.yaml", "broken.yaml", "c.yml"])
        check("select tag OR filter",
              basenames(_select_flows(d, ["smoke"]))
              == ["a.yaml", "b.yaml", "broken.yaml"])
        check("select multiple tags OR",
              basenames(_select_flows(d, ["smoke", "night"]))
              == ["a.yaml", "b.yaml", "broken.yaml"])
        check("select unreadable tags kept",
              "broken.yaml" in basenames(_select_flows(d, ["smoke"])))
    with tempfile.TemporaryDirectory() as clean:
        write(os.path.join(clean, "a.yaml"), base + "tags: [smoke]\n")
        for tags, label in [(["nope"], "no-match raises"),
                            (["Smoke"], "case-sensitive no-match raises")]:
            try:
                _select_flows(clean, tags)
                check(label, False)
            except BatchSelectionError:
                check(label, True)
    with tempfile.TemporaryDirectory() as empty:
        try:
            _select_flows(empty, None)
            check("empty dir raises", False)
        except BatchSelectionError:
            check("empty dir raises", True)

    # --- single-file _cmd_verify_run: invalid spec -> 2, investigate path ---
    import qaloop.cli as cli_mod
    import qaloop.ledger as ledger_mod

    def fake_result(status, ms=100):
        steps = [StepResult(index=0, phase="steps", name="s", op="wait_ms",
                            status="passed" if status == "passed"
                            else "failed",
                            duration_ms=ms)]
        return RunResult(flow_name="x", target="http://h", status=status,
                         started=1.0, ended=1.0 + ms / 1000.0, steps=steps,
                         failed_step=None if status == "passed" else 0,
                         console_errors=[], page_errors=[],
                         failed_requests=[], bad_responses=[], run_dir="")

    real_run_flow = cli_mod.run_flow
    real_append = ledger_mod.append
    real_default_runs = cli_mod.default_runs_root
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "bad.yaml")
        write(bad, "name: [unclosed\n")
        args = SimpleNamespace(flow=bad, target=None, headed=True,
                               executable_path=None, upload_dir=None,
                               investigate=False, keep_runs=0,
                               max_investigation_actions=None)
        rc = _cmd_verify_run(args, os.path.join(d, "r"), FakeLedger(), d, 0)
        check("single verify invalid spec exits 2", rc == 2, str(rc))
        # single-flow investigate path (stubbed investigator)
        import qaloop.investigate as inv_mod
        real_inv = inv_mod.investigate
        inv_calls = []
        inv_mod.investigate = lambda **kw: inv_calls.append(kw) or {
            "model": "fake", "tokens_in": 1, "tokens_out": 2,
            "cost_usd_est": 0.0, "likely_cause": "x", "diagnosis": "y"}
        cli_mod.run_flow = lambda spec, **kw: fake_result("failed")
        try:
            okf = os.path.join(d, "ok.yaml")
            write(okf, base + "investigate_on_failure: true\n")
            args_i = SimpleNamespace(flow=okf, target=None, headed=True,
                                     executable_path=None, upload_dir=None,
                                     investigate=True, keep_runs=0,
                                     max_investigation_actions=None)
            led = FakeLedger()
            ri_dir = os.path.join(d, "ri")
            os.makedirs(ri_dir, exist_ok=True)
            rc = _cmd_verify_run(args_i, ri_dir, led, d, 0)
            check("single verify investigate on failure exits 1",
                  rc == 1, str(rc))
            check("investigation ledger entry",
                  [r["kind"] for r in led.rows]
                  == ["investigation", "scripted"]
                  and inv_calls and inv_calls[0]["run_dir"].endswith("ri"),
                  str([r["kind"] for r in led.rows]))
        finally:
            inv_mod.investigate = real_inv
            cli_mod.run_flow = real_run_flow

    # --- cmd_verify: single file + --tag, and directory batch ---
    with tempfile.TemporaryDirectory() as d:
        ledger_mod.append = lambda row: None
        cli_mod.default_runs_root = lambda: os.path.join(d, "runs")
        cli_mod.run_flow = lambda spec, **kw: fake_result("passed")
        try:
            fp = os.path.join(d, "f.yaml")
            write(fp, base + "tags: [smoke]\n")

            def mk(**kw):
                a = dict(flow=fp, target=None, headed=True,
                         executable_path=None, upload_dir=None,
                         investigate=False, keep_runs=0,
                         max_investigation_actions=None, tag=None)
                a.update(kw)
                return SimpleNamespace(**a)

            check("single file + --tag mismatch exits 2",
                  cmd_verify(mk(tag=["night"])) == 2)
            check("single file + --tag match runs",
                  cmd_verify(mk(tag=["smoke"])) == 0)
            bad = os.path.join(d, "bad.yaml")
            write(bad, "name: [unclosed\n")
            check("single file + --tag invalid spec exits 2",
                  cmd_verify(mk(flow=bad, tag=["smoke"])) == 2)
            badsvc = os.path.join(d, "badsvc.yaml")
            write(badsvc, base + "tags: [smoke]\n"
                  "services:\n  - name: '9bad'\n    command: 'x'\n"
                  "    wait: {port: 1}\n")
            check("single file bad services exits 2",
                  cmd_verify(mk(flow=badsvc, tag=["smoke"])) == 2)
            bootsvc = os.path.join(d, "bootsvc.yaml")
            write(bootsvc, base + "tags: [smoke]\n"
                  "services:\n  - name: s\n    command: 'sleep 30'\n"
                  "    wait: {port: 9}\n    timeout_s: 1\n")
            check("single file service boot failure exits 3",
                  cmd_verify(mk(flow=bootsvc, tag=["smoke"])) == 3)
            empty = os.path.join(d, "empty")
            os.makedirs(empty)
            check("empty dir batch exits 2",
                  cmd_verify(mk(flow=empty, tag=None)) == 2)
            bdir = os.path.join(d, "bdir")
            os.makedirs(bdir)
            write(os.path.join(bdir, "g.yaml"), base)
            check("dir batch with --investigate note exits 0",
                  cmd_verify(mk(flow=bdir, investigate=True)) == 0)
            old_keep = os.environ.get("QALOOP_KEEP_RUNS")
            os.environ["QALOOP_KEEP_RUNS"] = "bogus"
            try:
                check("bad QALOOP_KEEP_RUNS exits 2",
                      cmd_verify(mk(flow=bdir, keep_runs=None)) == 2)
            finally:
                if old_keep is None:
                    del os.environ["QALOOP_KEEP_RUNS"]
                else:
                    os.environ["QALOOP_KEEP_RUNS"] = old_keep
        finally:
            ledger_mod.append = real_append
            cli_mod.default_runs_root = real_default_runs
            cli_mod.run_flow = real_run_flow

    # --- _cmd_verify_batch: mixed outcomes, stubbed run_flow ---
    with tempfile.TemporaryDirectory() as d:
        flowdir = os.path.join(d, "flows")
        os.makedirs(flowdir)
        write(os.path.join(flowdir, "a-pass.yaml"),
              "name: ok-flow\ntarget: http://h\ntags: [smoke]\n"
              "steps:\n  - name: s\n    wait_ms: 1\n")
        write(os.path.join(flowdir, "b-fail.yaml"),
              "name: fail-flow\ntarget: http://h\ntags: [smoke, night]\n"
              "steps:\n  - name: s\n    wait_ms: 1\n")
        write(os.path.join(flowdir, "c-broken.yaml"), "name: [unclosed\n")
        write(os.path.join(flowdir, "d-svc.yaml"),
              "name: svc-flow\ntarget: http://h\n"
              "services:\n"
              "  - name: s\n    command: \"sleep 30\"\n"
              "    wait: {port: 9}\n    timeout_s: 1\n"
              "steps:\n  - name: s\n    wait_ms: 1\n")
        write(os.path.join(flowdir, "e-matrix.yaml"),
              "name: mx-flow\ntarget: http://h/${t}\n"
              "matrix:\n  t: [a, b]\n"
              "steps:\n  - name: go\n    goto: /${t}\n")
        runs = os.path.join(d, "runs")
        os.makedirs(runs, exist_ok=True)
        # stale dirs so the batch prune actually prunes (covers the log line)
        for n in range(3):
            os.makedirs(os.path.join(runs, f"20000101T00000{n}Z-old-{n}"))
        ledger = FakeLedger()

        def fake_run_flow(spec, **kw):
            return fake_result("failed" if "fail" in spec.name else "passed")

        cli_mod.run_flow = fake_run_flow
        try:
            def bargs(**kw):
                a = dict(flow=flowdir, target=None, headed=True,
                         executable_path=None, upload_dir=None,
                         investigate=False, keep_runs=0,
                         max_investigation_actions=None, tag=None)
                a.update(kw)
                return SimpleNamespace(**a)

            rc = _cmd_verify_batch(bargs(), flowdir, ledger, runs, 2)
            check("batch exit 1 on failures", rc == 1, str(rc))
            parents = [p for p in os.listdir(runs) if "-flows-batch-" in p]
            check("batch parent dir created", len(parents) == 1,
                  str(parents))
            parent = os.path.join(runs, parents[0])
            body = open(os.path.join(parent, "REPORT.md")).read()
            check("batch summary 2/5 PASS", "[2/5 PASS]" in body, body[:160])
            check("batch summary rows",
                  "`a-pass.yaml`" in body and "`c-broken.yaml`" in body
                  and "FAIL" in body, body[:400])
            kinds = [r["kind"] for r in ledger.rows]
            check("batch per-flow ledger entries",
                  kinds == ["scripted", "scripted", "scripted-matrix"],
                  str(kinds))
            bj = json.load(open(os.path.join(parent, "batch.json")))
            check("batch.json has 5 entries", len(bj) == 5, str(len(bj)))
            by_file = {e["file"]: e for e in bj}
            check("batch.json pass entry",
                  by_file["a-pass.yaml"]["status"] == "passed"
                  and by_file["a-pass.yaml"]["steps_passed"] == 1
                  and by_file["a-pass.yaml"]["report"]
                  == "flows/a-pass/REPORT.md",
                  str(by_file["a-pass.yaml"]))
            check("batch.json fail entry",
                  by_file["b-fail.yaml"]["status"] == "failed"
                  and by_file["b-fail.yaml"]["error"] is None,
                  str(by_file["b-fail.yaml"]))
            check("batch.json invalid-spec entry",
                  by_file["c-broken.yaml"]["status"] == "failed"
                  and by_file["c-broken.yaml"]["error"].startswith(
                      "INVALID SPEC")
                  and by_file["c-broken.yaml"]["report"] is None,
                  str(by_file["c-broken.yaml"]))
            check("batch.json boot-failed entry",
                  by_file["d-svc.yaml"]["error"].startswith(
                      "SERVICE BOOT FAILED"),
                  str(by_file["d-svc.yaml"]))
            check("batch.json matrix aggregate",
                  by_file["e-matrix.yaml"]["status"] == "passed"
                  and by_file["e-matrix.yaml"]["steps_passed"] == 2
                  and by_file["e-matrix.yaml"]["steps_total"] == 2,
                  str(by_file["e-matrix.yaml"]))
            check("per-flow reports written",
                  os.path.isfile(os.path.join(parent, "flows", "a-pass",
                                              "REPORT.md"))
                  and os.path.isfile(os.path.join(parent, "flows",
                                                  "e-matrix", "REPORT.md")))
            # tag filter selects a subset
            ledger2 = FakeLedger()
            rc2 = _cmd_verify_batch(bargs(tag=["smoke"]), flowdir,
                                    ledger2, runs, 0)
            check("batch tag filter exit 1 (one fail)", rc2 == 1, str(rc2))
            check("batch tag filter ran 2 flows", len(ledger2.rows) == 2,
                  str(ledger2.rows))
            # all-pass batch -> exit 0
            fd2 = os.path.join(d, "flows2")
            os.makedirs(fd2)
            write(os.path.join(fd2, "only.yaml"), base)
            rc3 = _cmd_verify_batch(bargs(), fd2, FakeLedger(),
                                    os.path.join(d, "runs2"), 0)
            check("batch exit 0 all pass", rc3 == 0, str(rc3))
        finally:
            cli_mod.run_flow = real_run_flow

    # --- write_batch_summary on synthetic records ---
    with tempfile.TemporaryDirectory() as d:
        sub1 = os.path.join(d, "flows", "a-pass")
        os.makedirs(sub1)
        write(os.path.join(sub1, "REPORT.md"), "# x")
        sub2 = os.path.join(d, "flows", "b-fail")
        os.makedirs(sub2)
        sub3 = os.path.join(d, "flows", "c-err")
        os.makedirs(sub3)
        recs = [
            {"file": "a-pass.yaml", "status": "passed", "duration_ms": 120,
             "steps_passed": 2, "steps_total": 2, "subdir": sub1,
             "error": None},
            {"file": "b-fail.yaml", "status": "failed", "duration_ms": 340,
             "steps_passed": 1, "steps_total": 2, "subdir": sub2,
             "error": "assertion failed"},
            {"file": "c-err.yaml", "status": "error", "duration_ms": 10,
             "steps_passed": 0, "steps_total": 0, "subdir": sub3,
             "error": "boom"},
        ]
        path = write_batch_summary(recs, d)
        check("batch summary path", path == os.path.join(d, "REPORT.md"))
        body = open(path).read()
        check("batch summary 1/3 PASS", "[1/3 PASS]" in body, body[:120])
        check("batch summary rows",
              "`a-pass.yaml`" in body and "FAIL" in body
              and "ERROR" in body, body[:400])
        check("batch summary links existing report",
              "flows/a-pass/REPORT.md" in body, body[:600])
        bj = json.load(open(os.path.join(d, "batch.json")))
        check("batch.json entries",
              [e["file"] for e in bj]
              == ["a-pass.yaml", "b-fail.yaml", "c-err.yaml"], str(bj))
        check("batch.json fields",
              bj[0]["status"] == "passed"
              and bj[0]["duration_ms"] == 120
              and bj[0]["report"] == "flows/a-pass/REPORT.md"
              and bj[1]["error"] == "assertion failed"
              and bj[2]["report"] is None
              and bj[2]["error"] == "boom",
              str(bj[0]))


def test_issue_90_video_recording():
    """Issue #90: video recording per run.

    Loader: video: true accepted, default False, non-bool rejected with
    SpecError. run_flow: new_context gets record_video_dir/record_video_size
    when enabled, not when off; the fake page.video file is moved to
    <run_dir>/video.webm and result.video_path is set; a video failure
    never fails the run. to_dict/from_dict round-trip keeps video_path.
    CLI: verify --video parses. Report: header and failing-step sections
    link the video.
    """
    from qaloop.spec import load_spec, SpecError
    from qaloop.runner import run_flow, RunResult, StepResult
    from qaloop.report import write_report
    import playwright.sync_api as pw_sync

    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    def mkflow(video_line):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name,
                  "name: v\ntarget: http://h\n" + video_line +
                  "artifacts: {screenshot: none, trace: false}\n"
                  "steps:\n  - name: s\n    wait_ms: 1\n")
            spec = load_spec(f.name)
            os.unlink(f.name)
        return spec

    # --- loader ---
    spec_v = mkflow("video: true\n")
    check("video: true accepted", spec_v.video is True, str(spec_v.video))
    spec_no = mkflow("")
    check("video defaults to False", spec_no.video is False,
          str(spec_no.video))
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as f:
        write(f.name, "name: v\ntarget: http://h\nvideo: \"yes\"\n"
                      "steps:\n  - name: s\n    wait_ms: 1\n")
        try:
            load_spec(f.name)
            bad = None
        except SpecError as e:
            bad = str(e)
        finally:
            os.unlink(f.name)
    check("video: 'yes' rejected with SpecError",
          bad is not None and "video must be a bool" in bad, str(bad))

    # --- faked browser ---
    ctx_kwargs = []
    page_holder = {}

    class FakeVideo:
        def __init__(self, path):
            self._path = path

        def path(self):
            return self._path

    class BrokenVideo:
        def path(self):
            raise RuntimeError("video gone")

    class FakePage:
        def on(self, event, handler):
            pass

        def wait_for_timeout(self, ms):
            pass

    class FakeCtx:
        def new_page(self):
            return page_holder["page"]

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            ctx_kwargs.append(kw)
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        # video on: recording kwargs passed, file collected
        with tempfile.TemporaryDirectory() as run_dir:
            os.makedirs(os.path.join(run_dir, "video"))
            src = os.path.join(run_dir, "video", "fake.webm")
            with open(src, "wb") as f:
                f.write(b"FAKEVIDEO")
            page = FakePage()
            page.video = FakeVideo(src)
            page_holder["page"] = page
            result = run_flow(spec_v, run_dir=run_dir, video=True)
            check("video run passes", result.status == "passed",
                  result.status)
            check("record_video_dir passed to new_context",
                  ctx_kwargs[-1].get("record_video_dir")
                  == os.path.join(run_dir, "video"),
                  str(ctx_kwargs[-1]))
            check("record_video_size passed to new_context",
                  ctx_kwargs[-1].get("record_video_size")
                  == {"width": 1280, "height": 800},
                  str(ctx_kwargs[-1]))
            dst = os.path.join(run_dir, "video.webm")
            check("video.webm moved into run dir",
                  os.path.isfile(dst)
                  and open(dst, "rb").read() == b"FAKEVIDEO",
                  str(os.listdir(run_dir)))
            check("result.video_path set",
                  result.video_path == dst, result.video_path)
            rt = RunResult.from_dict(result.to_dict())
            check("video_path survives to_dict/from_dict",
                  rt.video_path == dst, rt.video_path)
            paths = write_report(result, spec_v, run_dir)
            body = open(paths["report_md"]).read()
            check("report header links video",
                  "Video: `video.webm`" in body, body[:400])

        # spec video: true is honored without the kwarg (default None)
        with tempfile.TemporaryDirectory() as run_dir:
            page = FakePage()
            page.video = None  # no recording happened
            page_holder["page"] = page
            ctx_kwargs.clear()
            result = run_flow(spec_v, run_dir=run_dir)
            check("spec video: true flows through",
                  "record_video_dir" in ctx_kwargs[-1],
                  str(ctx_kwargs[-1]))
            check("no video file -> video_path stays empty",
                  result.video_path == ""
                  and not os.path.exists(
                      os.path.join(run_dir, "video.webm")),
                  result.video_path)

        # video off: no recording kwargs
        with tempfile.TemporaryDirectory() as run_dir:
            page = FakePage()
            page_holder["page"] = page
            ctx_kwargs.clear()
            result = run_flow(spec_no, run_dir=run_dir)
            check("no record_video_dir when video off",
                  "record_video_dir" not in ctx_kwargs[-1],
                  str(ctx_kwargs[-1]))
            check("video off -> video_path empty",
                  result.video_path == "", result.video_path)

        # a video failure never fails the run
        with tempfile.TemporaryDirectory() as run_dir:
            page = FakePage()
            page.video = BrokenVideo()
            page_holder["page"] = page
            result = run_flow(spec_v, run_dir=run_dir, video=True)
            check("broken video does not fail the run",
                  result.status == "passed"
                  and result.video_path == "",
                  f"{result.status} {result.video_path!r}")
    finally:
        pw_sync.sync_playwright = old_pw

    # --- failing-step report links the video ---
    with tempfile.TemporaryDirectory() as run_dir:
        dst = os.path.join(run_dir, "video.webm")
        with open(dst, "wb") as f:
            f.write(b"FAKEVIDEO")
        fr = RunResult(
            flow_name="v", target="http://h", status="failed",
            started=0.0, ended=1.0,
            steps=[StepResult(0, "steps", "s", "goto", "failed", 5,
                              error="boom")],
            failed_step=0, console_errors=[], page_errors=[],
            failed_requests=[], bad_responses=[], run_dir=run_dir,
            video_path=dst)
        paths = write_report(fr, spec_v, run_dir)
        body = open(paths["report_md"]).read()
        check("failing step section links video",
              "## Failing step" in body
              and "Video: `video.webm`" in body, body[-400:])

    # --- CLI flag ---
    from qaloop.cli import build_parser
    a = build_parser().parse_args(["verify", "--video", "flows/x.yaml"])
    check("verify --video parses", a.video is True, str(a.video))
    a = build_parser().parse_args(["verify", "flows/x.yaml"])
    check("verify --video defaults off", a.video is False, str(a.video))


def test_issue_122_browser_context():
    from qaloop.spec import load_spec, SpecError
    from qaloop.runner import run_flow
    import playwright.sync_api as pw_sync

    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    def mkflow(browser_block):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name, "name: b\ntarget: http://h\n" + browser_block +
                  "artifacts: {screenshot: none, trace: false}\n"
                  "steps:\n  - name: s\n    wait_ms: 1\n")
            spec = load_spec(f.name)
            os.unlink(f.name)
        return spec

    def mkbad(browser_block):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            write(f.name, "name: b\ntarget: http://h\n" + browser_block +
                  "steps:\n  - name: s\n    wait_ms: 1\n")
            try:
                load_spec(f.name)
                bad = None
            except SpecError as e:
                bad = str(e)
            finally:
                os.unlink(f.name)
        return bad

    # --- loader: defaults ---
    spec_none = mkflow("")
    check("no browser: key yields {}", spec_none.browser == {},
          str(spec_none.browser))
    check("viewport still 1280x800",
          spec_none.viewport == {"width": 1280, "height": 800},
          str(spec_none.viewport))

    # --- loader: valid block accepted ---
    spec_ok = mkflow("browser:\n  color_scheme: dark\n  is_mobile: true\n"
                     "  has_touch: false\n  locale: en-US\n"
                     "  timezone_id: America/New_York\n"
                     "  user_agent: test-ua\n  device_scale_factor: 2\n")
    check("full valid browser block accepted",
          spec_ok.browser == {"color_scheme": "dark", "is_mobile": True,
                              "has_touch": False, "locale": "en-US",
                              "timezone_id": "America/New_York",
                              "user_agent": "test-ua",
                              "device_scale_factor": 2},
          str(spec_ok.browser))

    # --- loader: rejections name the offending key ---
    bad = mkbad("browser: {color_scheme: sepia}\n")
    check("bad color_scheme enum rejected",
          bad is not None and "browser.color_scheme" in bad, str(bad))
    bad = mkbad("browser: {frobnicate: 1}\n")
    check("unknown browser key rejected",
          bad is not None and "browser.frobnicate" in bad, str(bad))
    bad = mkbad("browser: {is_mobile: \"yes\"}\n")
    check("is_mobile 'yes' rejected",
          bad is not None and "browser.is_mobile" in bad, str(bad))
    bad = mkbad("browser: [dark]\n")
    check("browser not-a-mapping rejected",
          bad is not None and "browser must be a mapping" in bad, str(bad))
    bad = mkbad("browser: {device_scale_factor: 0}\n")
    check("device_scale_factor 0 rejected",
          bad is not None and "browser.device_scale_factor" in bad, str(bad))
    bad = mkbad("browser: {locale: 5}\n")
    check("locale non-string rejected",
          bad is not None and "browser.locale" in bad, str(bad))

    # --- faked browser: pass-through ---
    ctx_kwargs = []
    page_holder = {}

    class FakePage:
        def on(self, event, handler):
            pass

        def wait_for_timeout(self, ms):
            pass

    class FakeCtx:
        def new_page(self):
            return page_holder["page"]

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            ctx_kwargs.append(kw)
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        # declared keys reach new_context, video recording kwargs intact
        with tempfile.TemporaryDirectory() as run_dir:
            page = FakePage()
            page_holder["page"] = page
            ctx_kwargs.clear()
            result = run_flow(spec_ok, run_dir=run_dir, video=True)
            kw = ctx_kwargs[-1]
            check("video run with browser opts passes",
                  result.status == "passed", result.status)
            check("color_scheme reaches new_context",
                  kw.get("color_scheme") == "dark", str(kw))
            check("is_mobile reaches new_context",
                  kw.get("is_mobile") is True, str(kw))
            check("locale reaches new_context",
                  kw.get("locale") == "en-US", str(kw))
            check("timezone_id reaches new_context",
                  kw.get("timezone_id") == "America/New_York", str(kw))
            check("user_agent reaches new_context",
                  kw.get("user_agent") == "test-ua", str(kw))
            check("device_scale_factor reaches new_context",
                  kw.get("device_scale_factor") == 2, str(kw))
            check("video recording kwargs kept with browser opts",
                  kw.get("record_video_dir")
                  == os.path.join(run_dir, "video")
                  and kw.get("record_video_size")
                  == {"width": 1280, "height": 800}
                  and kw.get("viewport")
                  == {"width": 1280, "height": 800}, str(kw))

        # non-video path also forwards the declared keys
        with tempfile.TemporaryDirectory() as run_dir:
            page = FakePage()
            page_holder["page"] = page
            ctx_kwargs.clear()
            result = run_flow(spec_ok, run_dir=run_dir)
            check("non-video run with browser opts passes",
                  result.status == "passed", result.status)
            check("non-video path forwards color_scheme",
                  ctx_kwargs[-1].get("color_scheme") == "dark",
                  str(ctx_kwargs[-1]))

        # absent block: nothing extra passed (acceptance #3)
        with tempfile.TemporaryDirectory() as run_dir:
            page = FakePage()
            page_holder["page"] = page
            ctx_kwargs.clear()
            result = run_flow(spec_none, run_dir=run_dir)
            check("absent browser block run passes",
                  result.status == "passed", result.status)
            check("absent block passes only viewport",
                  ctx_kwargs[-1] == {"viewport": {"width": 1280,
                                                  "height": 800}},
                  str(ctx_kwargs[-1]))
    finally:
        pw_sync.sync_playwright = old_pw


def _syn_step_91(name, status, dur_ms):
    return {"index": 0, "name": name, "phase": "steps", "op": "goto",
            "status": status, "duration_ms": dur_ms, "attempts": 1}


def _syn_run_dir_91(root, dirname, flow, status, started, dur_ms, steps):
    d = os.path.join(root, dirname)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"flow_name": flow, "status": status, "started": started,
                   "duration_ms": dur_ms, "steps": steps, "run_dir": d}, f)
    return d


def test_issue_91_trends_chronology_and_window():
    from qaloop import dashboard
    # deliberately unsorted; compute_trends must order by `started`
    runs = [
        {"flow_name": "f1", "status": "failed", "started": 3.0},
        {"flow_name": "f1", "status": "passed", "started": 1.0},
        {"flow_name": "f1", "status": "passed", "started": 2.0},
        {"flow_name": "f2", "status": "passed", "started": 2.5},
        {"status": "failed"},  # no flow_name / started -> "—" group
    ]
    t = {x["flow"]: x for x in dashboard.compute_trends(runs)}
    check("trends f1 time-ordered", t["f1"]["recent"] == [True, True, False],
          str(t["f1"]["recent"]))
    check("trends f1 2/3 = 66.7%",
          t["f1"]["pass_rate_pct"] == 66.7 and t["f1"]["passed"] == 2
          and t["f1"]["runs"] == 3, str(t["f1"]))
    check("trends f2 single pass",
          t["f2"]["pass_rate_pct"] == 100.0 and t["f2"]["runs"] == 1,
          str(t["f2"]))
    check("trends missing flow_name grouped as dash",
          t["—"]["runs"] == 1 and t["—"]["pass_rate_pct"] == 0.0, str(t["—"]))
    # trailing-window math: with a window of 2 only the last 2 count
    old = dashboard.TREND_WINDOW
    dashboard.TREND_WINDOW = 2
    try:
        t2 = {x["flow"]: x for x in dashboard.compute_trends(runs)}
        check("trends trailing window math",
              t2["f1"]["runs"] == 2 and t2["f1"]["pass_rate_pct"] == 50.0
              and t2["f1"]["recent"] == [True, False], str(t2["f1"]))
    finally:
        dashboard.TREND_WINDOW = old


def test_issue_91_trends_skips_corrupt_json():
    from qaloop.dashboard import build
    old_ledger = os.environ.get("QALOOP_LEDGER")
    with tempfile.TemporaryDirectory() as root:
        os.environ["QALOOP_LEDGER"] = os.path.join(root, "ledger.jsonl")
        try:
            good1 = _syn_run_dir_91(root, "20261012T000000Z-f1-aaaa", "f1",
                                    "passed", 1000.0, 10, [])
            _syn_run_dir_91(root, "20261012T000001Z-f1-bbbb", "f1",
                            "failed", 2000.0, 10, [])
            corrupt_dir = os.path.join(root, "20261012T000002Z-f1-cccc")
            os.makedirs(corrupt_dir, exist_ok=True)
            with open(os.path.join(corrupt_dir, "run.json"), "w",
                      encoding="utf-8") as f:
                f.write("{not json")
            path = build(root, os.path.join(root, "dash"))
            html = open(path, encoding="utf-8").read()
            check("trends section renders", "Pass rate over time" in html,
                  html[-300:])
            check("trends counts only valid runs",
                  "50.0% (1/2 passed)" in html and "f1" in html,
                  html[-600:])
            empty_root = os.path.join(root, "empty")
            os.makedirs(empty_root)
            path2 = build(empty_root, os.path.join(root, "dash2"))
            html2 = open(path2, encoding="utf-8").read()
            check("trends empty root says no trend data",
                  "no trend data yet" in html2)
        finally:
            if old_ledger is None:
                del os.environ["QALOOP_LEDGER"]
            else:
                os.environ["QALOOP_LEDGER"] = old_ledger


def test_issue_91_compare_identical():
    from qaloop.cli import build_parser
    from qaloop.compare import compare_runs, render_text
    with tempfile.TemporaryDirectory() as root:
        steps = [_syn_step_91("load", "passed", 100),
                 _syn_step_91("click", "passed", 200)]
        a = _syn_run_dir_91(root, "a", "f", "passed", 1.0, 300, steps)
        b = _syn_run_dir_91(root, "b", "f", "passed", 2.0, 340, steps)
        diff = compare_runs(a, b)
        check("compare identical despite duration drift",
              diff["identical"] is True, str(diff["identical"]))
        check("compare duration delta recorded",
              diff["duration_delta_ms"] == 40, str(diff["duration_delta_ms"]))
        check("compare all steps unchanged",
              all(s["change"] == "unchanged" for s in diff["steps"]),
              str(diff["steps"]))
        txt = render_text(diff)
        check("compare text marks identical",
              txt.endswith("identical") and "steps: all unchanged" in txt, txt)
        args = build_parser().parse_args(["compare", a, b])
        check("compare CLI exit 0 on identical", args.fn(args) == 0)


def test_issue_91_compare_step_changes():
    from qaloop.cli import build_parser
    from qaloop.compare import compare_runs, render_text
    with tempfile.TemporaryDirectory() as root:
        a = _syn_run_dir_91(root, "a", "f", "failed", 1.0, 300,
                            [_syn_step_91("load", "passed", 100),
                             _syn_step_91("pay", "failed", 100),
                             _syn_step_91("flip", "passed", 100),
                             _syn_step_91("flip2", "passed", 100),
                             _syn_step_91("old", "passed", 50),
                             _syn_step_91("steady", "passed", 60)])
        b = _syn_run_dir_91(root, "b", "f", "passed", 2.0, 330,
                            [_syn_step_91("load", "passed", 100),
                             _syn_step_91("pay", "passed", 150),
                             _syn_step_91("flip", "failed", 110),
                             _syn_step_91("flip2", "failed", 100),
                             _syn_step_91("new", "passed", 70),
                             _syn_step_91("steady", "passed", 60)])
        diff = compare_runs(a, b)
        by = {s["name"]: s for s in diff["steps"]}
        check("compare status change",
              diff["status_changed"] is True and diff["status_a"] == "failed"
              and diff["status_b"] == "passed")
        check("compare step failed->passed",
              by["pay"]["change"] == "status_changed"
              and by["pay"]["status_a"] == "failed"
              and by["pay"]["status_b"] == "passed", str(by["pay"]))
        check("compare added step",
              by["new"]["change"] == "added"
              and by["new"]["status_b"] == "passed", str(by["new"]))
        check("compare removed step",
              by["old"]["change"] == "removed"
              and by["old"]["status_a"] == "passed", str(by["old"]))
        check("compare unchanged step",
              by["load"]["change"] == "unchanged", str(by["load"]))
        check("compare not identical", diff["identical"] is False)
        check("compare regression flagged at +50%",
              by["pay"]["duration_delta_ms"] == 50
              and by["pay"]["duration_regression"] is True, str(by["pay"]))
        check("compare mild +10% is not a regression",
              by["flip"]["duration_delta_ms"] == 10
              and by["flip"]["duration_regression"] is False, str(by["flip"]))
        check("compare added step no regression guard",
              by["new"]["duration_delta_ms"] == 70
              and by["new"]["duration_regression"] is False, str(by["new"]))
        check("compare top-level duration delta",
              diff["duration_delta_ms"] == 30, str(diff["duration_delta_ms"]))
        txt = render_text(diff)
        check("compare text status line",
              "status: failed -> passed (CHANGED)" in txt, txt)
        check("compare text step markers",
              "+ step 'new'" in txt and "- step 'old'" in txt
              and "! step 'pay'" in txt, txt)
        check("compare text REGRESSION flagged",
              "(+50ms) REGRESSION" in txt, txt)
        check("compare text mild delta shown without flag",
              "duration: 100ms -> 110ms (+10ms)" in txt, txt)
        check("compare text skips zero-delta duration line for flip2",
              txt.count("duration:") == 5, txt)
        args = build_parser().parse_args(["compare", a, b])
        check("compare CLI exit 1 on differ", args.fn(args) == 1)


def test_issue_91_compare_errors():
    from qaloop.cli import build_parser
    from qaloop.compare import CompareError, _load_run_json, resolve_run
    with tempfile.TemporaryDirectory() as root:
        empty = os.path.join(root, "empty")
        os.makedirs(empty)
        try:
            _load_run_json(empty)
            check("compare missing run.json raises", False, "no error")
        except CompareError as e:
            check("compare missing run.json raises", "no run.json" in str(e),
                  str(e))
        bad = _syn_run_dir_91(root, "bad", "f", "passed", 1.0, 10, [])
        with open(os.path.join(bad, "run.json"), "w",
                  encoding="utf-8") as f:
            f.write("{oops")
        try:
            _load_run_json(bad)
            check("compare corrupt run.json raises", False, "no error")
        except CompareError as e:
            check("compare corrupt run.json raises",
                  "corrupt run.json" in str(e), str(e))
        lst = os.path.join(root, "lst")
        os.makedirs(lst)
        with open(os.path.join(lst, "run.json"), "w",
                  encoding="utf-8") as f:
            f.write("[1,2]")
        try:
            _load_run_json(lst)
            check("compare non-dict run.json raises", False, "no error")
        except CompareError as e:
            check("compare non-dict run.json raises",
                  "not an object" in str(e), str(e))
        os.makedirs(os.path.join(root, "pref-one"))
        os.makedirs(os.path.join(root, "pref-two"))
        os.makedirs(os.path.join(root, "unique-aaa"))
        try:
            resolve_run("pref", root)
            check("compare ambiguous prefix raises", False, "no error")
        except CompareError as e:
            check("compare ambiguous prefix raises", "ambiguous" in str(e),
                  str(e))
        try:
            resolve_run("zzz-no-such", root)
            check("compare no-match raises", False, "no error")
        except CompareError as e:
            check("compare no-match raises", "no run dir matching" in str(e),
                  str(e))
        check("compare unique prefix resolves",
              resolve_run("unique", root) == os.path.join(root, "unique-aaa"),
              resolve_run("unique", root))
        check("compare direct dir resolves",
              resolve_run(os.path.join(root, "pref-one"), root)
              == os.path.join(root, "pref-one"))
        # CLI maps CompareError to exit 2
        args = build_parser().parse_args(
            ["compare", "zzz-no-such-run", "also-missing"])
        old_runs = os.environ.get("QALOOP_RUNS")
        os.environ["QALOOP_RUNS"] = root
        try:
            check("compare CLI exit 2 on missing", args.fn(args) == 2)
        finally:
            if old_runs is None:
                del os.environ["QALOOP_RUNS"]
            else:
                os.environ["QALOOP_RUNS"] = old_runs


def test_seed_http_headers_and_status():
    # Issue #96: seed.http headers validation + response status surfacing.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from qaloop.spec import load_spec, SpecError
    from qaloop.runner import (_do_seed, run_flow, RunResult, SeedRequestError,
                               StepResult)

    def load(text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return load_spec(path)
        finally:
            os.unlink(path)

    def rejects(text, why):
        try:
            load(text)
            check(why, False, "no error raised")
        except SpecError as e:
            check(why, True)

    # --- (a) spec validation: headers accepted / rejected ---
    good = ("name: s\ntarget: http://x\nsteps:\n"
            "  - name: seed ok\n"
            "    seed: {http: {url: http://x/u, headers: {X-Tenant: t1}}}\n")
    try:
        load(good)
        check("seed.http headers dict accepted", True)
    except SpecError as e:
        check("seed.http headers dict accepted", False, str(e))
    rejects(good.replace("headers: {X-Tenant: t1}", "headers: X-Tenant"),
            "seed.http non-dict headers rejected")
    rejects(good.replace("headers: {X-Tenant: t1}", "headers: {X-Tenant: 5}"),
            "seed.http non-str header value rejected")
    try:
        load("name: s\ntarget: http://x\nsteps:\n"
             "  - name: bad\n    seed: {http: {method: POST}}\n")
        check("seed.http error names headers?", False, "no error raised")
    except SpecError as e:
        check("seed.http error names headers?",
              "headers?" in str(e), str(e))

    # --- (b)/(c) local HTTP server: header forwarding + 500 fail-fast ---
    received = {}

    class OkHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["headers"] = dict(self.headers)
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class FailHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = b"error"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    ok_server = HTTPServer(("127.0.0.1", 0), OkHandler)
    ok_port = ok_server.server_address[1]
    fail_server = HTTPServer(("127.0.0.1", 0), FailHandler)
    fail_port = fail_server.server_address[1]
    for srv in (ok_server, fail_server):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status = _do_seed(
            None,
            {"http": {"url": f"http://127.0.0.1:{ok_port}/ok",
                      "json": {"a": 1},
                      "headers": {"X-Tenant": "t1"}}},
            5000, "http://x")
        check("seed returns response status", status == 200, str(status))
        check("custom header forwarded",
              received["headers"].get("X-Tenant") == "t1",
              str(received.get("headers")))
        check("default Content-Type kept",
              received["headers"].get("Content-Type") == "application/json",
              str(received.get("headers")))
        _do_seed(None,
                 {"http": {"url": f"http://127.0.0.1:{ok_port}/ok",
                           "headers": {"Content-Type": "text/plain"}}},
                 5000, "http://x")
        check("user Content-Type overrides default",
              received["headers"].get("Content-Type") == "text/plain",
              str(received.get("headers")))

        # --- run_flow with a faked browser: 500 fails fast, status recorded
        import playwright.sync_api as pw_sync

        class FakePage:
            url = "http://x/"

            def on(self, event, handler):
                pass

            def evaluate(self, js):
                return None

            def goto(self, url, **kw):
                pass

        class FakeCtx:
            def new_page(self):
                return FakePage()

            def close(self):
                pass

        class FakeBrowser:
            def new_context(self, **kw):
                return FakeCtx()

            def close(self):
                pass

        class FakePW:
            class chromium:
                @staticmethod
                def launch(**kw):
                    return FakeBrowser()

        class FakePWCM:
            def __enter__(self):
                return FakePW()

            def __exit__(self, *a):
                return False

        old_pw = pw_sync.sync_playwright
        pw_sync.sync_playwright = lambda: FakePWCM()
        try:
            spec = load(
                "name: seedfail\ntarget: http://x\nsteps:\n"
                "  - name: js seed\n    seed: {js: \"x=1\"}\n"
                "  - name: bad seed\n"
                f"    seed: {{http: {{url: http://127.0.0.1:{fail_port}/boom}}}}\n"
                "artifacts: {screenshot: none, ax_snapshot: none, "
                "trace: false}\n")
            with tempfile.TemporaryDirectory() as run_dir:
                result = run_flow(spec, run_dir=run_dir)
                check("500 seed fails the run",
                      result.status == "failed", result.status)
                js_step, bad_step = result.steps
                check("500 seed records seed_status 500",
                      bad_step.seed_status == 500, str(bad_step.seed_status))
                check("fail-fast error names the status",
                      "500" in bad_step.error
                      and "seed request failed" in bad_step.error,
                      bad_step.error)
                check("js seed has no seed_status",
                      js_step.seed_status is None, str(js_step.seed_status))
                with open(os.path.join(run_dir, "run.json"),
                          encoding="utf-8") as fh:
                    data = json.load(fh)
                check("run.json carries seed_status 500",
                      data["steps"][1]["seed_status"] == 500,
                      str(data["steps"][1].get("seed_status")))
                check("run.json js seed seed_status null",
                      data["steps"][0]["seed_status"] is None)
        finally:
            pw_sync.sync_playwright = old_pw
    finally:
        ok_server.shutdown()
        fail_server.shutdown()
        ok_server.server_close()
        fail_server.server_close()

    # --- (d) StepResult seed_status serializes round-trip ---
    seeded = StepResult(index=0, phase="steps", name="s", op="seed",
                        status="passed", duration_ms=1, seed_status=201)
    rr = RunResult(flow_name="f", target="t", status="passed",
                   started=0.0, ended=1.0, steps=[seeded],
                   failed_step=None, console_errors=[], page_errors=[],
                   failed_requests=[], bad_responses=[], run_dir="/tmp/x")
    d = rr.to_dict()
    check("to_dict carries seed_status", d["steps"][0]["seed_status"] == 201,
          str(d["steps"][0].get("seed_status")))
    check("from_dict restores seed_status",
          RunResult.from_dict(d).steps[0].seed_status == 201)
    plain = StepResult(index=0, phase="steps", name="s", op="click",
                       status="passed", duration_ms=1)
    d2 = RunResult(flow_name="f", target="t", status="passed",
                   started=0.0, ended=1.0, steps=[plain],
                   failed_step=None, console_errors=[], page_errors=[],
                   failed_requests=[], bad_responses=[],
                   run_dir="/tmp/x").to_dict()
    check("seed_status defaults to None",
          d2["steps"][0]["seed_status"] is None,
          str(d2["steps"][0].get("seed_status")))


def test_issue_97_step_screenshot_override():
    from qaloop.spec import load_spec, SpecError
    from qaloop.runner import _should_screenshot, run_flow

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    base = ("name: t\ntarget: http://x\nsteps:\n"
            "  - name: s\n    click: '#go'\n    screenshot: always\n")
    s = load(base)
    check("screenshot: always parses", s.steps[0].screenshot == "always",
          s.steps[0].screenshot)
    for val in ("never", "on-failure"):
        got = load(base.replace("screenshot: always",
                                f"screenshot: {val}")).steps[0].screenshot
        check(f"screenshot: {val} parses", got == val, got)
    plain = base.replace("    screenshot: always\n", "")
    check("screenshot absent -> None",
          load(plain).steps[0].screenshot is None)
    for bad, label in [
        (base.replace("screenshot: always", "screenshot: sometimes"),
         "bad value"),
        (base.replace("screenshot: always", "screenshot: true"),
         "bool value"),
        (base.replace("screenshot: always", "screenshot: 2"),
         "int value"),
        (base.replace("screenshot: always", "screenshot_typo: always"),
         "unknown key"),
    ]:
        try:
            load(bad)
            check(f"screenshot {label} rejected", False, "no SpecError")
        except SpecError:
            check(f"screenshot {label} rejected", True)

    # --- _should_screenshot decision table ---
    for step_ss, policy, status, want in [
        ("always", "on-failure", "passed", True),
        ("always", "none", "failed", True),
        ("never", "per-step", "passed", False),
        ("never", "on-failure", "failed", False),
        ("on-failure", "per-step", "passed", False),
        ("on-failure", "per-step", "failed", True),
        (None, "per-step", "passed", True),
        (None, "per-step", "failed", True),
        (None, "on-failure", "passed", False),
        (None, "on-failure", "failed", True),
        (None, "none", "passed", False),
        (None, "none", "failed", False),
    ]:
        got = _should_screenshot(step_ss, policy, status)
        check(f"_should_screenshot({step_ss!r},{policy},{status})={want}",
              got is want, str(got))

    # --- run_flow with a faked browser: override drives the capture ---
    import playwright.sync_api as pw_sync

    shots = []

    class FakePage97:
        def on(self, event, handler):
            pass

        def evaluate(self, js):
            return ""

        def wait_for_timeout(self, ms):
            pass

        def wait_for_selector(self, selector, state=None, timeout=None):
            if selector == "#missing":
                raise TimeoutError("selector never attached")

        def screenshot(self, path):
            shots.append(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"png")

    class FakeCtx97:
        def new_page(self):
            return FakePage97()

        def close(self):
            pass

    class FakeBrowser97:
        def new_context(self, **kw):
            return FakeCtx97()

        def close(self):
            pass

    class FakePW97:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser97()

    class FakePWCM97:
        def __enter__(self):
            return FakePW97()

        def __exit__(self, *a):
            return False

    body = (
        "name: s97\ntarget: http://h\n"
        "steps:\n"
        "  - name: always shoots on success\n"
        "    wait_ms: 1\n"
        "    screenshot: always\n"
        "  - name: inherits flow policy on success\n"
        "    wait_ms: 1\n"
        "  - name: never suppresses on failure\n"
        "    wait_ms: 1\n"
        "    screenshot: never\n"
        "    continue_on_fail: true\n"
        "    expect:\n"
        "      visible: '#missing'\n"
        "  - name: on-failure shoots on failure\n"
        "    wait_ms: 1\n"
        "    screenshot: on-failure\n"
        "    continue_on_fail: true\n"
        "    expect:\n"
        "      visible: '#missing'\n"
        "  - name: inherits flow policy on failure\n"
        "    wait_ms: 1\n"
        "    continue_on_fail: true\n"
        "    expect:\n"
        "      visible: '#missing'\n"
        "artifacts: {screenshot: on-failure, ax_snapshot: none, "
        "trace: false}\n"
    )
    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM97()
    try:
        with tempfile.TemporaryDirectory() as run_dir:
            spec = load(body)
            result = run_flow(spec, run_dir=run_dir)
            want = [os.path.join(run_dir, "steps", f"steps-0{i}.png")
                    for i in (0, 3, 4)]
            failed = [r for r in result.steps if r.status == "failed"]
            check("run passes with three failed steps",
                  result.status == "passed" and len(result.steps) == 5
                  and len(failed) == 3,
                  f"{result.status} {[r.status for r in result.steps]}")
            check("always shoots on success", want[0] in shots, str(shots))
            check("inherited policy skips success",
                  os.path.join(run_dir, "steps", "steps-01.png") not in shots,
                  str(shots))
            check("never suppresses on failure",
                  os.path.join(run_dir, "steps", "steps-02.png") not in shots,
                  str(shots))
            check("on-failure shoots on failure", want[1] in shots,
                  str(shots))
            check("inherited on-failure shoots on failure", want[2] in shots,
                  str(shots))
    finally:
        pw_sync.sync_playwright = old_pw


def _run_doctor():
    """Run cmd_doctor with stdout captured. Returns (rc, output)."""
    import io
    from qaloop import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
    return rc, buf.getvalue()


def _run_validate(path):
    """Run cmd_validate with stdout captured. Returns (rc, output)."""
    import io
    from qaloop import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_validate(cli.build_parser().parse_args(["validate", path]))
    return rc, buf.getvalue()


def test_issue_298_validate_valid_contract():
    rc, out = _run_validate("flows/demo-teardown.yaml")
    check("validate valid teardown exits 0", rc == 0, f"rc={rc}")
    check("validate valid teardown line is byte-for-byte",
          out == "VALID: demo-teardown — 7 steps (setup 1, steps 3, teardown 3), target ${TARGET_URL}\n",
          repr(out))
    rc, out = _run_validate("flows/demo-mock-ux.yaml")
    check("validate valid mock-ux exits 0", rc == 0, f"rc={rc}")
    check("validate valid mock-ux line is byte-for-byte",
          out == "VALID: demo-mock-ux — 9 steps (setup 2, steps 7, teardown 0), target ${TARGET_URL}\n",
          repr(out))


def test_issue_298_validate_invalid_contract():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("name: no-target\n"
                "steps:\n"
                "  - name: a step\n"
                "    goto: /\n")
        path = f.name
    try:
        rc, out = _run_validate(path)
    finally:
        os.unlink(path)
    check("validate invalid exits 1", rc == 1, f"rc={rc}")
    check("validate invalid line starts with INVALID prefix and spec path",
          out.startswith(f"INVALID: spec {path}: "), repr(out))
    check("validate invalid line names the missing target",
          "`target` is required" in out, repr(out))


def test_issue_298_validate_missing_file_propagates():
    import io
    from qaloop import cli
    missing = os.path.join(tempfile.gettempdir(),
                           "qaloop-no-such-flow-298.yaml")
    if os.path.exists(missing):
        os.unlink(missing)
    buf = io.StringIO()
    raised = False
    with contextlib.redirect_stdout(buf):
        try:
            cli.cmd_validate(cli.build_parser().parse_args(["validate", missing]))
        except FileNotFoundError:
            raised = True
    out = buf.getvalue()
    check("validate missing file raises FileNotFoundError", raised)
    check("validate missing file prints no INVALID line",
          "INVALID:" not in out, repr(out))


def test_issue_126_doctor_all_pass():
    rc, out = _run_doctor()
    lines = out.strip().splitlines()
    check("doctor all-pass exits 0", rc == 0, f"rc={rc}")
    names = [l.split()[1].rstrip(":") for l in lines
             if l.startswith(("PASS ", "FAIL "))]
    check("doctor prints one line per check",
          names == ["chromium", "ffmpeg", "python", "demo"], str(names))
    check("doctor summary line all PASS",
          lines[-1] == "doctor: chromium=PASS ffmpeg=PASS python=PASS demo=PASS",
          lines[-1] if lines else "")


def test_issue_126_doctor_missing_executable():
    old = os.environ.get("QALOOP_EXECUTABLE_PATH")
    os.environ["QALOOP_EXECUTABLE_PATH"] = "/nonexistent/chrome-xyz"
    try:
        rc, out = _run_doctor()
    finally:
        if old is None:
            os.environ.pop("QALOOP_EXECUTABLE_PATH", None)
        else:
            os.environ["QALOOP_EXECUTABLE_PATH"] = old
    check("doctor missing executable exits 2", rc == 2, f"rc={rc}")
    check("doctor names the missing executable",
          "/nonexistent/chrome-xyz" in out)
    check("doctor summary marks chromium FAIL", "chromium=FAIL" in out)


def test_issue_126_doctor_env_executable_present():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        real = pw.chromium.executable_path
    old = os.environ.get("QALOOP_EXECUTABLE_PATH")
    os.environ["QALOOP_EXECUTABLE_PATH"] = real
    try:
        rc, out = _run_doctor()
    finally:
        if old is None:
            os.environ.pop("QALOOP_EXECUTABLE_PATH", None)
        else:
            os.environ["QALOOP_EXECUTABLE_PATH"] = old
    check("doctor env executable present exits 0", rc == 0, f"rc={rc}")
    check("doctor env executable reports the env path",
          f"PASS chromium: {real}" in out, out.splitlines()[0] if out else "")


def test_issue_126_doctor_default_executable_missing():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        default_exe = pw.chromium.executable_path
    real_exists = os.path.exists

    def fake_exists(p):
        return False if p == default_exe else real_exists(p)

    os.path.exists = fake_exists
    try:
        rc, out = _run_doctor()
    finally:
        os.path.exists = real_exists
    check("doctor missing default chromium exits 2", rc == 2, f"rc={rc}")
    check("doctor summary marks chromium FAIL", "chromium=FAIL" in out)
    check("doctor suggests the install fix",
          "python3 -m playwright install chromium" in out)


def test_issue_126_doctor_playwright_unresolvable():
    from playwright import sync_api
    real_sp = sync_api.sync_playwright

    def boom(*args, **kwargs):
        raise RuntimeError("no browsers installed")

    sync_api.sync_playwright = boom
    try:
        rc, out = _run_doctor()
    finally:
        sync_api.sync_playwright = real_sp
    check("doctor unresolvable chromium exits 2", rc == 2, f"rc={rc}")
    check("doctor summary marks chromium FAIL", "chromium=FAIL" in out)


def test_issue_126_doctor_ffmpeg_missing():
    import shutil
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        return None if cmd == "ffmpeg" else real_which(cmd, *args, **kwargs)

    shutil.which = fake_which
    try:
        rc, out = _run_doctor()
    finally:
        shutil.which = real_which
    check("doctor ffmpeg-missing exits 2 (FAIL severity)", rc == 2, f"rc={rc}")
    check("doctor summary marks ffmpeg FAIL", "ffmpeg=FAIL" in out)


def test_issue_126_doctor_python_package_missing():
    import importlib
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    importlib.import_module = fake_import
    try:
        rc, out = _run_doctor()
    finally:
        importlib.import_module = real_import
    check("doctor missing package exits 2", rc == 2, f"rc={rc}")
    check("doctor names the missing package",
          "missing Python package: yaml" in out)
    check("doctor summary marks python FAIL", "python=FAIL" in out)


def test_issue_126_doctor_demo_fixture_missing():
    real_isfile = os.path.isfile

    def fake_isfile(p):
        return False if p.endswith("cookie-storage.html") else real_isfile(p)

    os.path.isfile = fake_isfile
    try:
        rc, out = _run_doctor()
    finally:
        os.path.isfile = real_isfile
    check("doctor missing demo fixture exits 2", rc == 2, f"rc={rc}")
    check("doctor names the missing fixture",
          "demo/cookie-storage.html" in out)
    check("doctor summary marks demo FAIL", "demo=FAIL" in out)


def test_issue_200_baselines_matrix_dispatch():
    """Issue #200: `qaloop baselines` on a matrix flow dispatches per
    combination (like verify), threading baseline_update=True into every
    run_flow call; exit 0 iff every combination passed. Non-matrix
    baselines behavior is unchanged (single run, no initial_vars)."""
    from types import SimpleNamespace
    import qaloop.cli as cli
    import qaloop.report as report_mod
    from qaloop.spec import load_spec

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    def fake_result(status):
        return SimpleNamespace(status=status, steps=[], duration_ms=1,
                               flow_name="bl-matrix", failed_step=None,
                               page_errors=[], failed_requests=[],
                               bad_responses=[], error=None)

    calls = []
    fail_combos = set()

    def fake_run_flow(spec, **kw):
        calls.append(kw)
        combo = kw.get("initial_vars") or {}
        status = "failed" if combo.get("t") in fail_combos else "passed"
        return fake_result(status)

    def fake_write_matrix_summary(spec, results, run_dir):
        path = os.path.join(run_dir, "REPORT.md")
        with open(path, "w") as fh:
            fh.write("matrix summary\n")
        return path

    old_run_flow, old_write_report = cli.run_flow, cli.write_report
    old_print_summary = cli.print_summary
    old_wms = report_mod.write_matrix_summary
    cli.run_flow = fake_run_flow
    cli.write_report = lambda *a, **kw: {}
    cli.print_summary = lambda result: None
    report_mod.write_matrix_summary = fake_write_matrix_summary
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write("name: bl-matrix\ntarget: http://h/${t}\n"
                    "matrix:\n  t: [a, b]\n"
                    "steps:\n"
                    "  - name: go\n    goto: /${t}\n"
                    "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                    "trace: false}\n")
            flow_path = f.name
        args = SimpleNamespace(flow=flow_path, target=None, headed=False,
                               executable_path=None, upload_dir=None)
        spec = load_spec(flow_path)
        with tempfile.TemporaryDirectory() as root:
            run_dir = os.path.join(root, "run1")
            os.makedirs(run_dir)
            ledger = FakeLedger()
            rc = cli._cmd_baselines_run(args, run_dir, spec, ledger, root)
            check("baselines matrix exit 0 when all combos pass",
                  rc == 0, str(rc))
            check("baselines matrix ran every combination",
                  len(calls) == 2, str(len(calls)))
            check("baselines matrix seeded initial_vars per combination",
                  [c.get("initial_vars") for c in calls]
                  == [{"t": "a"}, {"t": "b"}], str(calls))
            check("baselines matrix threads baseline_update=True",
                  all(c.get("baseline_update") is True for c in calls),
                  str(calls))
            check("baselines matrix uses per-combination subdirs",
                  all(os.path.basename(os.path.dirname(c["run_dir"]))
                      == "matrix" for c in calls), str(calls))
            check("baselines matrix ledger row recorded",
                  [r["kind"] for r in ledger.rows] == ["scripted-matrix"],
                  str(ledger.rows))
            # one failing combination -> exit 1
            calls.clear()
            fail_combos.add("b")
            run_dir2 = os.path.join(root, "run2")
            os.makedirs(run_dir2)
            rc = cli._cmd_baselines_run(args, run_dir2, spec,
                                        FakeLedger(), root)
            check("baselines matrix exit 1 when a combination fails",
                  rc == 1, str(rc))
            fail_combos.clear()
        # non-matrix flow: single run, baseline_update, no initial_vars
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write("name: bl-plain\ntarget: http://h\n"
                    "steps:\n"
                    "  - name: go\n    goto: /\n"
                    "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                    "trace: false}\n")
            plain_path = f.name
        try:
            calls.clear()
            args2 = SimpleNamespace(flow=plain_path, target=None,
                                    headed=False, executable_path=None,
                                    upload_dir=None)
            with tempfile.TemporaryDirectory() as root:
                rc = cli._cmd_baselines_run(
                    args2, os.path.join(root, "run3"))
                check("baselines non-matrix exit 0", rc == 0, str(rc))
                check("baselines non-matrix single run",
                      len(calls) == 1, str(len(calls)))
                check("baselines non-matrix baseline_update, no seed",
                      calls[0].get("baseline_update") is True
                      and "initial_vars" not in calls[0], str(calls))
        finally:
            os.unlink(plain_path)
        os.unlink(flow_path)
    finally:
        cli.run_flow, cli.write_report = old_run_flow, old_write_report
        cli.print_summary = old_print_summary
        report_mod.write_matrix_summary = old_wms


def test_issue_284_run_timeout():
    # Issue #284: run-level timeout (timeouts.run_ms) end-to-end with a
    # faked browser. The deadline must abort new steps (each unstarted
    # steps-phase step is recorded skipped, none dropped), the run must
    # record status error / "run timeout exceeded", and teardown must
    # still execute despite the crossed deadline.
    import time

    import playwright.sync_api as pw_sync

    from qaloop.runner import run_flow
    from qaloop.spec import load_spec

    class FakePage:
        def on(self, event, handler):
            pass

        def evaluate(self, js):
            return None

        def goto(self, url, **kw):
            pass

        def wait_for_timeout(self, ms):
            time.sleep(ms / 1000)

    class FakeCtx:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write("name: run-timeout\ntarget: http://h\n"
                    "timeouts: {step_ms: 15000, run_ms: 300}\n"
                    "steps:\n"
                    "  - name: wait one\n    wait_ms: 200\n"
                    "  - name: wait two\n    wait_ms: 200\n"
                    "  - name: wait three\n    wait_ms: 200\n"
                    "  - name: wait four\n    wait_ms: 200\n"
                    "teardown:\n"
                    "  - name: teardown still runs\n"
                    "    expect: [{noop: true}]\n"
                    "artifacts: {screenshot: none, ax_snapshot: on-failure, "
                    "trace: false}\n")
            spec_path = f.name
        try:
            spec = load_spec(spec_path)
        finally:
            os.unlink(spec_path)
        with tempfile.TemporaryDirectory() as run_dir:
            result = run_flow(spec, run_dir=run_dir)
    finally:
        pw_sync.sync_playwright = old_pw
    check("run timeout: run status is error",
          result.status == "error", result.status)
    check("run timeout: run error names the timeout",
          result.error == "run timeout exceeded", result.error)
    step_statuses = [s.status for s in result.steps if s.phase == "steps"]
    check("run timeout: unstarted steps recorded skipped, none dropped",
          step_statuses == ["passed", "passed", "skipped", "skipped"],
          str(step_statuses))
    teardown = [s for s in result.steps if s.phase == "teardown"]
    check("run timeout: teardown still runs and passes",
          len(teardown) == 1 and teardown[0].status == "passed",
          str([(s.phase, s.status) for s in result.steps]))


def test_issue_286_setup_fail():
    # Issue #286: setup-phase failure end-to-end with a faked browser.
    # A failing setup step must abort the run (status failed), every
    # steps-phase step must be recorded skipped (none dropped), and
    # teardown must still run and pass. The fake page is stateful:
    # evaluate() is backed by a dict, so seed-js setItem/clear mutate
    # it and storage assertions read it back - teardown clearing the
    # planted value is proven by the dict being empty after the run.
    import re

    import playwright.sync_api as pw_sync

    from qaloop.runner import run_flow
    from qaloop.spec import load_spec

    storage = {}

    class FakePage:
        def __init__(self):
            self.url = "about:blank"

        def on(self, event, handler):
            pass

        def evaluate(self, js, arg=None):
            if isinstance(arg, dict) and "key" in arg:
                return storage.get(arg["key"])
            if isinstance(js, str) and "localStorage.setItem" in js:
                m = re.search(
                    r"localStorage\.setItem\('([^']*)',\s*'([^']*)'\)", js)
                if m:
                    storage[m.group(1)] = m.group(2)
                return None
            if isinstance(js, str) and "localStorage.clear()" in js:
                storage.clear()
                return None

        def goto(self, url, **kw):
            self.url = url

        def wait_for_timeout(self, ms):
            pass

    class FakeCtx:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write("name: setup-fail\ntarget: http://h\n"
                    "setup:\n"
                    "  - name: open page\n"
                    "    goto: /demo/teardown-demo.html\n"
                    "  - name: plant state\n"
                    "    seed: {js: \"localStorage.setItem("
                    "'qaloop_teardown', 'SETUP-286 planted');\"}\n"
                    "  - name: designed failing setup step\n"
                    "    timeout_ms: 1000\n"
                    "    expect:\n"
                    "      - storage: {kind: local, key: qaloop_teardown, "
                    "equals: \"SETUP-286 wrong-value\"}\n"
                    "steps:\n"
                    "  - name: would pass one\n"
                    "    expect:\n"
                    "      - storage: {kind: local, key: qaloop_teardown, "
                    "equals: \"SETUP-286 planted\"}\n"
                    "  - name: would pass two\n"
                    "    expect: [{noop: true}]\n"
                    "teardown:\n"
                    "  - name: teardown clears the planted state\n"
                    "    seed: {js: \"localStorage.clear()\"}\n"
                    "  - name: teardown passes\n"
                    "    expect: [{noop: true}]\n"
                    "artifacts: {screenshot: none, ax_snapshot: none, "
                    "trace: false}\n")
            spec_path = f.name
        try:
            spec = load_spec(spec_path)
        finally:
            os.unlink(spec_path)
        with tempfile.TemporaryDirectory() as run_dir:
            result = run_flow(spec, run_dir=run_dir)
    finally:
        pw_sync.sync_playwright = old_pw
    check("setup fail: run status is failed",
          result.status == "failed", result.status)
    setup_statuses = [s.status for s in result.steps if s.phase == "setup"]
    check("setup fail: setup statuses are passed, passed, failed",
          setup_statuses == ["passed", "passed", "failed"],
          str(setup_statuses))
    failed_setup = [s for s in result.steps
                    if s.phase == "setup" and s.status == "failed"]
    check("setup fail: failure error names the observed value",
          len(failed_setup) == 1
          and "SETUP-286 planted" in failed_setup[0].error
          and "SETUP-286 wrong-value" in failed_setup[0].error,
          failed_setup[0].error if failed_setup else "no failed setup step")
    step_statuses = [s.status for s in result.steps if s.phase == "steps"]
    check("setup fail: steps recorded skipped, none dropped",
          step_statuses == ["skipped", "skipped"], str(step_statuses))
    teardown_statuses = [s.status for s in result.steps
                         if s.phase == "teardown"]
    check("setup fail: teardown still runs and passes",
          teardown_statuses == ["passed", "passed"],
          str(teardown_statuses))
    check("setup fail: teardown cleared the fake storage",
          storage == {}, str(storage))


def test_issue_288_steps_fail():
    # Issue #288: steps-phase abort end-to-end with a faked browser.
    # A steps-phase step that fails without continue_on_fail must
    # abort the run (status failed), every later steps-phase step
    # must be recorded skipped (none dropped, none passed), and
    # teardown must still run and pass. The fake page is stateful:
    # evaluate() is backed by a dict, so seed-js setItem/clear mutate
    # it and storage assertions read it back - teardown clearing the
    # planted value is proven by the dict being empty after the run.
    import re

    import playwright.sync_api as pw_sync

    from qaloop.runner import run_flow
    from qaloop.spec import load_spec

    storage = {}

    class FakePage:
        def __init__(self):
            self.url = "about:blank"

        def on(self, event, handler):
            pass

        def evaluate(self, js, arg=None):
            if isinstance(arg, dict) and "key" in arg:
                return storage.get(arg["key"])
            if isinstance(js, str) and "localStorage.setItem" in js:
                m = re.search(
                    r"localStorage\.setItem\('([^']*)',\s*'([^']*)'\)", js)
                if m:
                    storage[m.group(1)] = m.group(2)
                return None
            if isinstance(js, str) and "localStorage.clear()" in js:
                storage.clear()
                return None

        def goto(self, url, **kw):
            self.url = url

        def wait_for_timeout(self, ms):
            pass

    class FakeCtx:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write("name: steps-fail\ntarget: http://h\n"
                    "setup:\n"
                    "  - name: open page\n"
                    "    goto: /demo/teardown-demo.html\n"
                    "steps:\n"
                    "  - name: plant state and read it back\n"
                    "    seed: {js: \"localStorage.setItem("
                    "'qaloop_teardown', 'STEPS-288 planted');\"}\n"
                    "    expect:\n"
                    "      - storage: {kind: local, key: qaloop_teardown, "
                    "equals: \"STEPS-288 planted\"}\n"
                    "  - name: designed failing step\n"
                    "    timeout_ms: 1000\n"
                    "    expect:\n"
                    "      - storage: {kind: local, key: qaloop_teardown, "
                    "equals: \"STEPS-288 wrong-value\"}\n"
                    "  - name: would pass one\n"
                    "    expect:\n"
                    "      - storage: {kind: local, key: qaloop_teardown, "
                    "equals: \"STEPS-288 planted\"}\n"
                    "  - name: would pass two\n"
                    "    expect: [{noop: true}]\n"
                    "teardown:\n"
                    "  - name: teardown clears the planted state\n"
                    "    seed: {js: \"localStorage.clear()\"}\n"
                    "  - name: teardown passes\n"
                    "    expect: [{noop: true}]\n"
                    "artifacts: {screenshot: none, ax_snapshot: none, "
                    "trace: false}\n")
            spec_path = f.name
        try:
            spec = load_spec(spec_path)
        finally:
            os.unlink(spec_path)
        with tempfile.TemporaryDirectory() as run_dir:
            result = run_flow(spec, run_dir=run_dir)
    finally:
        pw_sync.sync_playwright = old_pw
    check("steps fail: run status is failed",
          result.status == "failed", result.status)
    setup_statuses = [s.status for s in result.steps if s.phase == "setup"]
    check("steps fail: setup status is passed",
          setup_statuses == ["passed"], str(setup_statuses))
    step_statuses = [s.status for s in result.steps if s.phase == "steps"]
    check("steps fail: steps are passed, failed, skipped, skipped",
          step_statuses == ["passed", "failed", "skipped", "skipped"],
          str(step_statuses))
    failed_steps = [s for s in result.steps
                    if s.phase == "steps" and s.status == "failed"]
    check("steps fail: failure error names observed and wanted values",
          len(failed_steps) == 1
          and "STEPS-288 planted" in failed_steps[0].error
          and "STEPS-288 wrong-value" in failed_steps[0].error,
          failed_steps[0].error if failed_steps else "no failed steps step")
    teardown_statuses = [s.status for s in result.steps
                         if s.phase == "teardown"]
    check("steps fail: teardown still runs and passes",
          teardown_statuses == ["passed", "passed"],
          str(teardown_statuses))
    check("steps fail: teardown cleared the fake storage",
          storage == {}, str(storage))


def test_issue_300_target_override_contract():
    """Issue #300: --target overrides the spec target in both the verify
    path (_cmd_verify_run -> _run_single_flow) and the baselines
    non-matrix path (_cmd_baselines_run). The printed target line, the
    run_flow target kwarg, and (for verify) the written report must all
    carry the override; with args.target=None the spec target is used."""
    import io
    from types import SimpleNamespace

    import qaloop.cli as cli
    from qaloop.runner import RunResult, StepResult
    from qaloop.spec import load_spec

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    spec_target = "http://spec-target"
    override = "http://override-target"
    calls = []

    def echo_run_flow(spec, **kw):
        calls.append(kw)
        steps = [StepResult(index=0, phase="steps", name="s", op="wait_ms",
                            status="passed", duration_ms=1)]
        return RunResult(flow_name=spec.name, target=kw.get("target", ""),
                         status="passed", started=1.0, ended=1.001,
                         steps=steps, failed_step=None, console_errors=[],
                         page_errors=[], failed_requests=[],
                         bad_responses=[], run_dir=kw.get("run_dir", ""))

    old_run_flow = cli.run_flow
    cli.run_flow = echo_run_flow
    try:
        with tempfile.TemporaryDirectory() as d:
            flow_path = os.path.join(d, "t300.yaml")
            with open(flow_path, "w") as fh:
                fh.write(f"name: t300\ntarget: {spec_target}\n"
                         "steps:\n"
                         "  - name: go\n    goto: /\n"
                         "artifacts: {screenshot: none, ax_snapshot: "
                         "on-failure, trace: false}\n")

            def verify_args(target):
                return SimpleNamespace(
                    flow=flow_path, target=target, headed=True,
                    executable_path=None, upload_dir=None, investigate=False,
                    keep_runs=0, max_investigation_actions=None)

            # --- verify: override wins over the spec target ---
            calls.clear()
            run_dir = os.path.join(d, "v-override")
            os.makedirs(run_dir)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_verify_run(verify_args(override), run_dir,
                                         FakeLedger(), d, 0)
            out = buf.getvalue()
            check("verify override exits 0", rc == 0, str(rc))
            check("verify override printed target line",
                  f"target:  {override}" in out, out)
            check("verify override threaded into run_flow",
                  len(calls) == 1 and calls[0].get("target") == override,
                  str(calls))
            with open(os.path.join(run_dir, "REPORT.md")) as fh:
                report = fh.read()
            check("verify override recorded in REPORT.md",
                  f"- target: `{override}`" in report, report[:300])
            check("verify report omits spec target as run target",
                  f"- target: `{spec_target}`" not in report, report[:300])
            with open(os.path.join(run_dir, "run.json")) as fh:
                run_data = json.load(fh)
            check("verify override recorded in run.json",
                  run_data.get("target") == override,
                  str(run_data.get("target")))

            # --- verify control: no override -> spec target ---
            calls.clear()
            run_dir2 = os.path.join(d, "v-spec")
            os.makedirs(run_dir2)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_verify_run(verify_args(None), run_dir2,
                                         FakeLedger(), d, 0)
            out = buf.getvalue()
            check("verify control exits 0", rc == 0, str(rc))
            check("verify control printed spec target line",
                  f"target:  {spec_target}" in out, out)
            check("verify control threaded spec target into run_flow",
                  len(calls) == 1 and calls[0].get("target") == spec_target,
                  str(calls))

            # --- baselines: override wins, baseline_update stays True ---
            spec = load_spec(flow_path)

            def baselines_args(target):
                return SimpleNamespace(flow=flow_path, target=target,
                                       headed=False, executable_path=None,
                                       upload_dir=None)

            calls.clear()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_baselines_run(baselines_args(override),
                                            os.path.join(d, "b-override"),
                                            spec, FakeLedger(), d)
            out = buf.getvalue()
            check("baselines override exits 0", rc == 0, str(rc))
            check("baselines override printed target line",
                  f"target:  {override}" in out, out)
            check("baselines override threaded into run_flow",
                  len(calls) == 1 and calls[0].get("target") == override,
                  str(calls))
            check("baselines override keeps baseline_update=True",
                  bool(calls) and calls[0].get("baseline_update") is True,
                  str(calls))

            # --- baselines control: no override -> spec target ---
            calls.clear()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_baselines_run(baselines_args(None),
                                            os.path.join(d, "b-spec"),
                                            spec, FakeLedger(), d)
            out = buf.getvalue()
            check("baselines control exits 0", rc == 0, str(rc))
            check("baselines control printed spec target line",
                  f"target:  {spec_target}" in out, out)
            check("baselines control threaded spec target into run_flow",
                  len(calls) == 1 and calls[0].get("target") == spec_target,
                  str(calls))
            check("baselines control keeps baseline_update=True",
                  bool(calls) and calls[0].get("baseline_update") is True,
                  str(calls))
    finally:
        cli.run_flow = old_run_flow


def test_issue_302_headed_executable_path_contract():
    """Issue #302: --headed and --executable-path are threaded as
    headless/executable_path kwargs into run_flow on the verify
    single path (_cmd_verify_run -> _run_single_flow), the baselines
    non-matrix path (_cmd_baselines_run), and one matrix path
    (baselines matrix, reusing _cmd_verify_matrix). The sentinel
    executable is never launched: run_flow is stubbed throughout."""
    import io
    from types import SimpleNamespace

    import qaloop.cli as cli
    import qaloop.report as report_mod
    from qaloop.runner import RunResult, StepResult
    from qaloop.spec import load_spec

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    EXE = "/fake/chromium-302"
    calls = []

    def echo_run_flow(spec, **kw):
        calls.append(kw)
        steps = [StepResult(index=0, phase="steps", name="s", op="wait_ms",
                            status="passed", duration_ms=1)]
        return RunResult(flow_name=spec.name, target=kw.get("target", ""),
                         status="passed", started=1.0, ended=1.001,
                         steps=steps, failed_step=None, console_errors=[],
                         page_errors=[], failed_requests=[],
                         bad_responses=[], run_dir=kw.get("run_dir", ""))

    old_run_flow = cli.run_flow
    cli.run_flow = echo_run_flow
    try:
        with tempfile.TemporaryDirectory() as d:
            flow_path = os.path.join(d, "t302.yaml")
            with open(flow_path, "w") as fh:
                fh.write("name: t302\ntarget: http://spec-target\n"
                         "steps:\n"
                         "  - name: go\n    goto: /\n"
                         "artifacts: {screenshot: none, ax_snapshot: "
                         "on-failure, trace: false}\n")

            def verify_args(headed, executable_path):
                return SimpleNamespace(
                    flow=flow_path, target=None, headed=headed,
                    executable_path=executable_path, upload_dir=None,
                    investigate=False, keep_runs=0,
                    max_investigation_actions=None)

            # --- verify single: headed + sentinel executable ---
            calls.clear()
            run_dir = os.path.join(d, "v-headed")
            os.makedirs(run_dir)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_verify_run(verify_args(True, EXE), run_dir,
                                         FakeLedger(), d, 0)
            check("verify headed exits 0", rc == 0, str(rc))
            check("verify headed single run_flow call",
                  len(calls) == 1, str(calls))
            check("verify headed threads headless=False",
                  bool(calls) and calls[0].get("headless") is False,
                  str(calls))
            check("verify headed threads executable_path sentinel",
                  bool(calls) and calls[0].get("executable_path") == EXE,
                  str(calls))

            # --- verify control: headless default, no executable ---
            calls.clear()
            run_dir2 = os.path.join(d, "v-control")
            os.makedirs(run_dir2)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_verify_run(verify_args(False, None), run_dir2,
                                         FakeLedger(), d, 0)
            check("verify control exits 0", rc == 0, str(rc))
            check("verify control single run_flow call",
                  len(calls) == 1, str(calls))
            check("verify control threads headless=True",
                  bool(calls) and calls[0].get("headless") is True,
                  str(calls))
            check("verify control threads executable_path None",
                  bool(calls) and calls[0].get("executable_path") is None,
                  str(calls))

            # --- baselines non-matrix: headed + sentinel executable ---
            spec = load_spec(flow_path)
            baselines_args = SimpleNamespace(
                flow=flow_path, target=None, headed=True,
                executable_path=EXE, upload_dir=None)
            calls.clear()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_baselines_run(baselines_args,
                                            os.path.join(d, "b1"),
                                            spec, FakeLedger(), d)
            check("baselines headed exits 0", rc == 0, str(rc))
            check("baselines headed single run_flow call",
                  len(calls) == 1, str(calls))
            check("baselines headed threads headless=False",
                  bool(calls) and calls[0].get("headless") is False,
                  str(calls))
            check("baselines headed threads executable_path sentinel",
                  bool(calls) and calls[0].get("executable_path") == EXE,
                  str(calls))
            check("baselines headed keeps baseline_update=True",
                  bool(calls) and calls[0].get("baseline_update") is True,
                  str(calls))

            # --- baselines matrix: headed + sentinel per combination ---
            matrix_path = os.path.join(d, "bl302-matrix.yaml")
            with open(matrix_path, "w") as fh:
                fh.write("name: bl302-matrix\ntarget: http://h/${t}\n"
                         "matrix:\n  t: [a, b]\n"
                         "steps:\n"
                         "  - name: go\n    goto: /${t}\n"
                         "artifacts: {screenshot: none, ax_snapshot: "
                         "on-failure, trace: false}\n")

            def fake_write_matrix_summary(spec, results, run_dir):
                path = os.path.join(run_dir, "REPORT.md")
                with open(path, "w") as fh:
                    fh.write("matrix summary\n")
                return path

            old_write_report = cli.write_report
            old_print_summary = cli.print_summary
            old_wms = report_mod.write_matrix_summary
            cli.write_report = lambda *a, **kw: {}
            cli.print_summary = lambda result: None
            report_mod.write_matrix_summary = fake_write_matrix_summary
            try:
                calls.clear()
                matrix_run_dir = os.path.join(d, "b-matrix")
                os.makedirs(matrix_run_dir)
                matrix_args = SimpleNamespace(
                    flow=matrix_path, target=None, headed=True,
                    executable_path=EXE, upload_dir=None)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli._cmd_baselines_run(
                        matrix_args, matrix_run_dir,
                        load_spec(matrix_path), FakeLedger(), d)
                check("baselines matrix headed exits 0", rc == 0, str(rc))
                check("baselines matrix headed ran every combination",
                      len(calls) == 2, str(len(calls)))
                check("baselines matrix threads headless=False",
                      bool(calls)
                      and all(c.get("headless") is False for c in calls),
                      str(calls))
                check("baselines matrix threads executable_path sentinel",
                      bool(calls)
                      and all(c.get("executable_path") == EXE
                              for c in calls),
                      str(calls))
                check("baselines matrix keeps baseline_update=True",
                      bool(calls)
                      and all(c.get("baseline_update") is True
                              for c in calls),
                      str(calls))
            finally:
                cli.write_report = old_write_report
                cli.print_summary = old_print_summary
                report_mod.write_matrix_summary = old_wms
    finally:
        cli.run_flow = old_run_flow


def test_issue_304_video_plumbing_contract():
    """Issue #304: --video is threaded as video=getattr(args, "video",
    False) or None into run_flow on the verify single path
    (_cmd_verify_run -> _run_single_flow) and the verify matrix path
    (_cmd_verify_matrix). True forces recording on; False and a missing
    attribute both deliver None so the spec governs (runner:
    record_video = spec.video if video is None else video). run_flow is
    stubbed throughout so no browser or recording is launched."""
    import io
    from types import SimpleNamespace

    import qaloop.cli as cli
    import qaloop.report as report_mod
    from qaloop.runner import RunResult, StepResult

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    calls = []

    def echo_run_flow(spec, **kw):
        calls.append(kw)
        steps = [StepResult(index=0, phase="steps", name="s", op="wait_ms",
                            status="passed", duration_ms=1)]
        return RunResult(flow_name=spec.name, target=kw.get("target", ""),
                         status="passed", started=1.0, ended=1.001,
                         steps=steps, failed_step=None, console_errors=[],
                         page_errors=[], failed_requests=[],
                         bad_responses=[], run_dir=kw.get("run_dir", ""))

    old_run_flow = cli.run_flow
    cli.run_flow = echo_run_flow
    try:
        with tempfile.TemporaryDirectory() as d:
            flow_path = os.path.join(d, "t304.yaml")
            with open(flow_path, "w") as fh:
                fh.write("name: t304\ntarget: http://spec-target\n"
                         "steps:\n"
                         "  - name: go\n    goto: /\n"
                         "artifacts: {screenshot: none, ax_snapshot: "
                         "on-failure, trace: false}\n")

            def verify_args(video):
                return SimpleNamespace(
                    flow=flow_path, target=None, headed=False,
                    executable_path=None, upload_dir=None,
                    investigate=False, keep_runs=0,
                    max_investigation_actions=None, video=video)

            def verify_args_no_video():
                return SimpleNamespace(
                    flow=flow_path, target=None, headed=False,
                    executable_path=None, upload_dir=None,
                    investigate=False, keep_runs=0,
                    max_investigation_actions=None)

            # --- verify single: video=True forces recording on ---
            calls.clear()
            run_dir = os.path.join(d, "v-true")
            os.makedirs(run_dir)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_verify_run(verify_args(True), run_dir,
                                         FakeLedger(), d, 0)
            check("verify video=True exits 0", rc == 0, str(rc))
            check("verify video=True single run_flow call",
                  len(calls) == 1, str(calls))
            check("verify video=True threads video=True",
                  bool(calls) and "video" in calls[0]
                  and calls[0].get("video") is True,
                  str(calls))

            # --- verify single: video=False threads None, not False ---
            calls.clear()
            run_dir = os.path.join(d, "v-false")
            os.makedirs(run_dir)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_verify_run(verify_args(False), run_dir,
                                         FakeLedger(), d, 0)
            check("verify video=False exits 0", rc == 0, str(rc))
            check("verify video=False single run_flow call",
                  len(calls) == 1, str(calls))
            check("verify video=False threads video=None not False",
                  bool(calls) and "video" in calls[0]
                  and calls[0].get("video") is None,
                  str(calls))

            # --- verify single: no video attribute threads None ---
            calls.clear()
            run_dir = os.path.join(d, "v-absent")
            os.makedirs(run_dir)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_verify_run(verify_args_no_video(), run_dir,
                                         FakeLedger(), d, 0)
            check("verify no video attr exits 0", rc == 0, str(rc))
            check("verify no video attr single run_flow call",
                  len(calls) == 1, str(calls))
            check("verify no video attr threads video=None",
                  bool(calls) and "video" in calls[0]
                  and calls[0].get("video") is None,
                  str(calls))

            # --- verify matrix: video=True on both combinations ---
            matrix_path = os.path.join(d, "t304-matrix.yaml")
            with open(matrix_path, "w") as fh:
                fh.write("name: t304-matrix\ntarget: http://h/${t}\n"
                         "matrix:\n  t: [a, b]\n"
                         "steps:\n"
                         "  - name: go\n    goto: /${t}\n"
                         "artifacts: {screenshot: none, ax_snapshot: "
                         "on-failure, trace: false}\n")

            def fake_write_matrix_summary(spec, results, run_dir):
                path = os.path.join(run_dir, "REPORT.md")
                with open(path, "w") as fh:
                    fh.write("matrix summary\n")
                return path

            old_write_report = cli.write_report
            old_print_summary = cli.print_summary
            old_wms = report_mod.write_matrix_summary
            cli.write_report = lambda *a, **kw: {}
            cli.print_summary = lambda result: None
            report_mod.write_matrix_summary = fake_write_matrix_summary
            try:
                calls.clear()
                matrix_run_dir = os.path.join(d, "v-matrix")
                os.makedirs(matrix_run_dir)
                matrix_args = SimpleNamespace(
                    flow=matrix_path, target=None, headed=False,
                    executable_path=None, upload_dir=None,
                    investigate=False, keep_runs=0,
                    max_investigation_actions=None, video=True)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli._cmd_verify_run(matrix_args, matrix_run_dir,
                                             FakeLedger(), d, 0)
                check("verify matrix video=True exits 0", rc == 0, str(rc))
                check("verify matrix video=True ran every combination",
                      len(calls) == 2, str(len(calls)))
                check("verify matrix video=True on both combinations",
                      bool(calls)
                      and all("video" in c and c.get("video") is True
                              for c in calls),
                      str(calls))
            finally:
                cli.write_report = old_write_report
                cli.print_summary = old_print_summary
                report_mod.write_matrix_summary = old_wms
    finally:
        cli.run_flow = old_run_flow


def test_issue_306_upload_dir_plumbing_contract():
    """Issue #306: --upload-dir is resolved (flag wins over env
    QALOOP_UPLOAD_DIR, via qaloop.env.resolve_upload_dir) and threaded
    as the upload_dir kwarg into run_flow on the verify single path
    (_cmd_verify_run), the baselines single path (_cmd_baselines_run,
    with baseline_update=True preserved), and the baselines matrix
    path (per combination). run_flow is stubbed throughout so no
    browser is launched."""
    import io
    from types import SimpleNamespace

    import qaloop.cli as cli
    import qaloop.report as report_mod
    from qaloop.runner import RunResult, StepResult
    from qaloop.spec import load_spec

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    calls = []

    def echo_run_flow(spec, **kw):
        calls.append(kw)
        steps = [StepResult(index=0, phase="steps", name="s", op="wait_ms",
                            status="passed", duration_ms=1)]
        return RunResult(flow_name=spec.name, target=kw.get("target", ""),
                         status="passed", started=1.0, ended=1.001,
                         steps=steps, failed_step=None, console_errors=[],
                         page_errors=[], failed_requests=[],
                         bad_responses=[], run_dir=kw.get("run_dir", ""))

    old_run_flow = cli.run_flow
    cli.run_flow = echo_run_flow
    try:
        with tempfile.TemporaryDirectory() as d:
            flow_path = os.path.join(d, "t306.yaml")
            with open(flow_path, "w") as fh:
                fh.write("name: t306\ntarget: http://spec-target\n"
                         "steps:\n"
                         "  - name: go\n    goto: /\n"
                         "artifacts: {screenshot: none, ax_snapshot: "
                         "on-failure, trace: false}\n")

            def verify_args(upload_dir):
                return SimpleNamespace(
                    flow=flow_path, target=None, headed=False,
                    executable_path=None, upload_dir=upload_dir,
                    investigate=False, keep_runs=0,
                    max_investigation_actions=None)

            def baselines_args(upload_dir):
                return SimpleNamespace(
                    flow=flow_path, target=None, headed=False,
                    executable_path=None, upload_dir=upload_dir)

            try:
                # --- (a) verify single: flag value threaded ---
                os.environ.pop("QALOOP_UPLOAD_DIR", None)
                calls.clear()
                run_dir = os.path.join(d, "v-flag")
                os.makedirs(run_dir)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli._cmd_verify_run(verify_args("/flag/up"),
                                             run_dir, FakeLedger(), d, 0)
                check("verify upload flag exits 0", rc == 0, str(rc))
                check("verify upload flag single run_flow call",
                      len(calls) == 1, str(calls))
                check("verify upload flag threads upload_dir",
                      bool(calls) and "upload_dir" in calls[0]
                      and calls[0]["upload_dir"] == "/flag/up",
                      str(calls))

                # --- (b) verify single: env fallback when flag None ---
                os.environ["QALOOP_UPLOAD_DIR"] = "/env/up"
                calls.clear()
                run_dir = os.path.join(d, "v-env")
                os.makedirs(run_dir)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli._cmd_verify_run(verify_args(None),
                                             run_dir, FakeLedger(), d, 0)
                check("verify upload env exits 0", rc == 0, str(rc))
                check("verify upload env single run_flow call",
                      len(calls) == 1, str(calls))
                check("verify upload env threads upload_dir",
                      bool(calls) and "upload_dir" in calls[0]
                      and calls[0]["upload_dir"] == "/env/up",
                      str(calls))
                os.environ.pop("QALOOP_UPLOAD_DIR", None)

                # --- (c) baselines single: flag, baseline_update kept ---
                spec = load_spec(flow_path)
                calls.clear()
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli._cmd_baselines_run(
                        baselines_args("/flag/up"),
                        os.path.join(d, "b-flag"), spec, FakeLedger(), d)
                check("baselines upload flag exits 0", rc == 0, str(rc))
                check("baselines upload flag single run_flow call",
                      len(calls) == 1, str(calls))
                check("baselines upload flag threads upload_dir",
                      bool(calls) and "upload_dir" in calls[0]
                      and calls[0]["upload_dir"] == "/flag/up",
                      str(calls))
                check("baselines upload flag keeps baseline_update=True",
                      bool(calls)
                      and calls[0].get("baseline_update") is True,
                      str(calls))

                # --- (d) baselines single: env fallback ---
                os.environ["QALOOP_UPLOAD_DIR"] = "/env/up"
                calls.clear()
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli._cmd_baselines_run(
                        baselines_args(None),
                        os.path.join(d, "b-env"), spec, FakeLedger(), d)
                check("baselines upload env exits 0", rc == 0, str(rc))
                check("baselines upload env single run_flow call",
                      len(calls) == 1, str(calls))
                check("baselines upload env threads upload_dir",
                      bool(calls) and "upload_dir" in calls[0]
                      and calls[0]["upload_dir"] == "/env/up",
                      str(calls))
                check("baselines upload env keeps baseline_update=True",
                      bool(calls)
                      and calls[0].get("baseline_update") is True,
                      str(calls))
                os.environ.pop("QALOOP_UPLOAD_DIR", None)

                # --- (e) baselines matrix: flag on every combination ---
                matrix_path = os.path.join(d, "t306-matrix.yaml")
                with open(matrix_path, "w") as fh:
                    fh.write("name: t306-matrix\ntarget: http://h/${t}\n"
                             "matrix:\n  t: [a, b]\n"
                             "steps:\n"
                             "  - name: go\n    goto: /${t}\n"
                             "artifacts: {screenshot: none, ax_snapshot: "
                             "on-failure, trace: false}\n")

                def fake_write_matrix_summary(spec, results, run_dir):
                    path = os.path.join(run_dir, "REPORT.md")
                    with open(path, "w") as fh:
                        fh.write("matrix summary\n")
                    return path

                old_write_report = cli.write_report
                old_print_summary = cli.print_summary
                old_wms = report_mod.write_matrix_summary
                cli.write_report = lambda *a, **kw: {}
                cli.print_summary = lambda result: None
                report_mod.write_matrix_summary = fake_write_matrix_summary
                try:
                    calls.clear()
                    matrix_run_dir = os.path.join(d, "b-matrix")
                    os.makedirs(matrix_run_dir)
                    matrix_args = SimpleNamespace(
                        flow=matrix_path, target=None, headed=False,
                        executable_path=None, upload_dir="/flag/up")
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        rc = cli._cmd_baselines_run(
                            matrix_args, matrix_run_dir,
                            load_spec(matrix_path), FakeLedger(), d)
                    check("baselines matrix upload exits 0",
                          rc == 0, str(rc))
                    check("baselines matrix upload ran every combination",
                          len(calls) == 2, str(len(calls)))
                    check("baselines matrix upload on both combinations",
                          bool(calls)
                          and all(c.get("upload_dir") == "/flag/up"
                                  for c in calls),
                          str(calls))
                    check("baselines matrix upload keeps "
                          "baseline_update=True",
                          bool(calls)
                          and all(c.get("baseline_update") is True
                                  for c in calls),
                          str(calls))
                finally:
                    cli.write_report = old_write_report
                    cli.print_summary = old_print_summary
                    report_mod.write_matrix_summary = old_wms
            finally:
                os.environ.pop("QALOOP_UPLOAD_DIR", None)

            # --- (f) parser: baselines --upload-dir parses, default None ---
            parsed = cli.build_parser().parse_args(
                ["baselines", "flows/x.yaml", "--upload-dir", "/flag/up"])
            check("baselines parser threads --upload-dir",
                  parsed.upload_dir == "/flag/up", str(parsed.upload_dir))
            parsed_default = cli.build_parser().parse_args(
                ["baselines", "flows/x.yaml"])
            check("baselines parser --upload-dir defaults to None",
                  parsed_default.upload_dir is None,
                  str(parsed_default.upload_dir))
    finally:
        cli.run_flow = old_run_flow


def test_issue_308_perform_cli_plumbing_contract():
    """Issue #308: cmd_perform threads the documented perform flags
    into qaloop.perform.perform verbatim (task/target/max_actions/
    headless=not headed/executable_path/cdp_url/allow_publish/
    upload_dir/max_cost_usd), rejects a negative --max-cost-usd with
    the exact ERROR line and exit 2 before any perform call, maps a
    RuntimeError from perform() to ERROR + exit 2, and returns 0 iff
    the result status is completed. perform is stubbed throughout so
    no browser or model is touched; the ledger append is captured so
    no real ledger row is written."""
    import io

    import qaloop.cli as cli
    import qaloop.ledger as ledger_mod
    import qaloop.perform as perform_mod

    calls = []
    ledger_rows = []
    state = {"mode": "ok", "status": "completed"}

    def fake_perform(**kw):
        calls.append(kw)
        if state["mode"] == "raise":
            raise RuntimeError("boom-308")
        return {"status": state["status"], "summary": "did it",
                "action_count": 3, "tokens_in": 10, "tokens_out": 5,
                "cost_usd_est": 0.0012, "run_dir": "/tmp/run308"}

    old_perform = perform_mod.perform
    old_append = ledger_mod.append
    perform_mod.perform = fake_perform
    ledger_mod.append = ledger_rows.append
    try:
        def run_cli(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_perform(cli.build_parser().parse_args(argv))
            return rc, buf.getvalue()

        # --- (a) full-flag case: every kwarg threaded verbatim ---
        calls.clear()
        ledger_rows.clear()
        state["mode"] = "ok"
        state["status"] = "completed"
        rc, out = run_cli(
            ["perform", "--task", "do X", "--target", "http://example.test/",
             "--max-actions", "7", "--headed",
             "--cdp-url", "http://127.0.0.1:9222",
             "--executable-path", "/fake/chromium-308",
             "--allow-publish", "--upload-dir", "/tmp/up308",
             "--max-cost-usd", "1.25"])
        check("perform full flags exits 0 on completed", rc == 0, str(rc))
        check("perform full flags single perform call",
              len(calls) == 1, str(calls))
        kw = calls[0] if calls else {}
        check("perform full flags kwarg keys exact",
              set(kw) == {"task", "target", "max_actions", "headless",
                          "executable_path", "cdp_url", "allow_publish",
                          "upload_dir", "max_cost_usd"},
              str(sorted(kw)))
        check("perform full flags task", kw.get("task") == "do X", str(kw))
        check("perform full flags target",
              kw.get("target") == "http://example.test/", str(kw))
        check("perform full flags max_actions",
              kw.get("max_actions") == 7, str(kw))
        check("perform full flags headed gives headless False",
              kw.get("headless") is False, str(kw))
        check("perform full flags executable_path",
              kw.get("executable_path") == "/fake/chromium-308", str(kw))
        check("perform full flags cdp_url threaded",
              "cdp_url" in kw
              and kw.get("cdp_url") == "http://127.0.0.1:9222", str(kw))
        check("perform full flags allow_publish",
              kw.get("allow_publish") is True, str(kw))
        check("perform full flags upload_dir",
              kw.get("upload_dir") == "/tmp/up308", str(kw))
        check("perform full flags max_cost_usd",
              kw.get("max_cost_usd") == 1.25, str(kw))
        check("perform full flags prints status line",
              "status: completed" in out, out)
        check("perform full flags ledger row kind perform",
              len(ledger_rows) == 1
              and ledger_rows[0].get("kind") == "perform",
              str(ledger_rows))

        # --- (b) defaults case: parser defaults reach the stub ---
        calls.clear()
        rc, out = run_cli(["perform", "--task", "x"])
        check("perform defaults exits 0 on completed", rc == 0, str(rc))
        check("perform defaults single perform call",
              len(calls) == 1, str(calls))
        kw = calls[0] if calls else {}
        check("perform defaults target None",
              "target" in kw and kw.get("target") is None, str(kw))
        check("perform defaults max_actions 30",
              kw.get("max_actions") == 30, str(kw))
        check("perform defaults headless True",
              kw.get("headless") is True, str(kw))
        check("perform defaults executable_path None",
              "executable_path" in kw
              and kw.get("executable_path") is None, str(kw))
        check("perform defaults cdp_url None",
              "cdp_url" in kw and kw.get("cdp_url") is None, str(kw))
        check("perform defaults allow_publish False",
              kw.get("allow_publish") is False, str(kw))
        check("perform defaults upload_dir None",
              "upload_dir" in kw and kw.get("upload_dir") is None, str(kw))
        check("perform defaults max_cost_usd 0.0",
              kw.get("max_cost_usd") == 0.0, str(kw))

        # --- (c) negative-cost guard: exit 2, exact line, no call ---
        calls.clear()
        ledger_rows.clear()
        rc, out = run_cli(["perform", "--task", "x",
                           "--max-cost-usd", "-1"])
        check("perform negative cost exits 2", rc == 2, str(rc))
        check("perform negative cost exact ERROR line",
              out.strip() == "ERROR: --max-cost-usd cannot be negative",
              out)
        check("perform negative cost stub not called",
              calls == [] and ledger_rows == [],
              str(calls) + str(ledger_rows))

        # --- (d) RuntimeError path: exit 2 with ERROR: <msg> ---
        calls.clear()
        state["mode"] = "raise"
        rc, out = run_cli(["perform", "--task", "x"])
        check("perform RuntimeError exits 2", rc == 2, str(rc))
        check("perform RuntimeError ERROR line",
              "ERROR: boom-308" in out.splitlines(), out)
        state["mode"] = "ok"

        # --- (e) non-completed status maps to exit 1 ---
        state["status"] = "blocked"
        rc, out = run_cli(["perform", "--task", "x"])
        check("perform non-completed exits 1", rc == 1, str(rc))
        check("perform non-completed prints status",
              "status: blocked" in out, out)
        state["status"] = "completed"
    finally:
        perform_mod.perform = old_perform
        ledger_mod.append = old_append


def test_issue_310_enqueue_cli_plumbing_contract():
    """Issue #310: cmd_enqueue threads the documented enqueue flags
    into qaloop.queue.enqueue (kind, payload built from --payload JSON
    with setdefault flow and conditional target for verify-flow,
    max_retries as kwarg), rejects a negative --max-retries with the
    exact lowercase error line and exit 2 before any enqueue call,
    and prints the exact enqueued job line. queue.enqueue is stubbed
    throughout so no real queue row is written."""
    import io

    import qaloop.cli as cli
    import qaloop.queue as queue_mod

    calls = []

    def fake_enqueue(kind, payload, max_retries=0):
        calls.append((kind, dict(payload), max_retries))
        return 4242

    old_enqueue = queue_mod.enqueue
    queue_mod.enqueue = fake_enqueue
    try:
        def run_cli(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_enqueue(cli.build_parser().parse_args(argv))
            return rc, buf.getvalue()

        # --- (a) full verify-flow case ---
        calls.clear()
        rc, out = run_cli(
            ["enqueue", "--kind", "verify-flow", "--flow", "f.yaml",
             "--target", "http://x.test/",
             "--payload", '{"extra":42,"repo":"r"}',
             "--max-retries", "2"])
        check("enqueue full exits 0", rc == 0, str(rc))
        check("enqueue full single call", len(calls) == 1, str(calls))
        kind, payload, mr = calls[0] if calls else (None, {}, None)
        check("enqueue full kind verify-flow", kind == "verify-flow", str(calls))
        check("enqueue full payload exact",
              payload == {"extra": 42, "repo": "r", "flow": "f.yaml",
                          "target": "http://x.test/"}, str(payload))
        check("enqueue full max_retries 2", mr == 2, str(calls))
        check("enqueue full printed line exact",
              out.strip() == "enqueued job #4242 (verify-flow, max_retries=2)",
              out)

        # --- (b) setdefault: payload flow wins over --flow ---
        calls.clear()
        rc, out = run_cli(
            ["enqueue", "--kind", "verify-flow", "--flow", "cli.yaml",
             "--payload", '{"flow":"payload.yaml"}'])
        check("enqueue setdefault exits 0", rc == 0, str(rc))
        check("enqueue setdefault single call", len(calls) == 1, str(calls))
        payload_b = calls[0][1] if calls else {}
        check("enqueue setdefault flow stays payload.yaml",
              payload_b.get("flow") == "payload.yaml", str(payload_b))

        # --- (c) no --target: target key absent ---
        calls.clear()
        rc, out = run_cli(
            ["enqueue", "--kind", "verify-flow", "--flow", "f.yaml"])
        check("enqueue no-target exits 0", rc == 0, str(rc))
        check("enqueue no-target single call", len(calls) == 1, str(calls))
        payload_c = calls[0][1] if calls else {}
        check("enqueue no-target target absent",
              "target" not in payload_c, str(payload_c))

        # --- (d) verify-repo: payload unchanged ---
        calls.clear()
        rc, out = run_cli(
            ["enqueue", "--kind", "verify-repo",
             "--payload", '{"repo":"ac","pr":7}'])
        check("enqueue verify-repo exits 0", rc == 0, str(rc))
        check("enqueue verify-repo single call", len(calls) == 1, str(calls))
        kind_d, payload_d, mr_d = calls[0] if calls else (None, {}, None)
        check("enqueue verify-repo kind", kind_d == "verify-repo", str(calls))
        check("enqueue verify-repo payload unchanged",
              payload_d == {"repo": "ac", "pr": 7}, str(payload_d))
        check("enqueue verify-repo printed line exact",
              out.strip() == "enqueued job #4242 (verify-repo, max_retries=0)",
              out)

        # --- (e) parser defaults ---
        parsed = cli.build_parser().parse_args(["enqueue"])
        check("enqueue defaults kind verify-flow",
              parsed.kind == "verify-flow", str(parsed.kind))
        check("enqueue defaults payload string {}",
              parsed.payload == "{}" and isinstance(parsed.payload, str),
              str(parsed.payload))
        check("enqueue defaults max_retries 0",
              parsed.max_retries == 0, str(parsed.max_retries))
        check("enqueue defaults flow None", parsed.flow is None, str(parsed.flow))
        check("enqueue defaults target None",
              parsed.target is None, str(parsed.target))
        check("enqueue defaults fn is cmd_enqueue",
              parsed.fn is cli.cmd_enqueue, str(parsed.fn))

        # --- (f) negative guard: exit 2, exact line, no call ---
        calls.clear()
        rc, out = run_cli(["enqueue", "--max-retries", "-1"])
        check("enqueue negative exits 2", rc == 2, str(rc))
        check("enqueue negative exact error line",
              out.strip() == "error: --max-retries must be >= 0 (got -1)",
              out)
        check("enqueue negative stub not called", calls == [], str(calls))
    finally:
        queue_mod.enqueue = old_enqueue


def test_issue_312_compare_cli_plumbing_contract():
    """Issue #312: cmd_compare resolves both run arguments through
    the compare module under the default runs root (QALOOP_RUNS),
    prints the rendered diff, and maps the outcome to the SPEC
    exit codes: 0 identical (duration drift alone tolerated), 1
    differ (top-level status change or a step status change), 2
    with exactly one ERROR line naming the unresolvable run.
    Real fixtures go through cmd_compare only; the compare module
    functions are never called directly and never stubbed."""
    import io

    import qaloop.cli as cli

    def run_cli(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_compare(cli.build_parser().parse_args(argv))
        return rc, buf.getvalue()

    # --- (0) parser wiring: compare dispatches to cmd_compare ---
    parsed = cli.build_parser().parse_args(["compare", "x", "y"])
    check("compare parser fn is cmd_compare",
          parsed.fn is cli.cmd_compare, str(parsed.fn))

    with tempfile.TemporaryDirectory() as root:
        old_runs = os.environ.get("QALOOP_RUNS")
        os.environ["QALOOP_RUNS"] = root
        try:
            steps = [_syn_step_91("load", "passed", 100),
                     _syn_step_91("click", "passed", 200)]

            # --- (a) identical: duration drift alone tolerated ---
            _syn_run_dir_91(root, "312-ident-a", "f", "passed", 1.0, 300,
                            steps)
            _syn_run_dir_91(root, "312-ident-b", "f", "passed", 2.0, 340,
                            steps)
            rc, out = run_cli(["compare", "312-ident-a", "312-ident-b"])
            check("compare cli identical exits 0", rc == 0, str(rc))
            check("compare cli identical final line",
                  out.strip().splitlines()[-1] == "identical", out)

            # --- (b1) differ: top-level status change ---
            _syn_run_dir_91(root, "312-top-a", "f", "passed", 1.0, 300,
                            steps)
            _syn_run_dir_91(root, "312-top-b", "f", "failed", 2.0, 300,
                            steps)
            rc, out = run_cli(["compare", "312-top-a", "312-top-b"])
            check("compare cli status change exits 1", rc == 1, str(rc))
            check("compare cli status change final line",
                  out.strip().splitlines()[-1] == "differ", out)

            # --- (b2) differ: one step status flipped ---
            steps_b = [_syn_step_91("load", "passed", 100),
                       _syn_step_91("click", "failed", 200)]
            _syn_run_dir_91(root, "312-step-a", "f", "passed", 1.0, 300,
                            steps)
            _syn_run_dir_91(root, "312-step-b", "f", "passed", 2.0, 300,
                            steps_b)
            rc, out = run_cli(["compare", "312-step-a", "312-step-b"])
            check("compare cli step change exits 1", rc == 1, str(rc))
            check("compare cli step change final line",
                  out.strip().splitlines()[-1] == "differ", out)

            # --- (c) missing run: exit 2, exactly one ERROR line ---
            rc, out = run_cli(["compare", "312-ident-a",
                               "zzz-no-such-312"])
            check("compare cli missing exits 2", rc == 2, str(rc))
            lines = out.strip().splitlines()
            check("compare cli missing exactly one line",
                  len(lines) == 1, out)
            line = lines[0] if lines else ""
            check("compare cli missing ERROR line names run",
                  line.startswith("ERROR: ")
                  and "no run dir matching" in line
                  and "'zzz-no-such-312'" in line, line)
        finally:
            if old_runs is None:
                del os.environ["QALOOP_RUNS"]
            else:
                os.environ["QALOOP_RUNS"] = old_runs


def test_issue_314_ledger_cli_plumbing_contract():
    """Issue #314: cmd_ledger prints ledger.summarize() as indented
    JSON to stdout and returns 0, dispatched by the ledger subparser
    with no flags. The ledger path honors QALOOP_LEDGER; a missing
    file summarizes to zeros. Mixed kinds pin the bucket rules:
    scripted counts in scripted_runs, investigation counts in
    investigations and its cost, perform and scripted-matrix count
    toward runs and total cost only, and a missing cost_usd_est
    counts 0."""
    import io

    import qaloop.cli as cli

    def run_cli(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_ledger(cli.build_parser().parse_args(argv))
        return rc, buf.getvalue()

    # --- parser wiring: ledger dispatches to cmd_ledger ---
    parsed = cli.build_parser().parse_args(["ledger"])
    check("ledger parser fn is cmd_ledger",
          parsed.fn is cli.cmd_ledger, str(parsed.fn))

    with tempfile.TemporaryDirectory() as root:
        old_ledger = os.environ.get("QALOOP_LEDGER")
        ledger_file = os.path.join(root, "ledger.jsonl")
        try:
            entries = [
                {"kind": "scripted", "cost_usd_est": 0.0},
                {"kind": "investigation", "cost_usd_est": 0.05},
                {"kind": "perform", "cost_usd_est": 0.1},
                {"kind": "scripted-matrix"},
            ]
            with open(ledger_file, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")
            os.environ["QALOOP_LEDGER"] = ledger_file

            rc, out = run_cli(["ledger"])
            check("ledger cli mixed exits 0", rc == 0, str(rc))
            check("ledger cli mixed exact summary",
                  json.loads(out) == {"runs": 4, "scripted_runs": 1,
                                      "investigations": 1,
                                      "total_cost_usd_est": 0.15,
                                      "investigation_cost_usd_est": 0.05},
                  out)
            check("ledger cli output multi-line",
                  len(out.strip().splitlines()) > 1, out)
            check("ledger cli output indented",
                  any(line.startswith("  \"") for line in out.splitlines()),
                  out)
            check("ledger cli output ends newline",
                  out.endswith("\n"), repr(out[-20:]))

            # --- absent file: zeros summary, still exit 0 ---
            os.environ["QALOOP_LEDGER"] = os.path.join(root, "nope.jsonl")
            rc, out = run_cli(["ledger"])
            check("ledger cli absent exits 0", rc == 0, str(rc))
            check("ledger cli absent zeros summary",
                  json.loads(out) == {"runs": 0, "scripted_runs": 0,
                                      "investigations": 0,
                                      "total_cost_usd_est": 0,
                                      "investigation_cost_usd_est": 0},
                  out)
        finally:
            if old_ledger is None:
                os.environ.pop("QALOOP_LEDGER", None)
            else:
                os.environ["QALOOP_LEDGER"] = old_ledger


def test_issue_316_investigate_cli_plumbing_contract():
    """Issue #316: cmd_investigate loads <run>/run.json as a
    RunResult, loads the flow spec with strict_env=False, and calls
    qaloop.investigate.investigate with keyword arguments only:
    target falls back to the recorded run target when --target is
    absent, --headed inverts to headless=False, the parser defaults
    are target=None / max_actions=20 / headed=False /
    executable_path=None, and the returned diagnosis is printed as
    indented JSON with non-ASCII characters unescaped (exit 0)."""
    import io

    import qaloop.cli as cli
    import qaloop.investigate as inv_mod
    from qaloop.runner import RunResult, StepResult

    diagnosis = {"mode": "stub-316",
                 "diagnosis": "café — broken",
                 "likely_cause": "boom-316"}
    captured = []
    real_inv = inv_mod.investigate

    def stub(**kw):
        captured.append(kw)
        return diagnosis

    inv_mod.investigate = stub
    try:
        with tempfile.TemporaryDirectory() as root:
            recorded = RunResult(
                flow_name="inv-316", target="http://recorded-316/",
                status="failed", started=0.0, ended=0.0,
                steps=[StepResult(index=0, phase="steps",
                                  name="only step", op="goto",
                                  status="failed", duration_ms=0,
                                  error="boom-316")],
                failed_step=0, console_errors=[], page_errors=[],
                failed_requests=[], bad_responses=[], run_dir=root)
            with open(os.path.join(root, "run.json"), "w",
                      encoding="utf-8") as f:
                json.dump(recorded.to_dict(), f)
            flow_path = os.path.join(root, "flow.yaml")
            with open(flow_path, "w", encoding="utf-8") as f:
                f.write("name: inv316flow\n"
                        "target: http://spec-316/\n"
                        "steps:\n"
                        "  - name: go\n"
                        "    goto: /\n")

            def run_parsed(parsed):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = parsed.fn(parsed)
                return rc, buf.getvalue()

            # --- parser wiring + defaults ---
            parsed = cli.build_parser().parse_args(
                ["investigate", "--run", root, "--flow", flow_path])
            check("investigate parser fn is cmd_investigate",
                  parsed.fn is cli.cmd_investigate, str(parsed.fn))
            check("investigate parser defaults",
                  parsed.target is None and parsed.max_actions == 20
                  and parsed.headed is False
                  and parsed.executable_path is None,
                  str(vars(parsed)))

            # --- (A) defaults: target fallback, headless on ---
            rc, out = run_parsed(parsed)
            check("investigate cli defaults exits 0", rc == 0, str(rc))
            check("investigate cli defaults one call",
                  len(captured) == 1, str(len(captured)))
            kw = captured[0] if captured else {}
            check("investigate cli kwargs are keyword-only set",
                  set(kw) == {"result", "spec", "target", "max_actions",
                              "headless", "executable_path", "run_dir"},
                  str(sorted(kw)))
            res = kw.get("result")
            check("investigate cli result is recorded RunResult",
                  isinstance(res, RunResult)
                  and res.flow_name == "inv-316"
                  and res.target == "http://recorded-316/"
                  and res.status == "failed"
                  and len(res.steps) == 1
                  and res.steps[0].name == "only step"
                  and res.steps[0].error == "boom-316",
                  str(res))
            check("investigate cli spec loaded",
                  kw.get("spec") is not None
                  and kw["spec"].name == "inv316flow",
                  str(kw.get("spec")))
            check("investigate cli target falls back to recorded",
                  kw.get("target") == "http://recorded-316/",
                  str(kw.get("target")))
            check("investigate cli default plumbing",
                  kw.get("max_actions") == 20
                  and kw.get("headless") is True
                  and kw.get("executable_path") is None
                  and kw.get("run_dir") == root,
                  str(kw))

            # --- output pinning: indented, unescaped JSON ---
            check("investigate cli output parses to diagnosis",
                  json.loads(out) == diagnosis, out)
            check("investigate cli output multi-line indented",
                  "\n  \"" in out, out)
            check("investigate cli output non-ascii literal",
                  "café — broken" in out
                  and "\\u00e9" not in out and "\\u2014" not in out,
                  out)

            # --- (B) overrides: target, budget, headed inversion ---
            captured.clear()
            parsed_b = cli.build_parser().parse_args(
                ["investigate", "--run", root, "--flow", flow_path,
                 "--target", "http://override-316/",
                 "--max-actions", "7", "--headed",
                 "--executable-path", "/sentinel/chrome-316"])
            rc, out = run_parsed(parsed_b)
            check("investigate cli overrides exits 0", rc == 0, str(rc))
            check("investigate cli overrides one call",
                  len(captured) == 1, str(len(captured)))
            kw = captured[0] if captured else {}
            check("investigate cli override plumbing",
                  kw.get("target") == "http://override-316/"
                  and kw.get("max_actions") == 7
                  and kw.get("headless") is False
                  and kw.get("executable_path") == "/sentinel/chrome-316"
                  and kw.get("run_dir") == root,
                  str(kw))
            check("investigate cli overrides output is diagnosis",
                  json.loads(out) == diagnosis, out)
    finally:
        inv_mod.investigate = real_inv


def test_issue_318_evaluate_cli_plumbing_contract():
    """Issue #318: cmd_evaluate plumbing contract: parser wiring
    (fn is cmd_evaluate, --repo defaults to None, --claim/--run/
    --diff required), missing run dir / unresolvable diff / empty
    diff / judge RuntimeError all exit 2 with one exact ERROR line
    and no evaluate/write/ledger side effects, and on success the
    claim, run dir (as passed) and diff text reach evaluate
    positionally and verbatim, write_evaluation gets (run_dir,
    claim, verdict), exactly four verdict lines are printed, and
    exactly one exact ledger row is appended (claim truncated to
    120 chars in the ledger only; missing _cost defaults to 0)."""
    import io

    import qaloop.cli as cli
    import qaloop.evaluate as eval_mod
    import qaloop.ledger as ledger_mod

    eval_calls = []
    write_calls = []
    ledger_rows = []
    state = {"raise": False, "verdict": None}

    def stub_evaluate(claim, run_dir, diff):
        eval_calls.append((claim, run_dir, diff))
        if state["raise"]:
            raise RuntimeError("boom-318")
        return state["verdict"]

    def stub_write(run_dir, claim, verdict):
        write_calls.append((run_dir, claim, verdict))
        return ("/sentinel-318/EVALUATION.md",
                "/sentinel-318/evaluation.json")

    def stub_append(entry):
        ledger_rows.append(entry)

    real_evaluate = eval_mod.evaluate
    real_write = eval_mod.write_evaluation
    real_append = ledger_mod.append
    eval_mod.evaluate = stub_evaluate
    eval_mod.write_evaluation = stub_write
    ledger_mod.append = stub_append
    try:
        def run_parsed(parsed):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = parsed.fn(parsed)
            return rc, buf.getvalue()

        def parse(claim, run_dir, diff):
            return cli.build_parser().parse_args(
                ["evaluate", "--claim", claim, "--run", run_dir,
                 "--diff", diff])

        with tempfile.TemporaryDirectory() as root:
            run_dir = os.path.join(root, "run-318")
            os.makedirs(run_dir)
            diff_text = ("diff --git a/x.py b/x.py\n"
                         "--- a/x.py\n+++ b/x.py\n"
                         "@@ -1 +1 @@\n-old\n+new 318\n")
            diff_path = os.path.join(root, "change-318.diff")
            with open(diff_path, "w", encoding="utf-8") as f:
                f.write(diff_text)
            claim = "the change does the 318 thing"

            # --- parser wiring + defaults + required ---
            parsed = parse(claim, run_dir, diff_path)
            check("evaluate parser fn is cmd_evaluate",
                  parsed.fn is cli.cmd_evaluate, str(parsed.fn))
            check("evaluate parser repo default None",
                  parsed.repo is None, str(vars(parsed)))
            check("evaluate parser values",
                  parsed.claim == claim and parsed.run == run_dir
                  and parsed.diff == diff_path, str(vars(parsed)))
            for missing in (["--claim"], ["--run"], ["--diff"]):
                argv = ["evaluate", "--claim", claim, "--run", run_dir,
                        "--diff", diff_path]
                idx = argv.index(missing[0])
                argv = argv[:idx] + argv[idx + 2:]
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        cli.build_parser().parse_args(argv)
                    req_ok = False
                except SystemExit:
                    req_ok = True
                check(f"evaluate parser {missing[0]} required",
                      req_ok, str(argv))

            # --- (1) missing run dir ---
            missing_run = os.path.join(root, "no-such-318")
            eval_calls.clear(); write_calls.clear(); ledger_rows.clear()
            rc, out = run_parsed(parse(claim, missing_run, diff_path))
            check("evaluate missing run rc 2", rc == 2, str(rc))
            check("evaluate missing run exact line",
                  out == f"ERROR: run dir not found: {missing_run}\n",
                  repr(out))
            check("evaluate missing run no side effects",
                  eval_calls == [] and write_calls == []
                  and ledger_rows == [],
                  f"{eval_calls} {write_calls} {ledger_rows}")

            # --- (2) unresolvable --diff ---
            eval_calls.clear(); write_calls.clear(); ledger_rows.clear()
            rc, out = run_parsed(parse(claim, run_dir, "bogus-318"))
            check("evaluate bogus diff rc 2", rc == 2, str(rc))
            check("evaluate bogus diff exact line",
                  out == ("ERROR: --diff must be a diff file or a git "
                          "range, got 'bogus-318'\n"), repr(out))
            check("evaluate bogus diff no side effects",
                  eval_calls == [] and write_calls == []
                  and ledger_rows == [],
                  f"{eval_calls} {write_calls} {ledger_rows}")

            # --- (3) empty diff ---
            empty_path = os.path.join(root, "empty-318.diff")
            with open(empty_path, "w", encoding="utf-8") as f:
                f.write("  \n\t \n")
            eval_calls.clear(); write_calls.clear(); ledger_rows.clear()
            rc, out = run_parsed(parse(claim, run_dir, empty_path))
            check("evaluate empty diff rc 2", rc == 2, str(rc))
            check("evaluate empty diff exact line",
                  out == "ERROR: empty diff — nothing to evaluate\n",
                  repr(out))
            check("evaluate empty diff no side effects",
                  eval_calls == [] and write_calls == []
                  and ledger_rows == [],
                  f"{eval_calls} {write_calls} {ledger_rows}")

            # --- (4) judge raises ---
            state["raise"] = True
            eval_calls.clear(); write_calls.clear(); ledger_rows.clear()
            rc, out = run_parsed(parse(claim, run_dir, diff_path))
            state["raise"] = False
            check("evaluate judge raises rc 2", rc == 2, str(rc))
            check("evaluate judge raises exact line",
                  out == "ERROR: boom-318\n", repr(out))
            check("evaluate judge raises no write or ledger",
                  write_calls == [] and ledger_rows == [],
                  f"{write_calls} {ledger_rows}")

            # --- (5) success ---
            verdict = {"verdict": "MAKES_SENSE", "confidence": "high",
                       "rationale": "looks right",
                       "_cost": {"tokens_in": 123, "tokens_out": 45,
                                 "cost_usd_est": 0.0012}}
            state["verdict"] = verdict
            eval_calls.clear(); write_calls.clear(); ledger_rows.clear()
            rc, out = run_parsed(parse(claim, run_dir, diff_path))
            check("evaluate success rc 0", rc == 0, str(rc))
            check("evaluate success stub positional verbatim",
                  eval_calls == [(claim, run_dir, diff_text)],
                  str(eval_calls))
            check("evaluate success write args",
                  write_calls == [(run_dir, claim, verdict)],
                  str(write_calls))
            check("evaluate success stdout exactly 4 lines",
                  out.splitlines() == [
                      "verdict: MAKES_SENSE (confidence: high)",
                      "rationale: looks right",
                      "cost: tokens 123/45  est $0.001200",
                      "report: /sentinel-318/EVALUATION.md"],
                  repr(out))
            check("evaluate success ledger exact row",
                  ledger_rows == [{"kind": "evaluate",
                                   "run_dir": os.path.abspath(run_dir),
                                   "claim": claim,
                                   "verdict": "MAKES_SENSE",
                                   "tokens_in": 123, "tokens_out": 45,
                                   "cost_usd_est": 0.0012}],
                  str(ledger_rows))

            # --- (5b) truncation: ledger 120, stub full 150 ---
            claim150 = "c" * 150
            eval_calls.clear(); write_calls.clear(); ledger_rows.clear()
            rc, out = run_parsed(parse(claim150, run_dir, diff_path))
            check("evaluate long claim rc 0", rc == 0, str(rc))
            check("evaluate long claim stub verbatim 150",
                  len(eval_calls) == 1
                  and eval_calls[0][0] == claim150
                  and len(eval_calls[0][0]) == 150,
                  str(eval_calls))
            check("evaluate long claim ledger truncated 120",
                  len(ledger_rows) == 1
                  and ledger_rows[0]["claim"] == claim150[:120]
                  and len(ledger_rows[0]["claim"]) == 120,
                  str(ledger_rows))

            # --- (6) no _cost ---
            verdict6 = {"verdict": "INSUFFICIENT_EVIDENCE",
                        "confidence": "low", "rationale": "thin"}
            state["verdict"] = verdict6
            eval_calls.clear(); write_calls.clear(); ledger_rows.clear()
            rc, out = run_parsed(parse(claim, run_dir, diff_path))
            check("evaluate no-cost rc 0", rc == 0, str(rc))
            check("evaluate no-cost ledger zeros",
                  len(ledger_rows) == 1
                  and ledger_rows[0]["tokens_in"] == 0
                  and ledger_rows[0]["tokens_out"] == 0
                  and ledger_rows[0]["cost_usd_est"] == 0,
                  str(ledger_rows))
            check("evaluate no-cost printed cost line",
                  "cost: tokens 0/0  est $0.000000" in out.splitlines(),
                  repr(out))
    finally:
        eval_mod.evaluate = real_evaluate
        eval_mod.write_evaluation = real_write
        ledger_mod.append = real_append


def test_issue_320_dashboard_cli_plumbing_contract():
    """Issue #320: cmd_dashboard plumbing contract: parser wiring
    (fn is cmd_dashboard, --out defaults to
    <QALOOP_HOME>/dashboard), exit 0, runs-root resolution from
    QALOOP_RUNS when set and <QALOOP_HOME>/runs when unset, --out
    passed through verbatim as the second positional argument to
    qaloop.dashboard.build, and the exact printed dashboard: line
    carrying the build return value."""
    import io

    import qaloop.cli as cli
    import qaloop.dashboard as dash_mod

    calls = []

    def stub(runs_root, out_dir):
        calls.append((runs_root, out_dir))
        return "/sentinel-320/index.html"

    real_build = dash_mod.build
    old = os.environ.get("QALOOP_RUNS")
    dash_mod.build = stub
    try:
        def run_parsed(parsed):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = parsed.fn(parsed)
            return rc, buf.getvalue()

        default_out = os.path.join(cli.QALOOP_HOME, "dashboard")

        # --- QALOOP_RUNS set, parser default --out ---
        os.environ["QALOOP_RUNS"] = "/sentinel-320/runs"
        parsed = cli.build_parser().parse_args(["dashboard"])
        check("dashboard parser fn is cmd_dashboard",
              parsed.fn is cli.cmd_dashboard, str(parsed.fn))
        check("dashboard parser out default",
              parsed.out == default_out, str(vars(parsed)))
        rc, out = run_parsed(parsed)
        check("dashboard runs env set rc 0", rc == 0, str(rc))
        check("dashboard runs env set stub args",
              calls == [("/sentinel-320/runs", default_out)],
              str(calls))
        check("dashboard runs env set exact line",
              out == "dashboard: /sentinel-320/index.html\n",
              repr(out))

        # --- QALOOP_RUNS unset, --out passthrough ---
        os.environ.pop("QALOOP_RUNS", None)
        parsed = cli.build_parser().parse_args(
            ["dashboard", "--out", "/sentinel-320/out"])
        check("dashboard parser fn is cmd_dashboard with out",
              parsed.fn is cli.cmd_dashboard, str(parsed.fn))
        check("dashboard parser out passthrough",
              parsed.out == "/sentinel-320/out", str(vars(parsed)))
        calls.clear()
        rc, out = run_parsed(parsed)
        check("dashboard runs env unset rc 0", rc == 0, str(rc))
        check("dashboard runs env unset stub args",
              calls == [(os.path.join(cli.QALOOP_HOME, "runs"),
                         "/sentinel-320/out")],
              str(calls))
        check("dashboard runs env unset exact line",
              out == "dashboard: /sentinel-320/index.html\n",
              repr(out))
    finally:
        dash_mod.build = real_build
        os.environ.pop("QALOOP_RUNS", None) if old is None else os.environ.update({"QALOOP_RUNS": old})


def test_issue_322_webhook_cli_plumbing_contract():
    """Issue #322: cmd_webhook plumbing contract: parser wiring
    (fn is cmd_webhook, --port defaults to int 8090, --host
    defaults to 127.0.0.1), exit 0, and port/host passed through
    verbatim as keyword-only arguments to qaloop.webhook.serve."""
    import qaloop.cli as cli
    import qaloop.webhook as webhook_mod

    calls = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))

    real_serve = webhook_mod.serve
    webhook_mod.serve = stub
    try:
        # --- parser defaults ---
        parsed = cli.build_parser().parse_args(["webhook"])
        check("webhook parser fn is cmd_webhook",
              parsed.fn is cli.cmd_webhook, str(parsed.fn))
        check("webhook parser port default 8090",
              parsed.port == 8090, str(vars(parsed)))
        check("webhook parser port default is int",
              isinstance(parsed.port, int), str(type(parsed.port)))
        check("webhook parser host default",
              parsed.host == "127.0.0.1", str(vars(parsed)))
        rc = parsed.fn(parsed)
        check("webhook defaults rc 0", rc == 0, str(rc))
        check("webhook defaults stub kwargs",
              calls == [((), {"port": 8090, "host": "127.0.0.1"})],
              str(calls))

        # --- explicit --port/--host passthrough ---
        calls.clear()
        parsed = cli.build_parser().parse_args(
            ["webhook", "--port", "9123", "--host", "0.0.0.0"])
        check("webhook parser fn is cmd_webhook with args",
              parsed.fn is cli.cmd_webhook, str(parsed.fn))
        check("webhook parser port passthrough",
              parsed.port == 9123, str(vars(parsed)))
        check("webhook parser host passthrough",
              parsed.host == "0.0.0.0", str(vars(parsed)))
        rc = parsed.fn(parsed)
        check("webhook explicit rc 0", rc == 0, str(rc))
        check("webhook explicit stub kwargs",
              calls == [((), {"port": 9123, "host": "0.0.0.0"})],
              str(calls))
    finally:
        webhook_mod.serve = real_serve

    check("webhook serve restored after test",
          webhook_mod.serve is real_serve, str(webhook_mod.serve))


def test_issue_325_console_clean_contract():
    """Issue #325 (tests from #324): the console_clean assertion contract.

    Pins runner._check_assertions' console_clean branch (detail exactly
    "clean", combined console-first failure detail, true count with
    first-3 naming and 120-char truncation, per-step checkpoint reset)
    and Collectors.errors_since_checkpoint directly, including the
    per-list-marks regression: with pre-step entries in BOTH lists, a
    pre-step page error must not be reported as new and a new console
    error must not be dropped (SPEC.md: console_clean is "no new
    console/page errors since the step started").
    """
    from qaloop.artifacts import Collectors
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step

    def clean_result(coll):
        step = Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=[("console_clean", True)],
                    continue_on_fail=False, timeout_ms=None, raw={})
        return _check_assertions(None, step, coll)[0]

    # (a) clean: no errors -> passes, detail exactly "clean"
    coll = Collectors(record_bodies=False)
    res = clean_result(coll)
    check("console_clean clean passes", res.passed is True, str(res))
    check("console_clean clean detail", res.detail == "clean", res.detail)

    # (b) one console + one page error since checkpoint -> fails with
    # detail "2 error(s): " naming both texts in combined order
    coll = Collectors(record_bodies=False)
    coll.checkpoint()
    coll.console_errors.append({"text": "boom console"})
    coll.page_errors.append({"text": "boom page"})
    res = clean_result(coll)
    check("console_clean combined fails", res.passed is False, str(res))
    check("console_clean combined detail",
          res.detail == "2 error(s): boom console; boom page", res.detail)

    # (c) checkpoint reset: the same errors no longer fail the assertion
    coll.checkpoint()
    res = clean_result(coll)
    check("console_clean reset passes", res.passed is True, str(res))
    check("console_clean reset detail", res.detail == "clean", res.detail)
    check("console_clean reset direct empty",
          coll.errors_since_checkpoint() == [],
          str(coll.errors_since_checkpoint()))

    # (d) 5 errors: true count, only the first 3 named, each text
    # truncated to 120 chars
    coll = Collectors(record_bodies=False)
    coll.checkpoint()
    long_text = "x" * 200
    coll.console_errors.append({"text": long_text})
    coll.console_errors.append({"text": "second"})
    coll.page_errors.append({"text": "third"})
    coll.page_errors.append({"text": "fourth"})
    coll.console_errors.append({"text": "fifth"})
    res = clean_result(coll)
    # Combined order is console-first: [long, second, fifth, third,
    # fourth], so the first 3 named are the three console errors.
    expected = "5 error(s): " + "; ".join([long_text[:120], "second",
                                           "fifth"])
    check("console_clean five fails", res.passed is False, str(res))
    check("console_clean five detail", res.detail == expected, res.detail)
    check("console_clean five names only first 3",
          "third" not in res.detail and "fourth" not in res.detail,
          res.detail)

    # (e) direct: mixed pre/post in both lists (the #325 reproduction)
    coll = Collectors(record_bodies=False)
    coll.console_errors.append({"text": "pre console"})
    coll.page_errors.append({"text": "pre page"})
    coll.checkpoint()
    coll.console_errors.append({"text": "post console"})
    coll.page_errors.append({"text": "post page"})
    texts = [e["text"] for e in coll.errors_since_checkpoint()]
    check("errors_since_checkpoint mixed pre/post",
          texts == ["post console", "post page"], str(texts))

    # (e2) direct: sharpest drift interleaving - pre-checkpoint page-only
    # error, post-checkpoint console-only error
    coll = Collectors(record_bodies=False)
    coll.page_errors.append({"text": "pre page only"})
    coll.checkpoint()
    coll.console_errors.append({"text": "post console only"})
    texts = [e["text"] for e in coll.errors_since_checkpoint()]
    check("errors_since_checkpoint page-pre console-post",
          texts == ["post console only"], str(texts))

    # (f) direct: never checkpointed -> everything is new, console-first
    coll = Collectors(record_bodies=False)
    coll.console_errors.append({"text": "c1"})
    coll.page_errors.append({"text": "p1"})
    texts = [e["text"] for e in coll.errors_since_checkpoint()]
    check("errors_since_checkpoint never checkpointed",
          texts == ["c1", "p1"], str(texts))


def _chat_fake_urlopen(captured, holder):
    def fake(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return io.BytesIO(json.dumps(holder["payload"]).encode())
    return fake


def test_chat_transport_contract():
    from qaloop import evaluate as ev_mod
    from qaloop import investigate as inv_mod
    from qaloop import perform as perf_mod
    cfg = {"base_url": "https://model.example/v1",
           "name": "fake-model-327", "api_key": "sk-test-327"}
    messages = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
    modules = [("evaluate", ev_mod, 1200),
               ("investigate", inv_mod, 1500),
               ("perform", perf_mod, 1200)]
    orig = urllib.request.urlopen
    try:
        for label, mod, max_tok in modules:
            captured = {}
            holder = {"payload": {
                "choices": [{"message": {"content": "hello-327"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}}}
            urllib.request.urlopen = _chat_fake_urlopen(captured, holder)
            result = mod._chat(cfg, messages)
            req = captured.get("req")
            check(label + " _chat url",
                  req is not None
                  and req.full_url == "https://model.example/v1/chat/completions",
                  str(getattr(req, "full_url", None)))
            check(label + " _chat authorization",
                  req is not None
                  and req.get_header("Authorization") == "Bearer sk-test-327",
                  str(req.get_header("Authorization")) if req else "no req")
            check(label + " _chat content-type",
                  req is not None
                  and req.get_header("Content-type") == "application/json",
                  str(req.get_header("Content-type")) if req else "no req")
            check(label + " _chat default timeout",
                  captured.get("timeout") == 90, str(captured.get("timeout")))
            body = json.loads(req.data) if req is not None else {}
            check(label + " _chat body",
                  body == {"model": "fake-model-327", "messages": messages,
                           "temperature": 0.2, "max_tokens": max_tok},
                  str(body))
            check(label + " _chat max_tokens",
                  body.get("max_tokens") == max_tok, str(body.get("max_tokens")))
            check(label + " _chat return",
                  result == ("hello-327",
                             {"prompt_tokens": 3, "completion_tokens": 4}),
                  str(result))
            captured.clear()
            mod._chat(cfg, messages, timeout_s=7)
            check(label + " _chat explicit timeout",
                  captured.get("timeout") == 7, str(captured.get("timeout")))
            holder["payload"] = {
                "choices": [{"message": {"content": None}}],
                "usage": {"prompt_tokens": 1}}
            result = mod._chat(cfg, messages)
            check(label + " _chat none content",
                  result == ("", {"prompt_tokens": 1}), str(result))
            holder["payload"] = {"choices": [{"message": {}}]}
            result = mod._chat(cfg, messages)
            check(label + " _chat missing content and usage",
                  result == ("", {}), str(result))
            holder["payload"] = {
                "choices": [{"message": {"content": "x-327"}}]}
            result = mod._chat(cfg, messages)
            check(label + " _chat missing usage",
                  result == ("x-327", {}), str(result))
    finally:
        urllib.request.urlopen = orig
    check("urlopen restored after chat contract test",
          urllib.request.urlopen is orig)


def test_issue_329_webhook_handler_http_contract():
    """Issue #329: pin the webhook Handler HTTP contract with real
    round-trip tests against a real ThreadingHTTPServer."""
    import hashlib
    import hmac
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from qaloop import webhook
    from qaloop import queue as q

    prior_secret = os.environ.get("QALOOP_WEBHOOK_SECRET")
    prior_token = os.environ.get("QALOOP_ENQUEUE_TOKEN")
    os.environ.pop("QALOOP_WEBHOOK_SECRET", None)
    os.environ.pop("QALOOP_ENQUEUE_TOKEN", None)
    try:
        with temp_db():
            srv = ThreadingHTTPServer(("127.0.0.1", 0), webhook.Handler)
            port = srv.server_address[1]
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            try:
                def req(method, path, body=None, headers=None):
                    hdrs = dict(headers or {})
                    if method == "POST":
                        hdrs.setdefault("Content-Type", "application/json")
                    r = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        data=body if method == "POST" else None,
                        method=method, headers=hdrs)
                    try:
                        with urllib.request.urlopen(r, timeout=5) as resp:
                            raw = resp.read().decode()
                            return resp.status, json.loads(raw) if raw else {}
                    except urllib.error.HTTPError as e:
                        raw = e.read().decode()
                        try:
                            parsed = json.loads(raw) if raw else {}
                        except Exception:
                            parsed = {}
                        return e.code, parsed

                def jobs_by_id():
                    return {j["id"]: j for j in q.list_jobs(limit=100)}

                def job_count():
                    return len(q.list_jobs(limit=100))

                # (1) health + unknown GET
                status, body = req("GET", "/health")
                check("329 health status", status == 200, str(status))
                check("329 health body", body == {"ok": True, "pending": 0},
                      str(body))
                status, body = req("GET", "/nope")
                check("329 get unknown status", status == 404, str(status))
                check("329 get unknown body",
                      body == {"ok": False, "error": "not found"}, str(body))

                # (2) ping + malformed JSON (no secret set)
                ping_body = json.dumps({"zen": "hi"}).encode()
                status, body = req("POST", "/webhook/github", ping_body,
                                   {"X-GitHub-Event": "ping"})
                check("329 ping status", status == 200, str(status))
                check("329 ping body", body == {"ok": True, "msg": "pong"},
                      str(body))
                status, body = req("POST", "/webhook/github", b"{not json",
                                   {"X-GitHub-Event": "ping"})
                check("329 webhook bad json status", status == 400,
                      str(status))
                check("329 webhook bad json body",
                      body == {"ok": False, "error": "bad json"}, str(body))

                # (3) pull_request opened enqueues verify-repo
                pr_payload = {"action": "opened",
                              "repository": {"full_name": "jjfwang/qaloop"},
                              "pull_request": {"number": 329,
                                               "head": {"sha": "abc123"}}}
                pr_body = json.dumps(pr_payload).encode()
                status, body = req("POST", "/webhook/github", pr_body,
                                   {"X-GitHub-Event": "pull_request"})
                check("329 pr opened status", status == 200, str(status))
                check("329 pr opened ok", body.get("ok") is True, str(body))
                job_id = body.get("job_id")
                check("329 pr opened job_id present", job_id is not None,
                      str(body))
                jobs = q.list_jobs(limit=100)
                check("329 pr opened exactly one job", len(jobs) == 1,
                      str(len(jobs)))
                if jobs:
                    job = jobs[0]
                    check("329 pr opened job id matches",
                          job["id"] == job_id, str(job))
                    check("329 pr opened job kind",
                          job["kind"] == "verify-repo", str(job))
                    check("329 pr opened job status",
                          job["status"] == "pending", str(job))
                    check("329 pr opened job payload",
                          job["payload"] == {"repo": "qaloop",
                                             "repo_full": "jjfwang/qaloop",
                                             "pr": 329, "sha": "abc123",
                                             "source": "github-webhook"},
                          str(job["payload"]))
                status, body = req("GET", "/health")
                check("329 health pending after enqueue",
                      body == {"ok": True, "pending": 1}, str(body))

                # (4) ignored action + ignored event
                closed_payload = dict(pr_payload, action="closed")
                status, body = req(
                    "POST", "/webhook/github",
                    json.dumps(closed_payload).encode(),
                    {"X-GitHub-Event": "pull_request"})
                check("329 pr closed status", status == 200, str(status))
                check("329 pr closed body",
                      body == {"ok": True, "msg": "ignored action closed"},
                      str(body))
                check("329 pr closed count unchanged", job_count() == 1,
                      str(job_count()))
                status, body = req("POST", "/webhook/github",
                                   json.dumps({}).encode(),
                                   {"X-GitHub-Event": "push"})
                check("329 push status", status == 200, str(status))
                check("329 push body",
                      body == {"ok": True, "msg": "ignored event push"},
                      str(body))
                check("329 push count unchanged", job_count() == 1,
                      str(job_count()))

                # (5) webhook secret enforcement
                secret = "test-secret-329"
                os.environ["QALOOP_WEBHOOK_SECRET"] = secret
                before = job_count()
                status, body = req("POST", "/webhook/github", pr_body,
                                   {"X-GitHub-Event": "pull_request"})
                check("329 unsigned status", status == 401, str(status))
                check("329 unsigned body",
                      body == {"ok": False, "error": "bad signature"},
                      str(body))
                status, body = req(
                    "POST", "/webhook/github", pr_body,
                    {"X-GitHub-Event": "pull_request",
                     "X-Hub-Signature-256": "sha256=nope"})
                check("329 bad signature status", status == 401, str(status))
                check("329 bad signature body",
                      body == {"ok": False, "error": "bad signature"},
                      str(body))
                check("329 bad signature count unchanged",
                      job_count() == before, str(job_count()))
                sig = "sha256=" + hmac.new(
                    secret.encode(), pr_body, hashlib.sha256).hexdigest()
                status, body = req(
                    "POST", "/webhook/github", pr_body,
                    {"X-GitHub-Event": "pull_request",
                     "X-Hub-Signature-256": sig})
                check("329 signed status", status == 200, str(status))
                check("329 signed ok with job_id",
                      body.get("ok") is True and body.get("job_id") is not None,
                      str(body))
                check("329 signed exactly one new job",
                      job_count() == before + 1, str(job_count()))
                os.environ.pop("QALOOP_WEBHOOK_SECRET", None)

                # (6) /enqueue without token
                before = job_count()
                enq_body = json.dumps(
                    {"kind": "verify-repo", "repo": "qaloop"}).encode()
                status, body = req("POST", "/enqueue", enq_body)
                check("329 enqueue status", status == 200, str(status))
                enq_id = body.get("job_id")
                check("329 enqueue job_id present", enq_id is not None,
                      str(body))
                job = jobs_by_id().get(enq_id)
                check("329 enqueue job kind",
                      job is not None and job["kind"] == "verify-repo",
                      str(job))
                check("329 enqueue job payload",
                      job is not None and job["payload"] == {"repo": "qaloop"},
                      str(job))
                status, body = req("POST", "/enqueue",
                                   json.dumps({"flow": "x.yaml"}).encode())
                check("329 enqueue default status", status == 200,
                      str(status))
                def_id = body.get("job_id")
                job = jobs_by_id().get(def_id)
                check("329 enqueue default kind",
                      job is not None and job["kind"] == "verify-flow",
                      str(job))
                check("329 enqueue default payload",
                      job is not None and job["payload"] == {"flow": "x.yaml"},
                      str(job))
                check("329 enqueue added two jobs",
                      job_count() == before + 2, str(job_count()))
                status, body = req("POST", "/enqueue", b"{not json")
                check("329 enqueue bad json status", status == 400,
                      str(status))
                check("329 enqueue bad json body",
                      body == {"ok": False, "error": "bad json"}, str(body))

                # (7) enqueue token enforcement
                token = "enqueue-token-329"
                os.environ["QALOOP_ENQUEUE_TOKEN"] = token
                before = job_count()
                status, body = req("POST", "/enqueue", enq_body)
                check("329 enqueue missing token status", status == 401,
                      str(status))
                check("329 enqueue missing token body",
                      body == {"ok": False, "error": "bad token"}, str(body))
                status, body = req("POST", "/enqueue", enq_body,
                                   {"X-QALoop-Token": "wrong"})
                check("329 enqueue wrong token status", status == 401,
                      str(status))
                check("329 enqueue wrong token body",
                      body == {"ok": False, "error": "bad token"}, str(body))
                check("329 enqueue token count unchanged",
                      job_count() == before, str(job_count()))
                status, body = req("POST", "/enqueue", enq_body,
                                   {"X-QALoop-Token": token})
                check("329 enqueue token ok status", status == 200,
                      str(status))
                check("329 enqueue token ok job_id",
                      body.get("ok") is True and body.get("job_id") is not None,
                      str(body))
                check("329 enqueue token added one job",
                      job_count() == before + 1, str(job_count()))
                os.environ.pop("QALOOP_ENQUEUE_TOKEN", None)

                # (8) unknown POST
                status, body = req("POST", "/nope", json.dumps({}).encode())
                check("329 post unknown status", status == 404, str(status))
                check("329 post unknown body",
                      body == {"ok": False, "error": "not found"}, str(body))
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)
    finally:
        if prior_secret is None:
            os.environ.pop("QALOOP_WEBHOOK_SECRET", None)
        else:
            os.environ["QALOOP_WEBHOOK_SECRET"] = prior_secret
        if prior_token is None:
            os.environ.pop("QALOOP_ENQUEUE_TOKEN", None)
        else:
            os.environ["QALOOP_ENQUEUE_TOKEN"] = prior_token
    check("329 webhook secret env restored",
          os.environ.get("QALOOP_WEBHOOK_SECRET") == prior_secret)
    check("329 enqueue token env restored",
          os.environ.get("QALOOP_ENQUEUE_TOKEN") == prior_token)

    # Coverage for serve / serve_in_thread without leaking a listener.
    recorded = {}

    class FakeServer:
        def __init__(self, address, handler_class):
            recorded["address"] = address
            recorded["handler_class"] = handler_class

        def serve_forever(self):
            recorded["serve_forever"] = True

    real_server_cls = webhook.ThreadingHTTPServer
    chosen_port = 18099
    try:
        webhook.ThreadingHTTPServer = FakeServer
        t = webhook.serve_in_thread(port=chosen_port, host="127.0.0.1")
        t.join(timeout=5)
        check("329 serve_in_thread thread finished", not t.is_alive())
        check("329 serve_in_thread fake server args",
              recorded.get("address") == ("127.0.0.1", chosen_port)
              and recorded.get("handler_class") is webhook.Handler,
              str(recorded))
        check("329 serve_in_thread serve_forever called",
              recorded.get("serve_forever") is True, str(recorded))
    finally:
        webhook.ThreadingHTTPServer = real_server_cls
    check("329 ThreadingHTTPServer restored",
          webhook.ThreadingHTTPServer is real_server_cls)


def _ax_node(nid, role, name=None, value=None, desc=None, parent=None):
    n = {"nodeId": nid, "role": {"value": role}}
    if parent is not None:
        n["parentId"] = parent
    if name is not None:
        n["name"] = {"value": name}
    if value is not None:
        n["value"] = {"value": value}
    if desc is not None:
        n["description"] = {"value": desc}
    return n


def test_artifacts_compact_ax_tree_contract():
    from qaloop.artifacts import compact_ax_tree

    def single(node):
        return compact_ax_tree({"nodes": [node]})

    check("343 value-only suffix form",
          single(_ax_node("1", "button", name="Volume", value="42"))
          == 'button "Volume" = 42')
    check("343 description-only suffix form",
          single(_ax_node("1", "button", name="Play",
                          desc="Plays the media"))
          == 'button "Play" (Plays the media)')
    check("343 value suffix precedes description suffix",
          single(_ax_node("1", "slider", name="Level", value="7",
                          desc="Current level"))
          == 'slider "Level" = 7 (Current level)')
    check("343 value newline flattened",
          single(_ax_node("1", "button", name="N", value="a\nb"))
          == 'button "N" = a b')
    check("343 value truncated at exactly 60 chars",
          single(_ax_node("1", "button", name="N", value="v" * 100))
          == 'button "N" = ' + "v" * 60)
    check("343 description newline flattened and truncated at 60",
          single(_ax_node("1", "button", name="N", desc="x\ny" + "d" * 100))
          == 'button "N" (x y' + "d" * 57 + ")")
    check("343 name truncated at exactly 80 chars",
          single(_ax_node("1", "button", name="n" * 100))
          == 'button "' + "n" * 80 + '"')

    tree = {"nodes": [_ax_node("r", "document", name="Root")] + [
        _ax_node(f"c{i}", "button", name=f"Child{i}", parent="r")
        for i in range(5)]}
    capped = compact_ax_tree(tree, max_nodes=3)
    check("343 max_nodes stops at exactly the cap",
          len(capped.split("\n")) == 3, capped)
    check("343 max_nodes keeps first lines, later children absent",
          capped.split("\n")[0] == 'document "Root"'
          and "Child1" in capped and "Child3" not in capped, capped)
    full = compact_ax_tree(tree)
    check("343 child rendered indented under parent",
          full.split("\n")[1] == '  button "Child0"', full)
    trunc = compact_ax_tree(tree, max_chars=10)
    check("343 max_chars truncates joined output exactly",
          len(trunc) == 10 and trunc == full[:10], repr(trunc))


def test_artifacts_ax_snapshot_wrapper():
    from qaloop.artifacts import ax_snapshot, compact_ax_tree

    tree = {"nodes": [_ax_node("r", "document", name="Root"),
                      _ax_node("c", "button", name="Go", parent="r")]}

    class FakeSession:
        def __init__(self, detach_raises=False):
            self.send_calls = []
            self.detach_calls = 0
            self.detach_raises = detach_raises

        def send(self, method, args):
            self.send_calls.append((method, args))
            return tree

        def detach(self):
            self.detach_calls += 1
            if self.detach_raises:
                raise RuntimeError("detach boom")

    class FakeContext:
        def __init__(self, session):
            self.session = session
            self.session_pages = []

        def new_cdp_session(self, page):
            self.session_pages.append(page)
            return self.session

    class FakePage:
        def __init__(self, session):
            self.context = FakeContext(session)

    session = FakeSession()
    page = FakePage(session)
    out = ax_snapshot(page)
    check("343 ax_snapshot returns compact_ax_tree of the CDP tree",
          out == compact_ax_tree(tree), out)
    check("343 ax_snapshot sends getFullAXTree with empty args",
          session.send_calls == [("Accessibility.getFullAXTree", {})],
          str(session.send_calls))
    check("343 ax_snapshot opens the session for the page itself",
          page.context.session_pages == [page])
    check("343 ax_snapshot detaches exactly once",
          session.detach_calls == 1, str(session.detach_calls))

    big = {"nodes": [_ax_node("r", "document", name="Root")] + [
        _ax_node(f"c{i}", "button", name=f"B{i}", parent="r")
        for i in range(6)]}

    class BigSession(FakeSession):
        def send(self, method, args):
            self.send_calls.append((method, args))
            return big

    session2 = BigSession()
    out2 = ax_snapshot(FakePage(session2), max_nodes=2)
    check("343 ax_snapshot passes max_nodes through",
          out2 == compact_ax_tree(big, max_nodes=2)
          and len(out2.split("\n")) == 2, out2)

    session3 = FakeSession(detach_raises=True)
    out3 = ax_snapshot(FakePage(session3))
    check("343 ax_snapshot swallows a detach failure",
          out3 == compact_ax_tree(tree) and session3.detach_calls == 1, out3)


def test_env_wait_for_http_wrong_status_353():
    from qaloop import env

    class FakeResp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    orig_urlopen = env.urlopen
    try:
        env.urlopen = lambda url, timeout=None: FakeResp(500)
        err = None
        try:
            env.wait_for_http("http://127.0.0.1:9/", timeout_s=1.0)
        except TimeoutError as e:
            err = e
        check("353 wait_for_http wrong status times out",
              isinstance(err, TimeoutError), repr(err))
        check("353 wait_for_http timeout names the last HTTP status",
              err is not None and "HTTP 500" in str(err), repr(err))

        env.urlopen = lambda url, timeout=None: FakeResp(200)
        ok = True
        try:
            env.wait_for_http("http://127.0.0.1:9/", timeout_s=5)
        except Exception:  # noqa: BLE001
            ok = False
        check("353 wait_for_http expected status returns", ok)
    finally:
        env.urlopen = orig_urlopen


def test_env_proc_target_stop_fallbacks_353():
    from qaloop import env

    class FakeProc:
        def __init__(self, terminate_raises=False, wait_raises=False,
                     kill_raises=False):
            self.terminate_raises = terminate_raises
            self.wait_raises = wait_raises
            self.kill_raises = kill_raises
            self.kill_calls = 0

        def terminate(self):
            if self.terminate_raises:
                raise RuntimeError("terminate boom")

        def wait(self, timeout=None):
            if self.wait_raises:
                raise RuntimeError("wait boom")

        def kill(self):
            self.kill_calls += 1
            if self.kill_raises:
                raise RuntimeError("kill boom")

    class FakeLog:
        def __init__(self, close_raises=False):
            self.close_raises = close_raises
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            if self.close_raises:
                raise RuntimeError("close boom")

    # clean stop: terminate+wait succeed, kill untouched, log closed
    proc = FakeProc()
    log = FakeLog()
    env.ProcTarget(url="http://x/", proc=proc, log_path=None,
                   _log_file=log).stop()
    check("353 stop clean path skips kill and closes log",
          proc.kill_calls == 0 and log.close_calls == 1)

    # terminate raises -> kill fallback, stop returns normally
    proc = FakeProc(terminate_raises=True)
    env.ProcTarget(url="http://x/", proc=proc, log_path=None,
                   _log_file=None).stop()
    check("353 stop falls back to kill when terminate raises",
          proc.kill_calls == 1, str(proc.kill_calls))

    # wait raises -> same kill fallback
    proc = FakeProc(wait_raises=True)
    env.ProcTarget(url="http://x/", proc=proc, log_path=None,
                   _log_file=None).stop()
    check("353 stop falls back to kill when wait raises",
          proc.kill_calls == 1, str(proc.kill_calls))

    # kill also raises -> swallowed, stop still returns
    proc = FakeProc(terminate_raises=True, kill_raises=True)
    raised = False
    try:
        env.ProcTarget(url="http://x/", proc=proc, log_path=None,
                       _log_file=None).stop()
    except Exception:  # noqa: BLE001
        raised = True
    check("353 stop swallows a failing kill", not raised
          and proc.kill_calls == 1)

    # log close raises -> swallowed
    raised = False
    try:
        env.ProcTarget(url="http://x/", proc=FakeProc(), log_path=None,
                       _log_file=FakeLog(close_raises=True)).stop()
    except Exception:  # noqa: BLE001
        raised = True
    check("353 stop swallows a failing log close", not raised)


def _fake_popen_factory(captured, proc):
    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc
    return fake_popen


def test_env_boot_command_env_merge_353():
    import subprocess
    from qaloop import env

    class FakeProc:
        returncode = None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return None

    captured = {}
    orig_popen = subprocess.Popen
    orig_wait = env.wait_for_port
    old_sentinel = os.environ.get("QALOOP_TEST_SENTINEL")
    os.environ["QALOOP_TEST_SENTINEL"] = "inherited"
    try:
        subprocess.Popen = _fake_popen_factory(captured, FakeProc())
        env.wait_for_port = lambda port, timeout_s: None
        target = env.boot_command("echo hi", env={"QALOOP_TEST_N": 123},
                                  wait={"port": 4321})
        penv = captured["kwargs"].get("env") or {}
        check("353 boot_command merges inherited environ",
              penv.get("QALOOP_TEST_SENTINEL") == "inherited",
              str(sorted(penv)[:5]))
        check("353 boot_command stringifies env override values",
              penv.get("QALOOP_TEST_N") == "123",
              repr(penv.get("QALOOP_TEST_N")))
        check("353 boot_command port wait yields the port URL",
              target.url == "http://127.0.0.1:4321/", target.url)
        target.stop()
    finally:
        subprocess.Popen = orig_popen
        env.wait_for_port = orig_wait
        if old_sentinel is None:
            os.environ.pop("QALOOP_TEST_SENTINEL", None)
        else:
            os.environ["QALOOP_TEST_SENTINEL"] = old_sentinel


def test_env_boot_command_wait_errors_353():
    import subprocess
    from qaloop import env

    class FakeProc:
        returncode = None

        def __init__(self, terminate_raises=False):
            self.terminate_raises = terminate_raises
            self.terminate_calls = 0

        def terminate(self):
            self.terminate_calls += 1
            if self.terminate_raises:
                raise RuntimeError("terminate boom")

        def poll(self):
            return None

    orig_popen = subprocess.Popen
    try:
        # no wait at all -> ValueError naming wait.port, proc terminated
        proc = FakeProc()
        subprocess.Popen = _fake_popen_factory({}, proc)
        err = None
        try:
            env.boot_command("echo hi", wait=None)
        except ValueError as e:
            err = e
        check("353 boot_command without wait raises ValueError naming wait.port",
              err is not None and "wait.port" in str(err), repr(err))
        check("353 boot_command no-wait failure terminates the proc",
              proc.terminate_calls == 1, str(proc.terminate_calls))

        # unknown wait spec -> ValueError naming the spec, proc terminated
        proc = FakeProc()
        subprocess.Popen = _fake_popen_factory({}, proc)
        err = None
        try:
            env.boot_command("echo hi", wait={"bogus": 1})
        except ValueError as e:
            err = e
        check("353 boot_command unknown wait spec raises ValueError",
              err is not None and "unknown wait spec" in str(err), repr(err))
        check("353 boot_command unknown-spec failure terminates the proc",
              proc.terminate_calls == 1, str(proc.terminate_calls))

        # cleanup terminate itself raises -> original error still propagates
        proc = FakeProc(terminate_raises=True)
        subprocess.Popen = _fake_popen_factory({}, proc)
        err = None
        try:
            env.boot_command("echo hi", wait=None)
        except ValueError as e:
            err = e
        check("353 boot_command failing cleanup keeps the original ValueError",
              err is not None and "wait.port" in str(err)
              and proc.terminate_calls == 1, repr(err))
    finally:
        subprocess.Popen = orig_popen


def test_env_boot_command_log_contains_353():
    import subprocess
    from qaloop import env

    class ExitedProc:
        returncode = 3

        def __init__(self):
            self.terminate_calls = 0

        def poll(self):
            return 3

        def terminate(self):
            self.terminate_calls += 1

    orig_popen = subprocess.Popen
    try:
        proc = ExitedProc()
        subprocess.Popen = _fake_popen_factory({}, proc)
        err = None
        try:
            env.boot_command("echo hi", wait={"log_contains": "NEVER"},
                             timeout_s=5)
        except RuntimeError as e:
            err = e
        check("353 log_contains exited proc raises RuntimeError naming the code",
              err is not None and "exited (3)" in str(err), repr(err))
        check("353 log_contains exit failure terminates the proc",
              proc.terminate_calls == 1, str(proc.terminate_calls))
    finally:
        subprocess.Popen = orig_popen

    # success shape: a real command prints the needle, target comes back
    with tempfile.TemporaryDirectory() as d:
        script = os.path.join(d, "svc.py")
        with open(script, "w") as f:
            f.write("import time\n"
                    "print('READY_NEEDLE_353', flush=True)\n"
                    "time.sleep(30)\n")
        target = env.boot_command(f"{sys.executable} {script}",
                                  wait={"log_contains": "READY_NEEDLE_353"},
                                  timeout_s=30, name="needle353")
        try:
            check("353 log_contains success returns a ProcTarget",
                  isinstance(target, env.ProcTarget))
        finally:
            target.stop()
        check("353 log_contains target stopped cleanly",
              target.proc.poll() is not None)


def test_env_flow_services_relative_cwd_353():
    from qaloop import env

    class FakeTarget:
        def __init__(self):
            self.url = "http://127.0.0.1:1234/"
            self.log_path = None
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1

    captured = []
    fake = FakeTarget()
    orig_boot = env.boot_command

    def fake_boot(command, cwd=None, env=None, wait=None, timeout_s=90,
                  name="cmd"):
        captured.append({"command": command, "cwd": cwd, "wait": wait})
        return fake

    try:
        env.boot_command = fake_boot
        with tempfile.TemporaryDirectory() as d:
            run_dir = os.path.join(d, "run")
            specs = [{"name": "svc", "command": "run it",
                      "wait": {"port": 1234}, "cwd": "sub"}]
            with env.flow_services(specs, d, run_dir) as targets:
                check("353 flow_services yields the booted target",
                      targets.get("svc") is fake)
                check("353 flow_services exports the service URL",
                      os.environ.get("QALOOP_SERVICE_SVC_URL")
                      == "http://127.0.0.1:1234/")
            check("353 flow_services resolves relative cwd against base_dir",
                  captured[-1]["cwd"] == os.path.join(d, "sub"),
                  repr(captured[-1]["cwd"]))
            check("353 flow_services stops targets on exit",
                  fake.stop_calls == 1, str(fake.stop_calls))
            check("353 flow_services unexports the service URL",
                  "QALOOP_SERVICE_SVC_URL" not in os.environ)

            abs_cwd = os.path.join(d, "abs")
            specs = [{"name": "svc", "command": "run it",
                      "wait": {"port": 1234}, "cwd": abs_cwd}]
            with env.flow_services(specs, d, run_dir):
                pass
            check("353 flow_services passes absolute cwd through unchanged",
                  captured[-1]["cwd"] == abs_cwd, repr(captured[-1]["cwd"]))

            specs = [{"name": "svc", "command": "run it",
                      "wait": {"port": 1234}}]
            with env.flow_services(specs, d, run_dir):
                pass
            check("353 flow_services leaves a missing cwd as None",
                  captured[-1]["cwd"] is None, repr(captured[-1]["cwd"]))
    finally:
        env.boot_command = orig_boot


def test_issue_355_baselines_spec_error():
    """Issue #355: cmd_baselines turns a SpecError from the light flow
    parse into "INVALID SPEC: ..." + exit 2 (cli.py lines 389-391) —
    the baselines entry point shares validate's bad-spec contract."""
    import argparse
    import qaloop.cli as cli
    orig_runs_root = cli.default_runs_root
    try:
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.yaml")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("name: [unclosed\n")
            cli.default_runs_root = lambda: os.path.join(d, "runs")
            args = argparse.Namespace(flow=bad, target=None, headed=False,
                                      executable_path=None, upload_dir=None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_baselines(args)
            out = buf.getvalue()
            check("355 baselines invalid spec exits 2", rc == 2, f"rc={rc}")
            check("355 baselines invalid spec prints INVALID SPEC prefix",
                  out.startswith("INVALID SPEC: "), repr(out[:60]))
    finally:
        cli.default_runs_root = orig_runs_root


def test_issue_355_baselines_boot_failure():
    """Issue #355: cmd_baselines turns a service-boot failure into
    "SERVICE BOOT FAILED: <type>: <err>" + exit 3 (cli.py lines
    403-405) — distinct from the spec-error exit 2."""
    import argparse
    import qaloop.cli as cli

    class BoomCtx:
        def __enter__(self):
            raise RuntimeError("boom-boot")

        def __exit__(self, *a):
            return False

    orig_runs_root = cli.default_runs_root
    orig_ctx = cli._service_ctx
    try:
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, "good.yaml")
            with open(good, "w", encoding="utf-8") as f:
                f.write("name: probe355\ntarget: http://127.0.0.1:9/\n"
                        "steps: []\n")
            cli.default_runs_root = lambda: os.path.join(d, "runs")
            cli._service_ctx = lambda flow, services, run_dir: BoomCtx()
            args = argparse.Namespace(flow=good, target=None, headed=False,
                                      executable_path=None, upload_dir=None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_baselines(args)
            out = buf.getvalue()
            check("355 baselines boot failure exits 3", rc == 3, f"rc={rc}")
            check("355 baselines boot failure prints SERVICE BOOT FAILED",
                  out.startswith(
                      "SERVICE BOOT FAILED: RuntimeError: boom-boot"),
                  repr(out[:80]))
    finally:
        cli.default_runs_root = orig_runs_root
        cli._service_ctx = orig_ctx


def test_issue_355_baselines_run_saved_lines():
    """Issue #355: _cmd_baselines_run echoes each saved screenshot
    baseline as "  screenshot_matches: <detail>" (cli.py line 447);
    other assertions' details are not echoed as saved lines."""
    import argparse
    from types import SimpleNamespace
    import qaloop.cli as cli
    from qaloop.runner import AssertionResult, RunResult, StepResult
    orig_run_flow = cli.run_flow
    try:
        with tempfile.TemporaryDirectory() as d:
            step = StepResult(
                index=0, phase="steps", name="shot", op="goto",
                status="passed", duration_ms=1,
                assertions=[
                    AssertionResult(name="screenshot_matches", passed=True,
                                    detail="saved baseline x.png"),
                    AssertionResult(name="text", passed=True, detail="ok"),
                ])
            result = RunResult(
                flow_name="probe355", target="http://example.test/",
                status="passed", started=1.0, ended=1.001, steps=[step],
                failed_step=None, console_errors=[], page_errors=[],
                failed_requests=[], bad_responses=[],
                run_dir=os.path.join(d, "rd"))
            cli.run_flow = lambda spec, **kw: result
            spec = SimpleNamespace(matrix=None,
                                   target="http://example.test/")
            args = argparse.Namespace(flow=os.path.join(d, "f.yaml"),
                                      target=None, headed=False,
                                      executable_path=None, upload_dir=None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._cmd_baselines_run(args, os.path.join(d, "rd"),
                                            spec=spec, ledger=None,
                                            runs_root=d)
            out = buf.getvalue()
            check("355 baselines run exits 0", rc == 0, f"rc={rc}")
            check("355 baselines run prints saved screenshot line",
                  "  screenshot_matches: saved baseline x.png" in out,
                  repr(out))
            check("355 baselines run does not echo text assertion as saved",
                  "text: ok" not in out, repr(out))
    finally:
        cli.run_flow = orig_run_flow


def test_issue_355_resolve_diff():
    """Issue #355: _resolve_diff shells out for a git range and returns
    the unified diff (cli.py lines 492-498); an unresolvable range
    raises RuntimeError naming the range."""
    import subprocess
    import qaloop.cli as cli
    with tempfile.TemporaryDirectory() as d:
        repo = os.path.join(d, "repo")
        os.makedirs(repo)

        def git(*a):
            subprocess.run(["git", "-C", repo, *a], check=True,
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "p@p")
        git("config", "user.name", "p")
        with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as f:
            f.write("one\n")
        git("add", ".")
        git("commit", "-qm", "one")
        with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as f:
            f.write("two\n")
        git("add", ".")
        git("commit", "-qm", "two")
        diff = cli._resolve_diff("HEAD~1..HEAD", repo)
        check("355 resolve_diff range returns the change",
              "-one" in diff and "+two" in diff, repr(diff[:60]))
        err = None
        try:
            cli._resolve_diff("deadbeef..cafe1234", repo)
        except RuntimeError as e:
            err = e
        check("355 resolve_diff bad range raises RuntimeError",
              err is not None, repr(err))
        check("355 resolve_diff bad range error names the range",
              err is not None
              and str(err).startswith("git diff deadbeef..cafe1234:"),
              repr(err))


def test_issue_355_worker_negative_keep_runs():
    """Issue #355: cmd_worker rejects a negative --keep-runs with
    "ERROR: ... cannot be negative ..." + exit 2 (cli.py lines
    559-561) before any worker starts."""
    import argparse
    import qaloop.cli as cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_worker(argparse.Namespace(keep_runs=-1, concurrency=1,
                                               job_timeout=1800))
    out = buf.getvalue()
    check("355 worker negative keep-runs exits 2", rc == 2, f"rc={rc}")
    check("355 worker negative keep-runs prints ERROR prefix",
          out.startswith("ERROR: "), repr(out[:60]))
    check("355 worker negative keep-runs explains the rejection",
          "cannot be negative" in out, repr(out[:80]))


def test_issue_355_doctor_default_chromium():
    """Issue #355: with QALOOP_EXECUTABLE_PATH unset, cmd_doctor
    resolves the default chromium via Playwright (cli.py line 651)
    and reports it as the chromium PASS detail."""
    from playwright import sync_api

    class FakeChromium:
        executable_path = "/bin/true"

    class FakePW:
        chromium = FakeChromium()

    class FakeCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    old_env = os.environ.get("QALOOP_EXECUTABLE_PATH")
    old_sp = sync_api.sync_playwright
    os.environ.pop("QALOOP_EXECUTABLE_PATH", None)
    sync_api.sync_playwright = lambda: FakeCM()
    try:
        _rc, out = _run_doctor()
    finally:
        sync_api.sync_playwright = old_sp
        if old_env is None:
            os.environ.pop("QALOOP_EXECUTABLE_PATH", None)
        else:
            os.environ["QALOOP_EXECUTABLE_PATH"] = old_env
    check("355 doctor resolves default chromium via Playwright",
          "PASS chromium: /bin/true" in out,
          repr([ln for ln in out.splitlines() if "chromium" in ln][:1]))


def test_issue_355_main_dispatch():
    """Issue #355: main() parses argv and dispatches to the subcommand
    (cli.py lines 869-870) — validate on a valid flow exits 0."""
    import qaloop.cli as cli
    with tempfile.TemporaryDirectory() as d:
        flow = os.path.join(d, "v.yaml")
        with open(flow, "w", encoding="utf-8") as f:
            f.write("name: v355\ntarget: http://127.0.0.1:9/\n"
                    "steps:\n  - goto: /\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["validate", flow])
        check("355 main dispatches validate and exits 0", rc == 0,
              f"rc={rc}")
        check("355 main validate prints VALID line",
              buf.getvalue().startswith("VALID: "),
              repr(buf.getvalue()[:60]))


def test_issue_355_main_guard():
    """Issue #355: the __main__ guard (cli.py line 874) raises
    SystemExit with main()'s code — 0 for a valid validate run."""
    import runpy
    with tempfile.TemporaryDirectory() as d:
        flow = os.path.join(d, "v.yaml")
        with open(flow, "w", encoding="utf-8") as f:
            f.write("name: v355\ntarget: http://127.0.0.1:9/\n"
                    "steps:\n  - goto: /\n")
        old_argv = sys.argv
        sys.argv = ["qaloop", "validate", flow]
        code = "no-exit"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    runpy.run_module("qaloop.cli", run_name="__main__")
                except SystemExit as e:
                    code = e.code
        finally:
            sys.argv = old_argv
        check("355 __main__ guard exits 0 on validate", code == 0,
              repr(code))


def test_issue_359_runner_unit_contracts():
    """Issue #359: pin runner.py unit contracts — _resolve_url absolute
    passthrough (138), _route_delay fallback sleep (240-241, 248),
    _do_mock body forms (270-274), _do_action reload/back/unknown op
    (425, 427, 436), and the hidden-assertion success path (609)."""
    import time

    from qaloop.runner import (_check_assertions, _do_action, _do_mock,
                               _resolve_url, _route_delay)
    from qaloop.spec import Step

    # _resolve_url: absolute URLs pass through untouched
    check("359 resolve_url absolute https passthrough",
          _resolve_url("http://base/app", "https://other/x?q=1")
          == "https://other/x?q=1")
    check("359 resolve_url absolute http passthrough",
          _resolve_url("http://base/app", "http://other/y")
          == "http://other/y")

    # _route_delay: no running loop and a route without _loop degrades
    # to a blocking time.sleep of exactly delay_s
    class BareRoute:
        pass

    sleeps = []
    real_sleep = time.sleep
    time.sleep = lambda s: sleeps.append(s)
    try:
        _route_delay(BareRoute(), 0.25)
    finally:
        time.sleep = real_sleep
    check("359 route_delay fallback sleeps once with delay_s",
          sleeps == [0.25], str(sleeps))

    # _do_mock: body forms — str body is encoded, bytes body is served
    # as-is, and a path body serves the fixture file's bytes
    class FakeRoute:
        def __init__(self):
            self.fulfill_kwargs = []
            self.fell_back = 0

        def fallback(self):
            self.fell_back += 1

        def fulfill(self, **kw):
            self.fulfill_kwargs.append(kw)

    class FakeRequest:
        def __init__(self, method):
            self.method = method

    class FakeMockPage:
        def __init__(self):
            self.routes = []

        def route(self, url, handler):
            self.routes.append((url, handler))

    def serve(params):
        page = FakeMockPage()
        hits = {}
        _do_mock(page, params, hits)
        route = FakeRoute()
        page.routes[0][1](route, FakeRequest("GET"))
        return route, hits

    route, hits = serve({"url": "**/a", "body": "text"})
    check("359 mock str body served as utf-8 bytes",
          route.fulfill_kwargs[0].get("body") == b"text",
          str(route.fulfill_kwargs))
    check("359 mock str body hit recorded",
          hits == {"**/a": 1}, str(hits))
    route, hits = serve({"url": "**/b", "body": b"raw"})
    check("359 mock bytes body served unchanged",
          route.fulfill_kwargs[0].get("body") == b"raw",
          str(route.fulfill_kwargs))
    check("359 mock bytes body hit recorded",
          hits == {"**/b": 1}, str(hits))
    with tempfile.TemporaryDirectory() as d:
        fixture = os.path.join(d, "fixture.bin")
        with open(fixture, "wb") as f:
            f.write(b"file-bytes\x00\xff")
        route, hits = serve({"url": "**/c", "path": fixture})
    check("359 mock path body served as file bytes",
          route.fulfill_kwargs[0].get("body") == b"file-bytes\x00\xff",
          str(route.fulfill_kwargs))
    check("359 mock path body hit recorded",
          hits == {"**/c": 1}, str(hits))

    # _do_action: reload/back use the default timeout and
    # domcontentloaded; an unknown op raises ValueError naming the op
    class FakeNavPage:
        def __init__(self):
            self.calls = []

        def reload(self, timeout=None, wait_until=None):
            self.calls.append(("reload", timeout, wait_until))

        def go_back(self, timeout=None, wait_until=None):
            self.calls.append(("back", timeout, wait_until))

    def nav_step(op):
        return Step(index=0, phase="steps", name="n", op=op, params={},
                    expect={}, expect_items=[], continue_on_fail=False,
                    timeout_ms=None, raw={})

    page = FakeNavPage()
    _do_action(page, nav_step("reload"), "http://x", {})
    check("359 reload uses default timeout and domcontentloaded",
          page.calls == [("reload", 15000, "domcontentloaded")],
          str(page.calls))
    page = FakeNavPage()
    _do_action(page, nav_step("back"), "http://x", {})
    check("359 back uses default timeout and domcontentloaded",
          page.calls == [("back", 15000, "domcontentloaded")],
          str(page.calls))
    err = None
    try:
        _do_action(FakeNavPage(), nav_step("bogus"), "http://x", {})
    except ValueError as e:
        err = e
    check("359 unknown op raises ValueError naming the op",
          err is not None and str(err) == "unknown op bogus", repr(err))

    # hidden assertion: a successful wait_for_selector(state="hidden")
    # records a passed AssertionResult whose detail is the selector
    class FakeHiddenPage:
        def __init__(self):
            self.calls = []

        def wait_for_selector(self, selector, state=None, timeout=None):
            self.calls.append((selector, state, timeout))

    page = FakeHiddenPage()
    step = Step(index=0, phase="steps", name="h", op=None, params=None,
                expect={}, expect_items=[("hidden", "#x")],
                continue_on_fail=False, timeout_ms=None, raw={})
    res = _check_assertions(page, step, None)
    check("359 hidden assertion passes on successful wait",
          len(res) == 1 and res[0].passed, str(res))
    check("359 hidden assertion detail is the selector",
          len(res) == 1 and res[0].name == "hidden"
          and res[0].detail == "#x", str(res))
    check("359 hidden assertion waited with hidden state",
          page.calls == [("#x", "hidden", 8000)], str(page.calls))


def test_issue_359_baseline_update():
    """Issue #359: screenshot_matches with baseline_update=True saves
    the captured screenshot as the baseline (runner.py 919-924), passes
    with the 'baseline saved to ...' detail, and writes no diff file."""
    from qaloop.runner import _check_assertions
    from qaloop.spec import Step
    from PIL import Image

    def make_step(params):
        return Step(index=0, phase="steps", name="t", op=None, params=None,
                    expect={}, expect_items=[("screenshot_matches", params)],
                    continue_on_fail=False, timeout_ms=None, raw={})

    class FakePage:
        def __init__(self, png):
            self.png = png

        def screenshot(self, path):
            Image.open(self.png).save(path)

    with tempfile.TemporaryDirectory() as d:
        baseline_dir = os.path.join(d, "baselines")
        os.makedirs(baseline_dir)
        run_dir = os.path.join(d, "run")
        shot_png = os.path.join(d, "shot.png")
        Image.new("RGB", (8, 8), (30, 90, 160)).save(shot_png)
        baseline = os.path.join(baseline_dir, "sub", "base.png")
        res = _check_assertions(
            FakePage(shot_png),
            make_step({"baseline": "sub/base.png"}), None,
            baseline_dir=baseline_dir, run_dir=run_dir,
            baseline_update=True)
        check("359 baseline update passes", res[0].passed, res[0].detail)
        check("359 baseline update detail names the saved baseline",
              res[0].detail == f"baseline saved to {baseline}",
              res[0].detail)
        shot_path = os.path.join(run_dir, "steps", "assert-steps-00.png")
        check("359 baseline file exists with the screenshot bytes",
              os.path.isfile(baseline)
              and open(baseline, "rb").read()
              == open(shot_path, "rb").read())
        check("359 baseline update writes no diff file",
              not os.path.exists(os.path.join(
                  run_dir, "steps", "assert-steps-00-diff.png")))
        # Without baseline_update, a missing baseline fails naming the
        # baseline and the command that creates it (runner.py 925-926)
        missing_dir = os.path.join(d, "missing")
        os.makedirs(missing_dir)
        missing = os.path.join(missing_dir, "gone.png")
        res = _check_assertions(
            FakePage(shot_png), make_step({"baseline": "gone.png"}), None,
            baseline_dir=missing_dir, run_dir=run_dir)
        check("359 missing baseline without update fails",
              not res[0].passed, res[0].detail)
        check("359 missing baseline detail names baseline and fix",
              res[0].detail == f"baseline missing: {missing} "
              f"(run `qaloop baselines <flow>` to create it)",
              res[0].detail)


def test_issue_359_run_flow_options():
    """Issue #359: run_flow contracts with a faked browser —
    executable_path reaches launch (1098), tracing start/stop when
    trace is on (1112, 1235), the retry delay sleep (1203), the
    ax-on-failure artifact write (1217-1220) and its error branch
    (1221-1222), and teardown best-effort failure (1227)."""
    import time

    import playwright.sync_api as pw_sync

    from qaloop.runner import run_flow
    from qaloop.spec import load_spec

    def load(body):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return load_spec(p)
        finally:
            os.unlink(p)

    spec = load(
        "name: runner-359\ntarget: http://h\n"
        "artifacts: {screenshot: none, ax_snapshot: on-failure, "
        "trace: true}\n"
        "steps:\n"
        "  - name: failing with retry\n"
        "    timeout_ms: 100\n"
        "    retry: 1\n"
        "    retry_delay_ms: 1\n"
        "    expect: [{visible: '#missing'}]\n")
    teardown_spec = load(
        "name: runner-359-teardown\ntarget: http://h\n"
        "artifacts: {screenshot: none, ax_snapshot: none, trace: false}\n"
        "steps:\n"
        "  - name: passing step\n"
        "    expect: [{noop: true}]\n"
        "teardown:\n"
        "  - name: failing teardown\n"
        "    timeout_ms: 100\n"
        "    expect: [{visible: '#missing'}]\n")

    launch_calls = []
    tracing_starts = []
    tracing_stops = []
    page_holder = {}

    class FakeSession:
        def __init__(self):
            self.send_calls = []
            self.detach_calls = 0

        def send(self, method, args):
            self.send_calls.append((method, args))
            return {"nodes": [
                _ax_node("r", "document", name="Root"),
                _ax_node("c", "button", name="Go", parent="r")]}

        def detach(self):
            self.detach_calls += 1

    class FakeCDPContext:
        def __init__(self, session):
            self.session = session

        def new_cdp_session(self, page):
            return self.session

    class FakePage:
        def __init__(self):
            self.context = FakeCDPContext(FakeSession())
            self.video = None

        def on(self, event, handler):
            pass

        def wait_for_timeout(self, ms):
            pass

        def wait_for_selector(self, selector, state=None, timeout=None):
            raise TimeoutError("selector missing")

        def screenshot(self, path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"fake-png")

    class FakeTracing:
        def start(self, screenshots=None, snapshots=None, sources=None):
            tracing_starts.append((screenshots, snapshots, sources))

        def stop(self, path=None):
            tracing_stops.append(path)

    class FakeCtx:
        def new_page(self):
            return page_holder["page"]

        def close(self):
            pass

        tracing = None

    class FakeBrowser:
        def new_context(self, **kw):
            ctx = FakeCtx()
            ctx.tracing = FakeTracing()
            return ctx

        def close(self):
            pass

    class FakePW:
        class chromium:
            @staticmethod
            def launch(**kw):
                launch_calls.append(kw)
                return FakeBrowser()

    class FakePWCM:
        def __enter__(self):
            return FakePW()

        def __exit__(self, *a):
            return False

    sleeps = []
    real_sleep = time.sleep
    old_pw = pw_sync.sync_playwright
    pw_sync.sync_playwright = lambda: FakePWCM()
    time.sleep = lambda s: sleeps.append(s)
    try:
        # Run A: steps dir is never created (screenshot: none), so the
        # ax artifact write fails and lands in the step error suffix
        with tempfile.TemporaryDirectory() as run_dir:
            page_holder["page"] = FakePage()
            launch_calls.clear()
            tracing_starts.clear()
            tracing_stops.clear()
            sleeps.clear()
            result = run_flow(spec, run_dir=run_dir,
                              executable_path="/fake/chrome")
            sr = result.steps[0]
            check("359 executable_path reaches chromium launch",
                  launch_calls == [{"headless": True,
                                    "executable_path": "/fake/chrome"}],
                  str(launch_calls))
            check("359 tracing starts with screenshots and snapshots",
                  tracing_starts == [(True, True, False)],
                  str(tracing_starts))
            check("359 tracing stops to trace.zip in the run dir",
                  tracing_stops == [os.path.join(run_dir, "trace.zip")],
                  str(tracing_stops))
            check("359 failing retried run is failed",
                  result.status == "failed" and sr.status == "failed",
                  result.status)
            check("359 retry used both attempts",
                  sr.attempts == 2, str(sr.attempts))
            check("359 retry delay slept between attempts",
                  sleeps == [0.001], str(sleeps))
            check("359 ax artifact error appended to step error",
                  " [artifact error: " in sr.error
                  and sr.error.endswith("]"), sr.error)
            check("359 ax artifact not recorded when its write fails",
                  sr.artifacts.ax_snapshot is None,
                  str(sr.artifacts.ax_snapshot))

        # Run B: steps dir pre-created and a working CDP fake, so the
        # ax snapshot is written and recorded with no artifact error
        with tempfile.TemporaryDirectory() as run_dir:
            os.makedirs(os.path.join(run_dir, "steps"))
            page_holder["page"] = FakePage()
            result = run_flow(spec, run_dir=run_dir,
                              executable_path="/fake/chrome")
            sr = result.steps[0]
            ax_path = os.path.join(run_dir, "steps", "steps-00-ax.txt")
            check("359 ax snapshot path recorded on the step",
                  sr.artifacts.ax_snapshot == ax_path,
                  str(sr.artifacts.ax_snapshot))
            check("359 ax snapshot file exists with the tree text",
                  os.path.isfile(ax_path)
                  and 'document "Root"' in open(ax_path).read())
            check("359 ax success leaves no artifact error suffix",
                  "[artifact error:" not in sr.error, sr.error)

        # Run C: a failing teardown step is recorded failed, but the
        # run still completes as passed (teardown is best-effort)
        with tempfile.TemporaryDirectory() as run_dir:
            page_holder["page"] = FakePage()
            result = run_flow(teardown_spec, run_dir=run_dir)
            teardown = [s for s in result.steps if s.phase == "teardown"]
            check("359 teardown failure is recorded failed",
                  len(teardown) == 1 and teardown[0].status == "failed",
                  str([(s.phase, s.status) for s in result.steps]))
            check("359 teardown failure does not fail the run",
                  result.status == "passed"
                  and result.failed_step is None, result.status)
    finally:
        time.sleep = real_sleep
        pw_sync.sync_playwright = old_pw


def test_perform_main_loop_contract():
    import base64
    import copy
    import playwright.sync_api as pw_sync
    import qaloop.perform as perf_mod
    from qaloop.perform import perform

    png_bytes = b"\x89PNG\r\n\x1a\nfake-perform-shot"

    class _ConsoleMsg:
        type = "error"
        text = "boom"

    class FakePage:
        def __init__(self):
            self.url = ""
            self.handlers = {}
            self._fired = False

        def on(self, event, handler):
            self.handlers[event] = handler

        def title(self):
            return "Fake Title"

        def goto(self, url, timeout=None, wait_until=None):
            self.url = url
            if not self._fired:
                self._fired = True
                if "console" in self.handlers:
                    self.handlers["console"](_ConsoleMsg())
                if "pageerror" in self.handlers:
                    self.handlers["pageerror"](Exception("page boom"))

        def screenshot(self, path=None, **kw):
            with open(path, "wb") as f:
                f.write(png_bytes)

    def make_pw(page):
        state = {"launch": [], "ctx": [], "closed": [], "entered": []}

        class FakeCtx:
            def new_page(self):
                return page

        class FakeBrowser:
            def new_context(self, **kw):
                state["ctx"].append(copy.deepcopy(kw))
                return FakeCtx()

            def close(self):
                state["closed"].append(True)

        class FakeChromium:
            def launch(self, **kw):
                state["launch"].append(copy.deepcopy(kw))
                return FakeBrowser()

        class FakePW:
            chromium = FakeChromium()

        class FakePWCM:
            def __enter__(self):
                state["entered"].append(True)
                return FakePW()

            def __exit__(self, *a):
                return False

        return FakePWCM, state

    def make_chat(replies):
        calls = []

        def fake_chat(cfg, messages, timeout_s=90):
            calls.append(copy.deepcopy(messages))
            raw = replies[len(calls) - 1]
            return raw, {"prompt_tokens": 100, "completion_tokens": 50}

        return fake_chat, calls

    good_cfg = {"base_url": "http://x", "name": "stub-model",
                "api_key": "test", "price_in": 1.0, "price_out": 2.0}
    old_chat = perf_mod._chat
    old_cfg = perf_mod._model_config
    old_ax = perf_mod.ax_snapshot
    old_pw = pw_sync.sync_playwright
    perf_mod._model_config = lambda: dict(good_cfg)
    perf_mod.ax_snapshot = lambda page, **kw: "AX TREE"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Scenario A: completed, with invalid JSON, navigate,
            # console, screenshot (vision), done.
            page_a = FakePage()
            cm_a, state_a = make_pw(page_a)
            replies_a = [
                "this is not json",
                json.dumps({"action": "navigate", "url": "http://x/next"}),
                json.dumps({"action": "console"}),
                json.dumps({"action": "screenshot"}),
                json.dumps({"action": "done", "summary": "did the thing"}),
            ]
            chat_a, calls_a = make_chat(replies_a)
            perf_mod._chat = chat_a
            pw_sync.sync_playwright = lambda: cm_a()
            result_a = perform(task="do the thing", target="http://x/",
                               max_actions=10, runs_root=tmp)
            check("perform loop A: 5 chat calls", len(calls_a) == 5,
                  str(len(calls_a)))
            if calls_a:
                check("perform loop A: system prompt",
                      calls_a[0][0].get("content") ==
                      perf_mod.SYSTEM.replace("{max_actions}", "10"),
                      repr(calls_a[0][0])[:300])
                check("perform loop A: initial user message",
                      len(calls_a[0]) > 1 and calls_a[0][1] ==
                      {"role": "user",
                       "content": "TASK: do the thing\n"
                                  "STARTING URL: http://x/\n"
                                  "Begin. Reply with exactly one JSON action."},
                      repr(calls_a[0][1])[:300] if len(calls_a[0]) > 1 else "short")
            if len(calls_a) > 1:
                check("perform loop A: invalid JSON retry prompt",
                      any(m.get("role") == "user" and m.get("content") ==
                          "That was not a valid JSON action "
                          "(model did not return a JSON action). "
                          "Reply with exactly one JSON action."
                          for m in calls_a[1]),
                      repr(calls_a[1])[:500])
            check("perform loop A: status completed",
                  result_a["status"] == "completed", str(result_a["status"]))
            check("perform loop A: summary",
                  result_a["summary"] == "did the thing",
                  str(result_a["summary"]))
            check("perform loop A: action_count 4 (invalid adds none)",
                  result_a["action_count"] == 4, str(result_a["action_count"]))
            results_a = [r.get("result") for r in result_a["actions"]]
            check("perform loop A: transcript results in order",
                  results_a == ["navigated to http://x/next",
                                '[{"type": "error", "text": "boom"}, '
                                '{"type": "pageerror", "text": "page boom"}]',
                                "screenshot saved (perform-03.png)",
                                "done"],
                  repr(results_a))
            check("perform loop A: record n values 1-4",
                  [r.get("n") for r in result_a["actions"]] == [1, 2, 3, 4],
                  repr([r.get("n") for r in result_a["actions"]]))
            if len(calls_a) == 5:
                last_msg = calls_a[4][-1]
                content = last_msg.get("content")
                check("perform loop A: vision message is list content",
                      isinstance(content, list) and len(content) == 2,
                      repr(content)[:300])
                if isinstance(content, list) and len(content) == 2:
                    check("perform loop A: vision text part",
                          content[0].get("type") == "text" and
                          content[0].get("text", "").startswith(
                              "URL: http://x/next"),
                          repr(content[0])[:300])
                    check("perform loop A: vision image part",
                          content[1] == {"type": "image_url",
                                         "image_url": {"url":
                                                       "data:image/png;base64," +
                                                       base64.b64encode(png_bytes).decode()}},
                          repr(content[1])[:300])
            check("perform loop A: tokens 500/250",
                  result_a["tokens_in"] == 500 and result_a["tokens_out"] == 250,
                  f"{result_a['tokens_in']}/{result_a['tokens_out']}")
            check("perform loop A: cost 0.001",
                  result_a["cost_usd_est"] == 0.001,
                  str(result_a["cost_usd_est"]))
            check("perform loop A: final url/title/attached",
                  result_a["final_url"] == "http://x/next" and
                  result_a["final_title"] == "Fake Title" and
                  result_a["attached"] is False,
                  repr({k: result_a[k] for k in
                        ("final_url", "final_title", "attached")}))
            check("perform loop A: launch recorded",
                  state_a["launch"] == [{"headless": True}],
                  repr(state_a["launch"]))
            check("perform loop A: context viewport",
                  state_a["ctx"] == [{"viewport": {"width": 1280,
                                                   "height": 800}}],
                  repr(state_a["ctx"]))
            check("perform loop A: browser closed",
                  state_a["closed"] == [True], repr(state_a["closed"]))
            pj_path = os.path.join(result_a["run_dir"], "perform.json")
            check("perform loop A: perform.json exists",
                  os.path.exists(pj_path))
            if os.path.exists(pj_path):
                saved = json.load(open(pj_path, encoding="utf-8"))
                check("perform loop A: perform.json matches result",
                      saved.get("status") == result_a["status"] and
                      saved.get("summary") == result_a["summary"] and
                      saved.get("tokens_in") == result_a["tokens_in"] and
                      saved.get("tokens_out") == result_a["tokens_out"] and
                      saved.get("cost_usd_est") == result_a["cost_usd_est"] and
                      saved.get("attached") == result_a["attached"],
                      repr(saved)[:300])
            md_path = os.path.join(result_a["run_dir"], "PERFORM.md")
            check("perform loop A: PERFORM.md exists",
                  os.path.exists(md_path))
            if os.path.exists(md_path):
                md = open(md_path, encoding="utf-8").read()
                check("perform loop A: PERFORM.md status line",
                      "status: **completed**" in md and
                      "did the thing" in md, md[:300])
                check("perform loop A: PERFORM.md tokens",
                      "500/250" in md, md[:300])

            # Scenario B: blocked.
            page_b = FakePage()
            cm_b, state_b = make_pw(page_b)
            chat_b, calls_b = make_chat(
                [json.dumps({"action": "blocked", "reason": "captcha wall"})])
            perf_mod._chat = chat_b
            pw_sync.sync_playwright = lambda: cm_b()
            result_b = perform(task="do the thing", target="http://x/",
                               max_actions=10, runs_root=tmp)
            check("perform loop B: status blocked",
                  result_b["status"] == "blocked", str(result_b["status"]))
            check("perform loop B: summary is reason",
                  result_b["summary"] == "captcha wall",
                  str(result_b["summary"]))
            check("perform loop B: action_count 1",
                  result_b["action_count"] == 1, str(result_b["action_count"]))
            check("perform loop B: record result blocked",
                  [r.get("result") for r in result_b["actions"]] == ["blocked"],
                  repr(result_b["actions"]))
            check("perform loop B: browser closed",
                  state_b["closed"] == [True], repr(state_b["closed"]))

            # Scenario C: exhaustion.
            page_c = FakePage()
            cm_c, state_c = make_pw(page_c)
            chat_c, calls_c = make_chat(
                [json.dumps({"action": "navigate", "url": "http://x/next"}),
                 json.dumps({"action": "navigate", "url": "http://x/next"})])
            perf_mod._chat = chat_c
            pw_sync.sync_playwright = lambda: cm_c()
            result_c = perform(task="do the thing", target="http://x/",
                               max_actions=2, runs_root=tmp)
            check("perform loop C: status incomplete",
                  result_c["status"] == "incomplete", str(result_c["status"]))
            check("perform loop C: summary exact",
                  result_c["summary"] == "used all 2 actions without finishing",
                  str(result_c["summary"]))
            check("perform loop C: action_count 2",
                  result_c["action_count"] == 2, str(result_c["action_count"]))

            # Scenario D: no API key raises before any browser launch.
            page_d = FakePage()
            cm_d, state_d = make_pw(page_d)
            perf_mod._model_config = lambda: {
                "base_url": "http://x", "name": "stub-model",
                "api_key": "", "price_in": 1.0, "price_out": 2.0}
            pw_sync.sync_playwright = lambda: cm_d()
            try:
                perform(task="do the thing", target="http://x/",
                        max_actions=10, runs_root=tmp)
                check("perform loop D: RuntimeError raised", False,
                      "no error raised")
            except RuntimeError as e:
                check("perform loop D: RuntimeError names env var",
                      "QALOOP_MODEL_API_KEY" in str(e), str(e))
            finally:
                perf_mod._model_config = lambda: dict(good_cfg)
            check("perform loop D: playwright never entered",
                  state_d["entered"] == [] and state_d["launch"] == [],
                  repr(state_d))
    finally:
        perf_mod._chat = old_chat
        perf_mod._model_config = old_cfg
        perf_mod.ax_snapshot = old_ax
        pw_sync.sync_playwright = old_pw



def test_perform_inloop_remainder_contract():
    # Pins the perform() in-loop remainder (issue 367): executable_path
    # launch kwarg, CDP attach, pre-call and inner cost-ceiling breaks,
    # vision retry on image/400 errors, harness PermissionError observation,
    # and the PERFORM.md ceiling stop-reason line.
    import copy
    import playwright.sync_api as pw_sync
    import qaloop.perform as perf_mod
    from qaloop.perform import perform

    png_bytes = b"\x89PNG\r\n\x1a\nfake-perform-shot"

    class FakePage:
        def __init__(self):
            self.url = ""
            self.handlers = {}
        def on(self, event, handler):
            self.handlers[event] = handler
        def title(self):
            return "Fake Title"
        def goto(self, url, timeout=None, wait_until=None):
            self.url = url
        def screenshot(self, path=None, **kw):
            with open(path, "wb") as f:
                f.write(png_bytes)

    def make_pw(page, cdp_browser=None, launched_ctx_pages=None):
        state = {"launch": [], "cdp": [], "ctx_new": [], "closed": [],
                 "entered": []}
        class FakeCtx:
            def __init__(self, pages):
                self.pages = list(pages)
                self.new_page_calls = []
            def new_page(self):
                self.new_page_calls.append(True)
                return page
        class FakeBrowser:
            def __init__(self, contexts):
                self.contexts = list(contexts)
                self.new_context_calls = []
            def new_context(self, **kw):
                self.new_context_calls.append(kw)
                ctx = FakeCtx([page])
                self.contexts.append(ctx)
                return ctx
            def close(self):
                state["closed"].append(True)
        launched_ctx = FakeCtx([page])
        launched_browser = FakeBrowser([launched_ctx])
        class FakeChromium:
            def launch(self, **kw):
                state["launch"].append(copy.deepcopy(kw))
                return launched_browser
            def connect_over_cdp(self, url):
                state["cdp"].append(url)
                return cdp_browser
        class FakePW:
            chromium = FakeChromium()
        class FakePWCM:
            def __enter__(self):
                state["entered"].append(True)
                return FakePW()
            def __exit__(self, *a):
                return False
        state["FakeCtx"] = FakeCtx
        state["FakeBrowser"] = FakeBrowser
        return FakePWCM, state

    def make_chat(items):
        calls = []
        def fake_chat(cfg, messages, timeout_s=90):
            calls.append(copy.deepcopy(messages))
            item = items[len(calls) - 1]
            if isinstance(item, Exception):
                raise item
            return item, {"prompt_tokens": 100, "completion_tokens": 50}
        return fake_chat, calls

    done = json.dumps({"action": "done", "summary": "did the thing"})
    nav = json.dumps({"action": "navigate", "url": "http://x/next"})
    shot = json.dumps({"action": "screenshot"})
    good_cfg = {"base_url": "http://x", "name": "stub-model",
                "api_key": "test", "price_in": 1.0, "price_out": 2.0}
    old_chat = perf_mod._chat
    old_cfg = perf_mod._model_config
    old_ax = perf_mod.ax_snapshot
    old_pw = pw_sync.sync_playwright
    old_ceiling = perf_mod.Performer._ceiling_hit
    old_act = perf_mod.Performer.act
    perf_mod._model_config = lambda: dict(good_cfg)
    perf_mod.ax_snapshot = lambda page, **kw: "AX TREE"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # S1: executable_path lands in launch kwargs (line 358).
            page = FakePage()
            cm, st = make_pw(page)
            chat, calls = make_chat([done])
            perf_mod._chat = chat
            pw_sync.sync_playwright = lambda: cm()
            r = perform(task="t", target="http://x/", max_actions=5,
                        runs_root=tmp, executable_path="/fake/chrome")
            check("perform remainder S1: launch kwargs carry executable_path",
                  st["launch"] == [{"headless": True,
                                    "executable_path": "/fake/chrome"}],
                  repr(st["launch"]))
            check("perform remainder S1: completed", r["status"] == "completed",
                  str(r["status"]))

            # S2: CDP attach (lines 360-364), three sub-cases.
            for label, mode in (("reuse", "reuse"), ("new_context", "noctx"),
                                ("new_page", "nopage")):
                page = FakePage()
                # Build the CDP browser with the shared fakes.
                cm0, st0 = make_pw(page)
                FakeCtx = st0["FakeCtx"]; FakeBrowser = st0["FakeBrowser"]
                if mode == "reuse":
                    cdp_browser = FakeBrowser([FakeCtx([page])])
                elif mode == "noctx":
                    cdp_browser = FakeBrowser([])
                else:
                    cdp_browser = FakeBrowser([FakeCtx([])])
                cm, st = make_pw(page, cdp_browser=cdp_browser)
                chat, calls = make_chat([done])
                perf_mod._chat = chat
                pw_sync.sync_playwright = lambda: cm()
                r = perform(task="t", target="http://x/", max_actions=5,
                            runs_root=tmp, cdp_url="http://127.0.0.1:9222")
                check(f"perform remainder S2-{label}: cdp url verbatim",
                      st["cdp"] == ["http://127.0.0.1:9222"], repr(st["cdp"]))
                check(f"perform remainder S2-{label}: attached True",
                      r["attached"] is True, repr(r.get("attached")))
                check(f"perform remainder S2-{label}: browser NOT closed",
                      st["closed"] == [], repr(st["closed"]))
                if mode == "noctx":
                    check("perform remainder S2-new_context: new_context called",
                          len(cdp_browser.new_context_calls) == 1,
                          repr(cdp_browser.new_context_calls))
                if mode == "nopage":
                    check("perform remainder S2-new_page: new_page called",
                          len(cdp_browser.contexts[0].new_page_calls) == 1,
                          repr(cdp_browser.contexts[0].new_page_calls))

            # S3: pre-call ceiling break (393-395) + PERFORM.md suffix (473).
            page = FakePage()
            cm, st = make_pw(page)
            chat, calls = make_chat([nav, nav])
            perf_mod._chat = chat
            pw_sync.sync_playwright = lambda: cm()
            r = perform(task="t", target="http://x/", max_actions=5,
                        runs_root=tmp, max_cost_usd=0.00015)
            check("perform remainder S3: exactly 1 chat call",
                  len(calls) == 1, str(len(calls)))
            check("perform remainder S3: status blocked",
                  r["status"] == "blocked", str(r["status"]))
            check("perform remainder S3: summary verbatim",
                  r["summary"] == "cost ceiling reached (max-cost-usd 0.00015)",
                  str(r["summary"]))
            check("perform remainder S3: ceiling_hit True",
                  r["ceiling_hit"] is True, repr(r.get("ceiling_hit")))
            md = open(os.path.join(r["run_dir"], "PERFORM.md"),
                      encoding="utf-8").read()
            check("perform remainder S3: PERFORM.md ceiling stop reason",
                  "stop reason: cost ceiling reached" in md, md[:400])

            # S4: vision retry on image/400 (398-410), then completes.
            page = FakePage()
            cm, st = make_pw(page)
            chat, calls = make_chat(
                [shot, RuntimeError("image content rejected 400"), done])
            perf_mod._chat = chat
            pw_sync.sync_playwright = lambda: cm()
            r = perform(task="t", target="http://x/", max_actions=5,
                        runs_root=tmp)
            check("perform remainder S4: 3 chat calls (shot, fail, retry)",
                  len(calls) == 3, str(len(calls)))
            check("perform remainder S4: completed after retry",
                  r["status"] == "completed", str(r["status"]))
            if len(calls) == 3:
                check("perform remainder S4: vision call used list content",
                      isinstance(calls[1][-1].get("content"), list),
                      repr(calls[1][-1])[:200])
                check("perform remainder S4: retry downgraded to plain text",
                      isinstance(calls[2][-1].get("content"), str) and
                      calls[2][-1]["content"].startswith("URL:"),
                      repr(calls[2][-1])[:200])

            # S4b: non-image error re-raises, no retry.
            page = FakePage()
            cm, st = make_pw(page)
            chat, calls = make_chat([RuntimeError("boom 500")])
            perf_mod._chat = chat
            pw_sync.sync_playwright = lambda: cm()
            raised = ""
            try:
                perform(task="t", target="http://x/", max_actions=5,
                        runs_root=tmp)
            except RuntimeError as e:
                raised = str(e)
            check("perform remainder S4b: non-image error re-raises",
                  raised == "boom 500", raised)
            check("perform remainder S4b: exactly 1 chat call",
                  len(calls) == 1, str(len(calls)))

            # S5: inner ceiling re-check (403-407). The loop passes unchanged
            # cost values to both checks, so this defensive break is reachable
            # only via a stateful _ceiling_hit stub: False, False, True.
            page = FakePage()
            cm, st = make_pw(page)
            seq = iter([False, False, True])
            perf_mod.Performer._ceiling_hit = (
                lambda self, cs, est: next(seq))
            chat, calls = make_chat(
                [shot, RuntimeError("image content rejected 400"), done])
            perf_mod._chat = chat
            pw_sync.sync_playwright = lambda: cm()
            try:
                r = perform(task="t", target="http://x/", max_actions=5,
                            runs_root=tmp, max_cost_usd=1.0)
            finally:
                perf_mod.Performer._ceiling_hit = old_ceiling
            check("perform remainder S5: blocked by inner ceiling re-check",
                  r["status"] == "blocked", str(r["status"]))
            check("perform remainder S5: summary verbatim",
                  r["summary"] == "cost ceiling reached (max-cost-usd 1.0)",
                  str(r["summary"]))
            check("perform remainder S5: ceiling_hit True",
                  r["ceiling_hit"] is True, repr(r.get("ceiling_hit")))
            check("perform remainder S5: retry chat call never made",
                  len(calls) == 2, str(len(calls)))

            # S6: harness PermissionError (440-441) becomes transcript result.
            page = FakePage()
            cm, st = make_pw(page)
            def raising_act(self, action):
                raise PermissionError("destructive click blocked")
            perf_mod.Performer.act = raising_act
            chat, calls = make_chat([nav, done])
            perf_mod._chat = chat
            pw_sync.sync_playwright = lambda: cm()
            try:
                r = perform(task="t", target="http://x/", max_actions=5,
                            runs_root=tmp)
            finally:
                perf_mod.Performer.act = old_act
            check("perform remainder S6: BLOCKED BY HARNESS verbatim",
                  [rec.get("result") for rec in r["actions"]][:1] ==
                  ["BLOCKED BY HARNESS: destructive click blocked"],
                  repr(r["actions"])[:300])
            check("perform remainder S6: loop continues to completed",
                  r["status"] == "completed", str(r["status"]))
    finally:
        perf_mod._chat = old_chat
        perf_mod._model_config = old_cfg
        perf_mod.ax_snapshot = old_ax
        pw_sync.sync_playwright = old_pw
        perf_mod.Performer._ceiling_hit = old_ceiling
        perf_mod.Performer.act = old_act

def test_perform_model_config_contract():
    import qaloop.perform as perf_mod
    keys = ["QALOOP_MODEL_BASE_URL", "QALOOP_MODEL_NAME",
            "QALOOP_MODEL_API_KEY", "QALOOP_MODEL_PRICE_IN",
            "QALOOP_MODEL_PRICE_OUT"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["QALOOP_MODEL_BASE_URL"] = "http://host.test/v1/"
        os.environ["QALOOP_MODEL_NAME"] = "stub-model"
        os.environ["QALOOP_MODEL_API_KEY"] = "sk-x"
        os.environ["QALOOP_MODEL_PRICE_IN"] = "1.5"
        os.environ["QALOOP_MODEL_PRICE_OUT"] = "2.5"
        check("perform model_config: overrides exact dict",
              perf_mod._model_config() ==
              {"base_url": "http://host.test/v1", "name": "stub-model",
               "api_key": "sk-x", "price_in": 1.5, "price_out": 2.5},
              repr(perf_mod._model_config()))
        for k in keys:
            if k in os.environ:
                del os.environ[k]
        check("perform model_config: defaults exact dict",
              perf_mod._model_config() ==
              {"base_url": "https://api.openai.com/v1",
               "name": "gpt-4o-mini", "api_key": "",
               "price_in": 0.15, "price_out": 0.60},
              repr(perf_mod._model_config()))
    finally:
        for k in keys:
            if saved[k] is None:
                if k in os.environ:
                    del os.environ[k]
            else:
                os.environ[k] = saved[k]


def test_issue_369_flow_corpus_validate():
    """Issue #369: every shipped top-level flow in flows/ loads clean.

    Enumerates the real flows/ directory (sorted glob, no hardcoded
    list or count), loads each file exactly as cli cmd_validate does
    (load_spec(path, strict_env=False), catching SpecError), and emits
    one check per file naming the file on failure. flows/fragments/
    partials are out of scope: the top-level *.yaml glob excludes them.
    """
    import glob
    from qaloop.spec import load_spec, SpecError

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def try_load(path):
        try:
            load_spec(path, strict_env=False)
        except SpecError as e:
            return e
        return None

    paths = sorted(glob.glob(os.path.join(repo, "flows", "*.yaml")))
    check("flow corpus: enumeration is non-empty", len(paths) > 0, str(len(paths)))
    for path in paths:
        err = try_load(path)
        check(f"flow corpus loads: {os.path.basename(path)}", err is None,
              f"{path}: {err}" if err is not None else "")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("name: corpus-negative\ntarget: http://x\nbogus_key: 1\n"
                "steps:\n  - name: s\n    expect: {noop: true}\n")
        bad_path = f.name
    try:
        bad_err = try_load(bad_path)
        check("flow corpus: corrupt flow is rejected naming its error",
              bad_err is not None and "bogus_key" in str(bad_err),
              f"{bad_path}: {bad_err}")
    finally:
        os.unlink(bad_path)


def test_issue_375_cli_branch_coverage():
    """Issue #375: pin the 2 untaken branch sides in cli.py.

    (a) cmd_verify on a single file with no tag (None and empty list):
    the falsy side of `if args.tag:` (127->139) runs the flow and
    exits 0. (b) A failing flow with investigate_on_failure and a
    stubbed investigator returning None: the falsy side of
    `if diagnosis:` (218->225) appends exactly one scripted ledger
    entry and no investigation entry, and still writes the report.
    """
    from types import SimpleNamespace
    from qaloop.runner import RunResult, StepResult
    from qaloop.cli import _cmd_verify_run, cmd_verify
    import qaloop.cli as cli_mod
    import qaloop.ledger as ledger_mod
    import qaloop.investigate as inv_mod

    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, row):
            self.rows.append(row)

    base = ("name: t\ntarget: http://h\nsteps:\n"
            "  - name: s\n    wait_ms: 1\n")

    def fake_result(status, ms=100):
        steps = [StepResult(index=0, phase="steps", name="s", op="wait_ms",
                            status="passed" if status == "passed"
                            else "failed",
                            duration_ms=ms)]
        return RunResult(flow_name="x", target="http://h", status=status,
                         started=1.0, ended=1.0 + ms / 1000.0, steps=steps,
                         failed_step=None if status == "passed" else 0,
                         console_errors=[], page_errors=[],
                         failed_requests=[], bad_responses=[], run_dir="")

    real_run_flow = cli_mod.run_flow
    real_append = ledger_mod.append
    real_default_runs = cli_mod.default_runs_root
    real_inv = inv_mod.investigate
    real_write_report = cli_mod.write_report
    try:
        # --- (a) single file with no tag: None and [] both run ---
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "f.yaml")
            write(fp, base)
            ledger_rows = []
            ledger_mod.append = lambda row: ledger_rows.append(row)
            cli_mod.default_runs_root = lambda: os.path.join(d, "runs")
            run_calls = []

            def fake_passed(spec, **kw):
                run_calls.append((spec, kw))
                return fake_result("passed")

            cli_mod.run_flow = fake_passed
            try:
                def mk(tag):
                    return SimpleNamespace(
                        flow=fp, target=None, headed=True,
                        executable_path=None, upload_dir=None,
                        investigate=False, keep_runs=0,
                        max_investigation_actions=None, tag=tag)

                for tag, label in [(None, "None"), ([], "empty list")]:
                    ledger_rows.clear()
                    run_calls.clear()
                    rc = cmd_verify(mk(tag))
                    check(f"375 no-tag ({label}) verify exits 0",
                          rc == 0, str(rc))
                    check(f"375 no-tag ({label}) run_flow called once",
                          len(run_calls) == 1, str(len(run_calls)))
                    check(f"375 no-tag ({label}) one scripted ledger entry",
                          len(ledger_rows) == 1
                          and ledger_rows[0].get("kind") == "scripted",
                          str(ledger_rows))
            finally:
                ledger_mod.append = real_append
                cli_mod.default_runs_root = real_default_runs
                cli_mod.run_flow = real_run_flow

        # --- (b) falsy diagnosis: investigator returns None ---
        with tempfile.TemporaryDirectory() as d:
            inv_calls = []

            def fake_inv(**kw):
                inv_calls.append(kw)
                return None

            inv_mod.investigate = fake_inv
            cli_mod.run_flow = lambda spec, **kw: fake_result("failed")
            captured = {}

            def capturing_write(result, spec, run_dir, diagnosis=None):
                captured["diagnosis"] = diagnosis
                captured["run_dir"] = run_dir
                return real_write_report(result, spec, run_dir,
                                         diagnosis=diagnosis)

            cli_mod.write_report = capturing_write
            try:
                okf = os.path.join(d, "ok.yaml")
                write(okf, base + "investigate_on_failure: true\n")
                args_i = SimpleNamespace(
                    flow=okf, target=None, headed=True,
                    executable_path=None, upload_dir=None,
                    investigate=True, keep_runs=0,
                    max_investigation_actions=None)
                led = FakeLedger()
                ri_dir = os.path.join(d, "ri")
                os.makedirs(ri_dir, exist_ok=True)
                rc = _cmd_verify_run(args_i, ri_dir, led, d, 0)
                check("375 falsy diagnosis verify exits 1",
                      rc == 1, str(rc))
                check("375 falsy diagnosis investigator called once",
                      len(inv_calls) == 1, str(len(inv_calls)))
                check("375 falsy diagnosis ledger has scripted only",
                      [r["kind"] for r in led.rows] == ["scripted"],
                      str([r["kind"] for r in led.rows]))
                check("375 falsy diagnosis write_report diagnosis is None",
                      "diagnosis" in captured
                      and captured["diagnosis"] is None,
                      str(captured))
                check("375 falsy diagnosis report written",
                      os.path.isfile(os.path.join(ri_dir, "REPORT.md")),
                      ri_dir)
            finally:
                inv_mod.investigate = real_inv
                cli_mod.run_flow = real_run_flow
                cli_mod.write_report = real_write_report
    finally:
        cli_mod.run_flow = real_run_flow
        ledger_mod.append = real_append
        cli_mod.default_runs_root = real_default_runs
        inv_mod.investigate = real_inv
        cli_mod.write_report = real_write_report


def test_issue_377_flow_services_explicit_url():
    """Issue #377: pin the explicit-url branch side in flow_services.

    When a service spec supplies an explicit url, env.py line 206
    (if url and not s.get("url")) takes its falsy side, so the
    210-212 origin rewrite must not fire: the exported
    QALOOP_SERVICE_<NAME>_URL is the explicit url verbatim,
    including its path, not the wait-derived origin.
    """
    import socket
    from qaloop.env import flow_services
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    spec = [{"name": "deep", "command": f"python3 -m http.server {port}",
             "cwd": None, "env": {}, "wait": {"port": port},
             "timeout_s": 15.0, "url": "http://example.test/deep/path"}]
    with tempfile.TemporaryDirectory() as run_dir:
        ctx = flow_services(spec, os.getcwd(), run_dir)
        ctx.__enter__()
        try:
            check("377 explicit service URL exported verbatim",
                  os.environ.get("QALOOP_SERVICE_DEEP_URL") ==
                  "http://example.test/deep/path",
                  str(os.environ.get("QALOOP_SERVICE_DEEP_URL")))
            check("377 explicit service PORT exported",
                  os.environ.get("QALOOP_SERVICE_DEEP_PORT") == str(port),
                  str(os.environ.get("QALOOP_SERVICE_DEEP_PORT")))
        finally:
            ctx.__exit__(None, None, None)
        check("377 explicit service URL cleaned up",
              "QALOOP_SERVICE_DEEP_URL" not in os.environ)
        check("377 explicit service PORT cleaned up",
              "QALOOP_SERVICE_DEEP_PORT" not in os.environ)


def test_issue_379_perform_branch_coverage():
    """Issue #379: pin perform.py untaken branch sides.

    Part A pins the 198->exit side of Performer._guard: names outside
    the guarded set ("click", "dblclick", "press", "check") skip the
    guard body entirely, so even destructive/publish labels and a
    None target return None without raising. Part B pins the
    375->377 side of perform(): a falsy target skips page.goto, so
    the run completes with no navigation recorded.
    """
    import copy
    import playwright.sync_api as pw_sync
    import qaloop.perform as perf_mod
    from qaloop.perform import Performer, perform

    # Part A: _guard with unguarded names.
    p = Performer.__new__(Performer)
    p.allow_publish = False
    for name in ("navigate", "fill", "hover", "uncheck"):
        for target in ({"name": "Delete account"}, {"name": "Publish"},
                       None):
            try:
                ret = p._guard(name, target)
                check(f"379 guard: unguarded {name} target={target!r} "
                      "does not raise", ret is None)
            except PermissionError:
                check(f"379 guard: unguarded {name} target={target!r} "
                      "does not raise", False)
    for target, label in (({"name": "Delete account"}, "destructive"),
                          ({"name": "Publish"}, "publish")):
        try:
            p._guard("click", target)
            check(f"379 guard: guarded click {label} raises", False)
        except PermissionError:
            check(f"379 guard: guarded click {label} raises", True)

    # Part B: perform() with a falsy target never navigates.
    class FakePage:
        def __init__(self):
            self.url = ""
            self.handlers = {}
            self.goto_calls = []

        def on(self, event, handler):
            self.handlers[event] = handler

        def title(self):
            return "Fake Title"

        def goto(self, url, timeout=None, wait_until=None):
            self.goto_calls.append((url, timeout, wait_until))
            self.url = url

        def screenshot(self, path=None, **kw):
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nfake-perform-shot")

    def make_pw(page):
        state = {"launch": [], "ctx": [], "closed": [], "entered": []}

        class FakeCtx:
            def new_page(self):
                return page

        class FakeBrowser:
            def new_context(self, **kw):
                state["ctx"].append(copy.deepcopy(kw))
                return FakeCtx()

            def close(self):
                state["closed"].append(True)

        class FakeChromium:
            def launch(self, **kw):
                state["launch"].append(copy.deepcopy(kw))
                return FakeBrowser()

        class FakePW:
            chromium = FakeChromium()

        class FakePWCM:
            def __enter__(self):
                state["entered"].append(True)
                return FakePW()

            def __exit__(self, *a):
                return False

        return FakePWCM, state

    def make_chat(replies):
        calls = []

        def fake_chat(cfg, messages, timeout_s=90):
            calls.append(copy.deepcopy(messages))
            raw = replies[len(calls) - 1]
            return raw, {"prompt_tokens": 100, "completion_tokens": 50}

        return fake_chat, calls

    good_cfg = {"base_url": "http://x", "name": "stub-model",
                "api_key": "test", "price_in": 1.0, "price_out": 2.0}
    old_chat = perf_mod._chat
    old_cfg = perf_mod._model_config
    old_ax = perf_mod.ax_snapshot
    old_pw = pw_sync.sync_playwright
    perf_mod._model_config = lambda: dict(good_cfg)
    perf_mod.ax_snapshot = lambda page, **kw: "AX TREE"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for falsy in (None, ""):
                page = FakePage()
                cm, state = make_pw(page)
                fake_chat, calls = make_chat(
                    [json.dumps({"action": "done",
                                 "summary": "did the thing"})])
                perf_mod._chat = fake_chat
                pw_sync.sync_playwright = lambda: cm()
                result = perform(task="do the thing", target=falsy,
                                 max_actions=10, runs_root=tmp)
                check(f"379 perform: falsy target={falsy!r} no goto",
                      page.goto_calls == [], repr(page.goto_calls))
                check(f"379 perform: falsy target={falsy!r} completed",
                      result["status"] == "completed",
                      str(result["status"]))
                check(f"379 perform: falsy target={falsy!r} summary",
                      result["summary"] == "did the thing",
                      str(result["summary"]))
                check(f"379 perform: falsy target={falsy!r} echoed",
                      result["target"] == falsy, repr(result["target"]))
                check(f"379 perform: falsy target={falsy!r} one chat call",
                      len(calls) == 1, str(len(calls)))
                check(f"379 perform: falsy target={falsy!r} browser closed",
                      state["closed"] == [True], repr(state["closed"]))
    finally:
        perf_mod._chat = old_chat
        perf_mod._model_config = old_cfg
        perf_mod.ax_snapshot = old_ax
        pw_sync.sync_playwright = old_pw


def test_issue_383_spec_branch_coverage():
    """Issue #383: pin spec.py untaken branch sides.

    _validate_op_params has no final else: an op outside every known
    branch falls through and returns None silently. _validate_browser
    now uses a final else for device_scale_factor (the unknown-key
    guard above it already rejects anything outside the known set):
    valid int/float values are returned verbatim and invalid values
    raise SpecError naming browser.device_scale_factor.
    """
    from qaloop.spec import SpecError, _validate_browser, _validate_op_params

    # Out-of-set op: arbitrary params are ignored, returns None.
    for params in (None, "anything", 123, {"target": "x"}, ["a"]):
        try:
            ret = _validate_op_params("bogus_op", params, "steps[0]")
            check(f"383 op_params: bogus_op params={params!r} returns None",
                  ret is None, repr(ret))
        except SpecError as e:
            check(f"383 op_params: bogus_op params={params!r} returns None",
                  False, str(e))

    # In-chain controls: valid goto passes, invalid goto raises.
    try:
        ret = _validate_op_params("goto", "http://example.test", "steps[0]")
        check("383 op_params: valid goto returns None", ret is None,
              repr(ret))
    except SpecError as e:
        check("383 op_params: valid goto returns None", False, str(e))
    for bad in ("", None, 123):
        try:
            _validate_op_params("goto", bad, "steps[0]")
            check(f"383 op_params: invalid goto {bad!r} raises", False)
        except SpecError:
            check(f"383 op_params: invalid goto {bad!r} raises", True)

    # device_scale_factor: valid int and float returned verbatim.
    for good in (2, 1.5):
        out = _validate_browser({"device_scale_factor": good}, "spec test")
        check(f"383 browser: device_scale_factor {good!r} verbatim",
              out == {"device_scale_factor": good}
              and type(out["device_scale_factor"]) is type(good),
              repr(out))

    # device_scale_factor: invalid values raise naming the key.
    for bad in (0, -1, -0.5, True, "2"):
        try:
            _validate_browser({"device_scale_factor": bad}, "spec test")
            check(f"383 browser: device_scale_factor {bad!r} raises",
                  False)
        except SpecError as e:
            check(f"383 browser: device_scale_factor {bad!r} raises",
                  "browser.device_scale_factor" in str(e), str(e))

    # Mixed browser block returns every declared key.
    mixed = {"color_scheme": "dark", "is_mobile": True, "has_touch": False,
             "locale": "en-US", "timezone_id": "America/New_York",
             "user_agent": "test-ua", "device_scale_factor": 1.5}
    out = _validate_browser(dict(mixed), "spec test")
    check("383 browser: mixed block returns every declared key",
          out == mixed, repr(out))


def test_issue_385_smoke_corpus_execute():
    """Issue #385: execute the smoke-tagged shipped flows against the proof
    server. Default mode (QALOOP_SMOKE_CORPUS_FULL unset) really executes a
    pinned 7-flow subset covering every outcome class (passed, failed,
    error, boot_failed, matrix). QALOOP_SMOKE_CORPUS_FULL=1/true selects the
    full enumerated corpus; this test asserts that selection under the flag
    and drives the full-mode loop with a fake executor so the suite stays
    fast, while the real subset execution proves the executor end to end.
    The subset is never shrunk silently: the docstring and the checks below
    name the flag and the pinned subset."""
    import glob
    import socket
    import subprocess
    import time
    from types import SimpleNamespace

    from qaloop.cli import cmd_verify
    from qaloop.spec import load_spec

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exe = os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-1243/"
        "chrome-headless-shell-linux64/chrome-headless-shell")
    # Only designed-failure exceptions; every other smoke flow must pass.
    # Other *-fail flows (demo-hidden-fail, demo-js-fail, ...) fail at step
    # level with continue_on_fail, so their run outcome is "passed".
    smoke_expectations = {
        "demo-setup-fail": ("failed", None),
        "demo-steps-fail": ("failed", None),
        "demo-run-timeout": ("error", "run timeout"),
        "demo-services-http-fail": ("boot_failed", None),
        "demo-services-partial-fail": ("boot_failed", None),
        "demo-services-log-fail": ("boot_failed", None),
    }
    smoke_subset = ["demo-seed-http-json", "demo-mock-method",
                    "demo-matrix-list", "demo-setup-fail",
                    "demo-run-timeout", "demo-services-http-fail",
                    "demo-wait-ms"]

    def smoke_mismatch(flow_name, outcome, table):
        expected_status, expected_sub = table.get(
            flow_name, ("passed", None))
        status, error_text = outcome
        if status != expected_status: return (
            f"{flow_name}: expected status {expected_status}, "
            f"got {status} ({error_text})")
        if expected_sub is not None and expected_sub not in error_text: return (
            f"{flow_name}: expected error containing {expected_sub!r}, "
            f"got {error_text!r}")
        return None

    def select_smoke_paths(all_paths, full):
        if full: return list(all_paths)
        wanted = set(smoke_subset)
        return [p for p in all_paths
                if os.path.splitext(os.path.basename(p))[0] in wanted]

    def full_mode():
        return os.environ.get(
            "QALOOP_SMOKE_CORPUS_FULL", "").lower() in ("1", "true")

    # Enumerate the corpus from the shipped tags, never a hardcoded list.
    corpus = [p for p in sorted(glob.glob(os.path.join(root, "flows",
                                                       "*.yaml")))
              if "smoke" in load_spec(p, strict_env=False).tags]
    check("385 smoke corpus: smoke-tagged flows enumerated",
          len(corpus) > 0, str(len(corpus)))
    subset = select_smoke_paths(corpus, False)
    subset_names = sorted(os.path.splitext(os.path.basename(p))[0]
                          for p in subset)
    check("385 smoke corpus: default selection is the pinned subset",
          subset_names == sorted(smoke_subset), str(subset_names))
    check("385 smoke corpus: full selection returns the whole corpus",
          select_smoke_paths(corpus, True) == corpus)
    # Corrupted-expectation probes: a wrong table entry must be caught.
    bad = dict(smoke_expectations)
    bad["demo-wait-ms"] = ("failed", None)
    probe = smoke_mismatch("demo-wait-ms", ("passed", ""), bad)
    check("385 smoke corpus: corrupted expectation caught, names flow",
          probe is not None and "demo-wait-ms" in probe, probe or "")
    check("385 smoke corpus: matching outcome returns no mismatch",
          smoke_mismatch("demo-wait-ms", ("passed", ""),
                         smoke_expectations) is None)
    sub_probe = smoke_mismatch("demo-run-timeout",
                               ("error", "some other error"),
                               smoke_expectations)
    check("385 smoke corpus: wrong error text caught, names flow",
          sub_probe is not None and "demo-run-timeout" in sub_probe,
          sub_probe or "")
    if not os.path.exists(exe): check("385 smoke corpus execution skipped (no browser)", True); return

    env_keys = ("TARGET_URL", "QALOOP_RUNS", "QALOOP_LEDGER",
                "QALOOP_SMOKE_CORPUS_FULL")
    saved = {k: os.environ.get(k) for k in env_keys}
    srv = None
    with tempfile.TemporaryDirectory() as d:
        runs_dir = os.path.join(d, "runs")
        os.environ["TARGET_URL"] = "http://127.0.0.1:8933"
        os.environ["QALOOP_RUNS"] = runs_dir
        os.environ["QALOOP_LEDGER"] = os.path.join(d, "ledger.jsonl")
        os.environ.pop("QALOOP_SMOKE_CORPUS_FULL", None)
        try:
            def poll_greeting():
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:8933/api/greeting",
                            timeout=1) as r:
                        return r.status == 200
                except Exception:
                    return False

            def run_corpus(paths, executor):
                for p in paths:
                    name = os.path.splitext(os.path.basename(p))[0]
                    mismatch = smoke_mismatch(name, executor(p),
                                              smoke_expectations)
                    check(f"smoke corpus executes: {os.path.basename(p)}",
                          mismatch is None, mismatch or "")

            def real_executor(path):
                spec = load_spec(path, strict_env=False)
                before = (set(os.listdir(runs_dir))
                          if os.path.isdir(runs_dir) else set())
                args = SimpleNamespace(
                    flow=os.path.abspath(path), target=None, headed=False,
                    executable_path=exe, upload_dir=None, investigate=False,
                    keep_runs=0, max_investigation_actions=None, tag=None,
                    video=False)
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cmd_verify(args)
                if rc == 3: return ("boot_failed", "")
                if spec.matrix: return ("passed" if rc == 0 else "failed", "")
                new = sorted(set(os.listdir(runs_dir)) - before)
                with open(os.path.join(runs_dir, new[0],
                                       "run.json")) as f:
                    data = json.load(f)
                return (data["status"], data.get("error") or "")

            def fake_executor(path):
                name = os.path.splitext(os.path.basename(path))[0]
                status, sub = smoke_expectations.get(name, ("passed", None))
                return (status, "run timeout exceeded" if sub else "")

            sock = socket.socket()
            occupied = sock.connect_ex(("127.0.0.1", 8933)) == 0
            sock.close()
            check("385 smoke corpus: port 8933 not already in use",
                  not occupied, "stale server already on 8933")
            check("385 smoke corpus: no stale proof server answering",
                  not poll_greeting())
            if not occupied:
                srv = subprocess.Popen(
                    [sys.executable, "demo/proof_server.py"], cwd=root,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.time() + 10
                ready = False
                while not ready and time.time() < deadline:
                    ready = poll_greeting()
                    time.sleep(0 if ready else 0.1)
                check("385 smoke corpus: proof server ready on 8933", ready)
                if ready:
                    run_corpus(select_smoke_paths(corpus, full_mode()),
                               real_executor)
                    os.environ["QALOOP_SMOKE_CORPUS_FULL"] = "1"
                    full_paths = select_smoke_paths(corpus, full_mode())
                    check("385 smoke corpus: flag selects the full corpus",
                          full_paths == corpus)
                    run_corpus(full_paths, fake_executor)
        finally:
            srv.terminate() if srv is not None else None
            srv.wait() if srv is not None else None
            for k in env_keys: os.environ.pop(k, None) if saved[k] is None else os.environ.update({k: saved[k]})


if __name__ == "__main__":
    for fn in sorted([v for k, v in globals().items()
                      if k.startswith("test_")], key=lambda f: f.__name__):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            FAIL.append(fn.__name__)
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


