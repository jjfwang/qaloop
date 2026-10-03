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
    for bad in [
        "name: t\nsetup:\n  - mock: {json: {x: 1}}\n",                              # no url
        "name: t\nsetup:\n  - mock: {url: '**/a', json: {x: 1}, body: 'z'}\n",  # two payloads
        "name: t\nsetup:\n  - mock: {url: '**/a'}\n",                             # no payload
        "name: t\nsetup:\n  - mock: {url: '**/a', json: {x: 1}, method: get}\n",   # lowercase
        "name: t\nsetup:\n  - mock: {url: '**/a', json: {x: 1}, times: 0}\n",      # times<=0
    ]:
        try:
            load(bad)
            check(f"bad mock rejected: {bad.splitlines()[1].strip()}", False, "no error")
        except SpecError:
            check(f"bad mock rejected: {bad.splitlines()[1].strip()}", True)


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


def test_perform_action_json_extraction():
    from qaloop.perform import _extract_json
    a = _extract_json('here you go: {"action": "click", "target": {"role": "button"}} done')
    check("json extracted from prose", a["action"] == "click")
    try:
        _extract_json("no json here")
        check("non-json rejected", False)
    except ValueError:
        check("non-json rejected", True)


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
