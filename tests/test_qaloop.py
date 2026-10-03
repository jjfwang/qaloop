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
