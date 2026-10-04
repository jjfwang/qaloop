# qaloop flow-spec format (v1)

A flow spec is a YAML document describing one deterministic browser flow:
what to set up, what to do, and what must be true afterwards. The
deterministic runner executes it with zero LLM cost; the agentic
investigator is only summoned when a step fails.

```yaml
version: 1
name: companion-detail-tabs
description: "CF-01 detail screen: tabs switch panels"
target: ${TARGET_URL}            # $VAR / ${VAR} expanded; missing var = error

viewport: {width: 1280, height: 800}

setup:                          # abort the run if any setup step fails
  - name: known state
    seed: {js: "localStorage.clear()"}
  - goto: /companions/cf01

steps:
  - name: open needs tab
    click: 'button[aria-label="需求"]'
    expect:
      visible: "#needs-panel"
      text_contains: {selector: "#needs-panel h2", text: "需求"}

  - name: personality tab
    click: "#tab-personality"
    expect:
      visible: "#personality-panel"
      hidden: "#needs-panel"

  - name: journal affordance is intentionally unbound (OD-P36)
    click: "#write-log"
    expect:
      noop: true                # documents intentional no-ops; not a bug
      console_clean: true

teardown:                       # best-effort, failures don't fail the run
  - seed: {js: "localStorage.clear()"}

artifacts:
  screenshot: per-step         # per-step | on-failure | none
  ax_snapshot: on-failure      # accessibility-tree dump (text, cheap)
  trace: true                  # Playwright trace.zip

timeouts: {step_ms: 15000, run_ms: 300000}

investigate_on_failure: true
max_investigation_actions: 20

services:                       # boot real services before the flow runs
  - name: api
    command: "python3 app.py --port 8000"
    cwd: ../myapp               # optional; relative to the flow file
    env: {PORT: "8000"}         # optional extra env for the command
    wait: {http: "http://127.0.0.1:8000/health"}  # exactly one of:
                                # {port: 8000} | {http: <url>} | {log_contains: "ready"}
    timeout_s: 60               # optional, default 90
    url: "http://127.0.0.1:8000/"  # optional; default is the wait URL's
                                # origin (for http) or http://127.0.0.1:<port>/
```

Services are booted before `verify`/`baselines` run, then torn down (in
reverse order) even when the flow fails. Each service exports
`QALOOP_SERVICE_<NAME>_URL` (and `QALOOP_SERVICE_<NAME>_PORT` when a port
is known), so `target: ${QALOOP_SERVICE_API_URL}` resolves after boot.
Service stdout/stderr are copied to `<run_dir>/services/<name>.log`.
If a service fails to boot, the run aborts with `SERVICE BOOT FAILED`
and previously booted services are stopped.

## Action ops (one per step; a step may also carry `expect`)

| op | params | notes |
|---|---|---|
| `goto` | URL string | `/path` resolves against `target` |
| `click` / `dblclick` | selector | CSS, `text=`, `role=` all work |
| `fill` | `{target, text}` | clears first |
| `press` | `{target, key}` | e.g. `{target: "#q", key: "Enter"}` |
| `check` / `uncheck` | selector | checkboxes |
| `select` | `{target, value}` | `<select>` value |
| `wait` | `{target?, state?, text?, timeout_ms?}` | two forms — with `target`: state visible\|hidden\|attached\|detached (`text` waits for the selector to contain the text); without `target`: state load\|domcontentloaded\|networkidle (page load state, default load) |
| `wait_ms` | int | fixed pause (prefer `wait`) |
| `reload` / `back` | `true` | navigation |
| `seed` | `{http: {url, method?, json?}}` or `{js: "..."}` | test-data / state setup |
| `mock` | `{url, json?|body?|path?, status?, method?, headers?, times?, delay_ms?}` | register a network mock before navigation (see below) |
| `script` | `{js: "..."}` | `page.evaluate`; result ignored |

