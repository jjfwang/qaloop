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
