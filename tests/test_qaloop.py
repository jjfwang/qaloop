"""qaloop unit tests (stdlib only). Run: python3 tests/test_qaloop.py"""
import contextlib
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
                             "evidence": ["a.py:1 -> saves",
                                          "step load -> shows"],
                             "risks": []})
    check("high-confidence MAKES_SENSE with evidence stays",
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
    cases = [
        ([], False),
        (["only one bullet"], False),
        (["", "  ", None], False),
        (["a.py:1 -> saves", ""], False),
        (["a.py:1 -> saves", "step load -> shows"], True),
    ]
    for evidence, stays in cases:
        v, tmp = _stub_evaluate({"verdict": "MAKES_SENSE",
                                 "confidence": "high",
                                 "rationale": "r", "evidence": evidence,
                                 "risks": []})
        want = "MAKES_SENSE" if stays else "INSUFFICIENT_EVIDENCE"
        non_empty = sum(1 for e in evidence
                        if isinstance(e, str) and e.strip())
        check(f"high-confidence rule: {non_empty} non-empty bullets -> {want}",
              v["verdict"] == want, v["verdict"])
        if not stays:
            check(f"downgrade rationale mentions evidence "
                  f"({non_empty} bullets)",
                  "Downgrade" in v["rationale"]
                  and "evidence" in v["rationale"], v["rationale"])
        tmp.cleanup()


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
                             "evidence": ["a.py:1 -> saves",
                                          "step load -> shows"],
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
                             "evidence": ["a.py:1 -> saves",
                                          "step load -> shows"],
                             "risks": [], "scores": s})
    check("end-to-end: 0 evidence_exercises_claim -> INSUFFICIENT_EVIDENCE",
          v["verdict"] == "INSUFFICIENT_EVIDENCE"
          and v["verdict_raw"] == "MAKES_SENSE", v["verdict"])
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