Step meta keys: `name`, `expect`, `continue_on_fail` (default false —
a failed step aborts the run), `timeout_ms` (overrides `timeouts.step_ms`),
`retry` (default 0 — run once; integer ≥ 0; when a step fails it is re-run
up to `retry` more times, first success wins; attempts run back-to-back
with no delay; out of scope: cross-step retry, retrying setup-phase mocks).

### `mock` — deterministic data without a backend

`mock` registers a Playwright route before the requests are made, so it
belongs in `setup` (or at least before the `goto` that triggers the
request). Exactly one of `json`, `body`, or `path` must be given.

```yaml
setup:
  - name: companions API returns two pets
    mock:
      url: "**/api/companions"   # URL glob
      method: GET                 # optional; uppercase
      status: 200                 # optional, default 200
      json: {companions: [{id: c1, name: 阿岩}, {id: c2, name: 小雪}]}
      times: 3                    # optional: mock only the first N hits, then passthrough
```

Later requests to the same URL fall through to the real network, so flows
can mix mocked data with real endpoints.

`delay_ms` (optional int ≥ 0, default 0) delays the mocked response by exactly
that many milliseconds — the route fires on the first request, the response
is just held back. The hold does not freeze the automation: the handler
yields back to the browser driver while waiting, so mid-flight assertions
keep running during the delay. With 0 the mock responds instantly (current
behavior, no timing change). With a delay, flows can deterministically assert
loading states: show the spinner or skeleton on click, assert the indicator
`visible` while the delayed response is in flight, then `wait` for it to be
hidden and assert the settled content with `text_contains`.

`mock_calls` (assertion) counts how often each registered mock actually
served a request, keyed by the mock's `url` pattern — e.g. after the flow
above triggers one fetch, `expect: {mock_calls: {url: "**/api/companions", equals: 1}}`
passes. Only *served* hits count: requests that fell through on a method
mismatch, or that arrived after `times` was exceeded, do not count. A URL
with no registered mock — or no hits yet — evaluates as 0, so
`mock_calls: {url: "**/api/x", equals: 0}` asserts the request was never made.

## Assertions (`expect:` mapping)

| assertion | params |
|---|---|
| `visible` / `hidden` | selector |
| `text_contains` | `{selector, text}` |
| `text_matches` | `{selector, pattern}` (regex) |
| `count` | `{selector, equals: n}` / `{selector, gte: n}` / `{selector, lte: n}` |
| `mock_calls` | `{url, equals: n}` / `{url, gte: n}` / `{url, lte: n}` — how often a mocked route was served (see `mock`) |
| `network_calls` | `{url, equals: n}` / `{url, gte: n}` / `{url, lte: n}` / `{url, status: {equals: 200}, equals: n}` — how many entries in `network.jsonl` whose url contains the given substring, optionally filtered by response `status` (`equals`/`gte`/`lte`; never matches `status: null` failed requests) (see `network.jsonl`) |
| `url_contains` / `title_contains` | string |
| `noop` | `true` — documents an intentionally dead control |
| `console_clean` | `true` — no new console/page errors since the step started |
| `js` | `{script, contains}` — evaluate JS, output must contain text |
| `screenshot_matches` | `{baseline, selector?, max_diff?}` — visual pinning (see below) |
| `ax` | `{role, name?, state?}` — accessibility-contract assertion (see below) |

`text_contains`, `text_matches`, `url_contains`, and `title_contains` all poll
until the substring appears or the step deadline expires, because
async-rendered state races a one-shot read: element text is re-read via
`text_content` (after `wait_for_selector` attaches), and `page.url` /
`page.title()` are re-read directly, every 0.15s against
`min(step.timeout_ms or 15000, 8000)`. On timeout the failure detail names the
final observed value, not the desired substring alone.

### `screenshot_matches` — visual UX pinning

Takes a screenshot (full page, or an element via `selector`) and compares it
against a checked-in baseline with a normalized RMS pixel difference
(0.0 = identical). `max_diff` defaults to 0.02 (2% RMS). Baselines resolve
relative to the flow file's directory — conventionally `baselines/`.

```yaml
expect:
  - screenshot_matches: {baseline: "baselines/companion-card.png", max_diff: 0.03}
```

Generate baselines explicitly, never by accident:

```bash
python3 -m qaloop.cli baselines flows/my-flow.yaml
```

A missing baseline fails the assertion with a pointer to this command;
`qaloop verify` never writes baselines.

### Diff artifacts on mismatch

A failing `screenshot_matches` comparison (baseline exists, RMS diff above
`max_diff`) additionally writes a diff-highlight image next to the actual
screenshot: `steps/assert-<phase>-<idx>-diff.png` in the run dir. It is the
actual screenshot with every drifted pixel highlighted red, so you can see
at a glance what changed. The assertion detail names the artifact, e.g.
`rms_diff=0.0410 max_diff=0.0200 diff=steps/assert-steps-02-diff.png`.

The artifact is written only on mismatch: green runs produce no diff files,
and baseline-update mode never writes one (it saves the baseline instead).
If the diff image cannot be written, the run continues and the detail says
so — a diff-write failure never fails the run by itself.

### `ax` — user-centric accessibility assertions

Asserts against the accessibility tree via Playwright role locators, i.e.
what a screen-reader user (and the agent) actually sees:

```yaml
expect:
  - ax: {role: button, name: "Save profile"}        # visible (default)
  - ax: {role: dialog, state: hidden}               # state: visible|hidden|attached
```

Prefer `ax` over CSS selectors when the contract is "the user can perceive
and operate this control", not "this class exists in the DOM".

## Conventions

- Write the spec alongside the feature; the implementer authors its own
  acceptance flow. Encode known limitations (`noop`, OD references in
  `name`) so the verifier doesn't file bugs about intentional behavior.
- Prefer `wait` with a state over `wait_ms`.
- Keep flows to one user goal each; 5–25 steps is the sweet spot.

## Run artifacts

Every `verify`/`worker` run writes into `runs/<id>/`:

- `run.json` — the full `RunResult` (steps, assertions, console/network errors).
- `REPORT.md` — human-readable report card.
- `junit.xml` — JUnit XML for CI ingestion (one `<testsuite>` per flow,
  one `<testcase>` per step; failed steps carry a `<failure>` element with
  the step error text, skipped steps a `<skipped/>` element).
- `network.jsonl` — one JSON line per observed network response, in event
  order. Line schema: `{ts, method, url, status, ms}`; failed requests
  (no response event) get `status: null, ms: null, failure: <reason>` instead.
  `url` is capped at 500 chars; response bodies are never recorded
  (privacy by default).

  `network_calls` (assertion) counts entries in this log whose `url` contains
  the given url substring — including mock-served responses, which appear in
  the log as observed responses, and failed requests (`status: null`), which
  count as calls unless a `status` filter is given. An optional `status`
  sub-filter `{equals: 200}` / `{gte: 200}` / `{lte: 399}` narrows the count
  to entries whose response status satisfies it; it never matches
  `status: null` entries. No-entry urls evaluate as 0, so
  `network_calls: {url: "/demo/", equals: 0}` asserts the page made no
  matching request. Matching is a plain substring test on the url; matching
  on request bodies is out of scope.

Retention: `verify` and `worker` accept `--keep-runs N` (or the
`QALOOP_KEEP_RUNS` env var; the flag wins when both are set). After the
report is written, qaloop prunes the runs root so at most the N newest run
directories remain, deleting the oldest first (run ids are
`YYYYMMDDTHHMMSSZ`-prefixed, so name order is chronological). The run that
just finished is never pruned. The knob is opt-in: the default is 0, which
keeps everything. Negative values are rejected with an error (exit 2).
