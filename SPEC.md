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

browser:                         # optional; only declared keys reach
  color_scheme: dark             # dark | light | no-preference
  is_mobile: true                # real bool
  has_touch: true                # real bool
  locale: en-US                  # string, e.g. navigator.language
  timezone_id: America/New_York  # string, IANA timezone
  user_agent: "Mozilla/5.0 ..."  # string
  device_scale_factor: 2         # number > 0 (int or float)

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

The top-level key set is closed (issue #296): a flow may only use
`version`, `name`, `description`, `target`, `viewport`, `artifacts`,
`timeouts`, `record_bodies`, `video`, `tags`, `browser`, `setup`,
`steps`, `teardown`, `investigate_on_failure`,
`max_investigation_actions`, `services`, and `matrix`. Any other
top-level key is a load-time error naming the key — a typo such as
`servces:` fails fast instead of being silently ignored while the flow
runs with defaults. The mapping-valued keys are closed too:
`timeouts` allows only `step_ms` and `run_ms`; `artifacts` allows only
`screenshot`, `ax_snapshot`, and `trace`; `viewport` allows only
`width` and `height`. (`browser` likewise rejects unknown subkeys.)

## Action ops (one per step; a step may also carry `expect`)

| op | params | notes |
|---|---|---|
| `goto` | URL string | `/path` resolves against `target` |
| `click` / `dblclick` | selector | CSS, `text=`, `role=` all work |
| `fill` | `{target, text}` | clears first |
| `press` | `{target, key}` | e.g. `{target: "#q", key: "Enter"}` |
| `type` | `{target, text, delay_ms?}` | keystroke-by-keystroke typing via `locator.press_sequentially` — fires keydown/keypress/input events per character, so debounced search and autocomplete can be exercised; `delay_ms` is the pause in ms between keystrokes |
| `focus` | `{target}` | `locator.focus()` — focuses an element with no other interaction |
| `hover` | `{target}` | mouse hover on the selector — drives CSS `:hover` states, tooltips, and hover menus |
| `scroll` | `{target? , direction?: up\|down\|left\|right, pixels?: N}` | with `target`: `locator.scroll_into_view_if_needed` (step timeout); without `target`: `mouse.wheel` — down/up scroll ±pixels vertically, left/right horizontally (default `pixels` 600); drives lazy/infinite-scroll content deterministically |
| `check` / `uncheck` | selector | checkboxes |
| `select` | `{target, value}` | `<select>` value |
| `wait` | `{target?, state?, text?, js?, response?, timeout_ms?}` | four disjoint forms — with `target`: state visible\|hidden\|attached\|detached (`text` waits for the selector to contain the text); without `target`: state load\|domcontentloaded\|networkidle (page load state, default load); `js`: poll a JS expression until truthy; `response`: wait for a network response whose URL contains the substring (`js`/`response` take no `target`/`state`/`text`, and never both at once — see below) |
| `wait_ms` | int | fixed pause (prefer `wait`) |
| `reload` / `back` | `true` | navigation |
| `seed` | `{http: {url, method?, json?, headers?}}` or `{js: "..."}` | test-data / state setup (see below) |
| `upload` | `{target, path}` | `locator.set_input_files` on a file input; `path` must be absolute and inside the declared upload dir (`verify --upload-dir` or `QALOOP_UPLOAD_DIR` env) — undeclared dir or violation fails the step |
| `drag` | `{target, to}` | drag-and-drop — `to` is a target selector or `{x: N, y: N}` page coordinates; drives drops and sliders |
| `mock` | `{url, json?|body?|path?, status?, method?, headers?, times?, delay_ms?}` | register a network mock before navigation (see below) |
| `script` | `{js: "..."}` | `page.evaluate`; result ignored |

Step meta keys: `name`, `expect`, `continue_on_fail` (default false —
a failed step aborts the run; steps phase only — `true` in setup or
teardown is a spec error: setup aborts on any failure and teardown is
best-effort, so the flag would be inert there), `timeout_ms` (overrides
`timeouts.step_ms`),
`retry` (default 0 — run once; integer ≥ 0; when a step fails it is re-run
up to `retry` more times, first success wins; attempts run back-to-back
with no delay by default — see `retry_delay_ms`; out of scope: cross-step
retry, retrying setup-phase mocks),
`retry_delay_ms` (default 0 — milliseconds to wait between retry attempts;
integer ≥ 0; `> 0` requires `retry > 0` in the steps phase — a delay with no
retries is a spec error; the delay counts toward the step's duration),
`on_dialog` (`accept`|`dismiss`, default `dismiss` — what the runner does
with a JS dialog raised while the step's action runs; see `dialog`),
`frame` (`<name | url substring>` — when present, selector resolution goes
through `page.frame(name=...)`/`page.frame(url=...)` instead of the main
frame; see below),
`screenshot` (`always`|`never`|`on-failure`, default none — per-step override
of the flow-level `artifacts.screenshot` policy: `always` captures the step
screenshot regardless of the flow policy, `never` suppresses it,
`on-failure` captures only if the step failed; when absent the flow policy
governs, so omitting the key is byte-identical to not having the override).

```yaml
  - name: always photograph this step
    screenshot: always                # even when artifacts.screenshot: on-failure
    click: "#buy"
  - name: skip the screenshot for this noisy step
    screenshot: never                 # even when artifacts.screenshot: per-step
    wait: "#spinner"
```

### `frame`: iframe/frame scoping for actions and assertions

A step may carry `frame: <name | url substring>` to run its selectors
inside an iframe instead of the main frame:

```yaml
- name: toggle the widget inside its iframe
  click: "#widget-toggle"
  frame: demo-frame
  expect:
    - text_contains: {selector: "#widget-state", text: "on"}
```

The runner resolves the value first with `page.frame(name=...)` (the
iframe's `name` attribute), then with `page.frame(url=...)` as a literal
URL substring, so `frame: iframe-content` matches a frame served from
`/demo/iframe-content.html`. Frames attach asynchronously (after the
parent page's DOMContentLoaded), so the lookup polls until the step's
`timeout_ms` expires — mirroring `wait_for_selector` — and only then does
a non-matching frame fail the step with
`RuntimeError: frame not found: '<frame>'` (fail-closed, no silent
main-frame fallback).

What applies: locator actions (`click`, `dblclick`, `fill`, `press`,
`type`, `focus`, `hover`, `scroll` with a target, `check`, `uncheck`,
`select`, `upload`, `drag`) and selector assertions (`visible`, `hidden`,
`text_contains`, `text_matches`, `count`, `ax`, `screenshot_matches`
with a `selector`, `wait` with a `target`).

What stays on the main page: `goto`, `reload`, `back`, `wait_ms`,
targetless/`js`/`response` waits, `seed`, `script`, `mock`, and the
page-level assertions (`url_contains`, `title_contains`, `console_clean`,
`js`, `dialog`, `mock_calls`, `network_calls`) — a `Frame` has no URL,
title, `evaluate`, or dialogs. `frame:` combined with `goto`, `seed`,
`mock`, or `script` is a spec error. The coordinate form of `drag` uses
`page.mouse`, which is viewport-relative and therefore not frame-scoped.

### `wait`: `js` and `response` forms

Two more `wait` forms for conditions no selector or load state can express:

```yaml
- name: data arrived before rendering
  click: "#load"
- wait: {response: "/api/waitdata", timeout_ms: 10000}
- wait: {js: "window.dataLoaded === true"}
- name: rendered content
  expect:
    - text_contains: {selector: "#out", text: "done"}
```

`js` polls the expression inside the page (via Playwright's
`wait_for_function`) until it evaluates to a truthy value, then the step
passes. Use it for app state the DOM doesn't expose yet — flags like
`window.dataLoaded`, values set by framework hydration, canvas/WebGL
readiness — anywhere a selector can't tell you "done".

`response` waits for a network response whose URL contains the given
substring (via Playwright's `response` event — Playwright 1.63 has no
`wait_for_response` on `Page`; the event has the same semantics). The
predicate matches the URL only, so `"/api/waitdata"` also matches
`"/api/waitdata?fresh=1"`.

Headers-vs-body caveat: the `response` event fires when the response
**headers** arrive (TTFB), not when the body is fully received. The JS
may therefore not have the payload yet when the step passes — this is
the run-55 TTFB-vs-`responseEnd` lesson encoded as a contract. When body
content matters, follow the `response` wait with the `js` form
(`window.dataLoaded === true` set in the fetch's `.then`, or directly
with `text_contains` on the rendered element), which polls until the
page actually shows it.

`timeout_ms` (optional non-negative int) applies to all four `wait`
forms; when absent the step timeout applies.

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

### `seed` — test-data setup via HTTP or page JS

`seed` with `http` issues a real HTTP request (default `method` POST with a
`Content-Type: application/json` header; `json` is sent as the body).
`headers` (optional, a dict of string to string) is merged over the default
headers — user values win, so `headers: {Content-Type: text/plain}` overrides
the default and `headers: {Authorization: "Bearer ..."}` adds auth.

```yaml
setup:
  - name: create a test user
    seed:
      http:
        url: http://localhost:8000/users
        json: {name: test-user}
        headers: {X-Tenant: t1}   # optional; merged over the defaults
```

The response status is recorded structurally in run.json as the step's
`seed_status` (null for `js` seeds and non-seed steps). A 4xx/5xx response
fails the step fast with `seed request failed: HTTP <status>: ...` — the
status stays on the step record rather than living only in the error text.

## Assertions (`expect:` mapping)

| assertion | params |
|---|---|
| `visible` / `hidden` | selector |
| `text_contains` | `{selector, text}` |
| `text_matches` | `{selector, pattern}` (regex) |
| `count` | `{selector, equals: n}` / `{selector, gte: n}` / `{selector, lte: n}` |
| `mock_calls` | `{url, equals: n}` / `{url, gte: n}` / `{url, lte: n}` — how often a mocked route was served (see `mock`) |
| `network_calls` | `{url, equals: n}` / `{url, gte: n}` / `{url, lte: n}` / `{url, status: {equals: 200}, equals: n}` — how many entries in `network.jsonl` whose url contains the given substring, optionally filtered by response `status` (`equals`/`gte`/`lte`; never matches `status: null` failed requests) (see `network.jsonl`) |
| `request_body` | `{url, contains: s}` / `{url, equals: s}` — at least one `network.jsonl` entry whose url contains the given substring carries a recorded request body matching `contains` (substring) / `equals` (exact). Requires the flow to opt in with top-level `record_bodies: true` (see `network.jsonl`); fails when no matching entry has a recorded body |
| `url_contains` / `title_contains` | string |
| `noop` | `true` — documents an intentionally dead control |
| `console_clean` | `true` — no new console/page errors since the step started |
| `js` | `{script, contains}` — evaluate JS, polls until output contains text |
| `screenshot_matches` | `{baseline, selector?, max_diff?}` — visual pinning (see below) |
| `ax` | `{role, name?, state?}` — accessibility-contract assertion (see below) |
| `dialog` | `{type?, text?}` — a JS dialog appeared since the step started (see below) |
| `attribute` | `{selector, name, equals: v}` / `{selector, name, contains: s}` — element attribute value (see below) |
| `value` | `{selector, equals: v}` — live value of an input after `fill`/`type` |
| `focused` | `{selector}` — `document.activeElement` is the element |
| `cookie` | `{name, equals: v}` / `{name, contains: s}` / `{name}` — cookie value, or presence (see below) |
| `storage` | `{kind: local|session, key, equals?: v}` — web-storage value, or presence (see below) |
| `perf` | `{lcp_ms: {lte: n}, cls: {lte: n}, inp_ms: {lte: n}}` — Web Vitals performance budgets in ms (LCP, INP) / shift score (CLS) (see below) |
| `a11y` | `{violations: {lte: n}, rules?: [...]}` — axe-core accessibility scan (see below) |

`text_contains`, `text_matches`, `url_contains`, `title_contains`, `js`, `count`,
`attribute`, `value`, `focused`, `cookie`, `storage`, and `perf`
all poll until the value matches or the step deadline expires, because
async-rendered state races a one-shot read: element text is re-read via
`text_content` (after `wait_for_selector` attaches), `page.url` /
`page.title()` are re-read directly, `page.evaluate(script)` output is
re-read directly, locator `count()` is re-read
against the comparator (`equals` > `gte` > `lte`), the attribute is re-read via
`get_attribute`, the input value via `input_value`, and focus via
`document.activeElement === el` (Playwright has no `is_focused()`), the cookie
list via `context.cookies()`, the storage value via a `page.evaluate` read
of `getItem`, and the Web Vitals metrics via a fixed `page.evaluate` read of
the Performance API, every 0.15s against
`min(step.timeout_ms or 15000, 8000)`. On timeout the failure detail names the
final observed value, not the desired value alone.

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
`qaloop verify` never writes baselines. On a flow that declares
`matrix:`, `baselines` dispatches once per combination exactly like
`verify` (per-combination run dirs under `matrix/`, variables seeded
from the combination), saving each combination's baselines in
baseline-update mode; the exit code is 0 iff every combination passed.
`baselines` also accepts `--upload-dir <dir>` (env
`QALOOP_UPLOAD_DIR`) with the same semantics as `verify`: the flag
wins when both are set; when neither is declared, the runner is
fail-closed (issue #76).

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

### `a11y` — deterministic accessibility audit

Asserts against a one-shot [axe-core](https://github.com/dequelabs/axe-core)
scan of the page, i.e. a deterministic audit of WCAG violations — where
`ax` asks "can a screen-reader user reach this control?" (`ax` = the
user-centric accessibility *contract* via role locators), `a11y` asks
"does the page carry accessibility *violations*?" (`a11y` = the
violation audit).

```yaml
expect:
  - a11y: {violations: {lte: 0}}                              # no violations
  - a11y: {violations: {lte: 0}, rules: ["color-contrast"]}   # scoped scan
```

`violations` takes exactly `{lte: N}` — violation counts are upper
bounds, so `gte`/`equals` are rejected, and `N` must be a non-negative
number. `rules` (optional) is a non-empty list of axe rule ids
(e.g. `["color-contrast", "image-alt"]`); without it the scan runs
axe-core's default rule set.

The runner injects the vendored axe-core bundle idempotently (only when
`window.axe` is undefined), then calls `axe.run(document, {runOnly})`
once per step — `runOnly` is omitted when `rules` is not given. Unlike
the polling assertions this is a one-shot read: axe.run is seconds-heavy
per iteration, so it never polls. The scan is fail-closed: an injection
or scan error fails the assertion with the error named, never passes
silently. The failure detail names the violation count, the violating
rule ids, and the first two violation descriptions (truncated), e.g.
`violations=2 want <=0; ids=image-alt,button-name | image-alt: Images
must have alternate text | button-name: Buttons must have discernible
text`. Page level: reads the main page, not the step's `frame:` scope.

Vendoring decision: axe-core is bundled in-repo at
`qaloop/assets/axe.min.js` (axe-core 4.10.2, MPL-2.0; provenance in
`qaloop/assets/README.md`), never fetched at runtime, so scans stay
deterministic and offline-safe. The license header is intact at the top
of the bundled file.

### `dialog` — JS dialog assertions

Without a handler Playwright silently auto-dismisses `alert()`/`confirm()`/
`prompt()`, so a flow could pass while never noticing the dialog it was
supposed to verify. The runner now records every dialog (type, message) and
applies the step's `on_dialog` policy (`dismiss` by default, `accept` to take
the affirmative path — e.g. confirming a destructive action). The `dialog`
assertion matches against dialogs raised since the step started:

```yaml
- name: delete asks for confirmation
  click: "#delete-row"
  on_dialog: accept                       # take the confirm's OK path
  expect:
    - dialog: {type: confirm, text: "Delete this row?"}
    - hidden: "#row-1"
```

`type` is `alert`|`confirm`|`prompt`|`beforeunload`; `text` is a substring of
the dialog message. Either may be omitted to match any dialog. All dialogs
are listed in the run report and `run.json` under `dialogs`. Note: `prompt()`
is accepted with empty input — typed prompt input is out of scope.

A `beforeunload` dialog raised by a step's `goto` follows the same policy:
`accept` lets the navigation complete; `dismiss` cancels it, and the `goto`
counts as completed (Playwright's `net::ERR_ABORTED` for the blocked
navigation is not a step error) so the step's assertions judge where the
browser ended up — e.g. `dialog: {type: beforeunload}` plus a `url_contains`
for the page it stayed on.

### Element state — `attribute`, `value`, `focused`

Three assertions on an element's runtime state, for contracts like "the
control reflects its model after the interaction":

```yaml
expect:
  - attribute: {selector: "#submit", name: "aria-disabled", equals: "false"}
  - attribute: {selector: "#badge", name: "class", contains: "active"}
  - value: {selector: "#email", equals: "qa@example.com"}
  - focused: {selector: "#search"}
```

`attribute` needs `selector`, `name`, and exactly one of `equals` /
`contains`; `value` needs `selector` and `equals`; `focused` needs only
`selector`. `get_attribute` returning `None` (missing attribute) never
matches — not even `contains`. Like the other polling assertions these
re-read every 0.15s until the step deadline and the failure detail names
the final observed value (e.g. `#badge: class='chip idle' want contains 'active'`).
All three resolve inside the step's `frame:` scope like other selector
assertions.

### Cookies and web storage — `cookie`, `storage`

Two assertions on browser-level state, for contracts like "the login
request set the session cookie" or "consent was persisted":

```yaml
expect:
  - cookie: {name: "session", equals: "abc123"}
  - cookie: {name: "promo", contains: "summer"}
  - cookie: {name: "session"}                       # presence only
  - storage: {kind: local, key: "consent", equals: "accepted"}
  - storage: {kind: session, key: "flag"}           # presence only
```

`cookie` needs a non-empty `name` and at most one of `equals` /
`contains` (never both) as a string; omitting both asserts presence —
"the login cookie is set". `storage` needs `kind: local|session`, a
non-empty `key`, and an optional `equals` string; omitting `equals`
asserts the key exists. Both poll every 0.15s until the step deadline,
because cookies are often set by an async request and storage by a
delayed script write. A missing cookie or a missing key (`getItem`
returns `null`) never matches, not even with `contains`. The failure
detail names the final observed value (e.g. `cookie 'session'=None want
='abc123'`); the key is passed to `page.evaluate` as an argument, never
interpolated into the script. Storage reads the main page, not the
step's `frame:` scope: storage is keyed on the page origin.

### Web Vitals performance budgets — `perf`

Asserts that the page's Web Vitals metrics stay under the given budgets,
for contracts like "the hero image renders within the LCP budget" or
"the CTA responds within the INP budget":

```yaml
expect:
  - perf: {lcp_ms: {lte: 2500}, cls: {lte: 0.1}, inp_ms: {lte: 500}}
```

Supported metrics: `lcp_ms` (Largest Contentful Paint, ms), `cls`
(Cumulative Layout Shift, shift score), `inp_ms` (Interaction to Next
Paint, ms). Each metric takes exactly `{lte: N}` — budgets are upper
bounds, so `gte`/`equals` are rejected, and `N` must be a non-negative
number. At least one metric is required.

The runner measures via one fixed `page.evaluate` read of the
Performance API per poll iteration, returning all three metrics at once:
LCP is the last `largest-contentful-paint` entry's `renderTime ||
startTime` (`null` when no such entry was recorded); CLS is the sum of
`layout-shift` entry values with `!hadRecentInput` (`0` when none
recorded — a real reading); INP is the max `duration` of `first-input`
entries with an `interactionId` (`null` when none, i.e. no real
interaction happened yet). Two headless-shell realities shape this
read: LCP and `layout-shift` entries are delivered to observers but
not retained in the queryable `getEntriesByType` buffer, so the read
collects them with short-lived `PerformanceObserver`s
(`buffered: true`) — CLS is therefore the sum of the `layout-shift`
entry values delivered to that observer with `!hadRecentInput`; and
`event`
timing entries are only recorded for an observer registered *before*
the interaction — which an assertion cannot install after the fact —
so INP is approximated from the buffered `first-input` entry instead
of the full interaction set (documented limitation: `inp_ms` reflects
the first interaction, not the worst over the page lifetime). Each
requested metric is polled every 0.15s until a non-`null` reading is at
or below its budget or the step deadline expires, because LCP/CLS/INP
entries land asynchronously.
Fail-closed on missing data: a `null` reading never satisfies the
budget — an assertion on `inp_ms` fails on a page nobody interacted
with rather than passing vacuously. The failure detail names the final
observed value per metric (e.g. `lcp_ms=3200.5 want <=2500;
cls=0.0 want <=0.1`).

Note: `event` performance entries (the textbook INP source) are only
recorded for real user interactions seen by an observer registered
*before* the interaction. A demo page that asserts `inp_ms` must
therefore drive a real interaction (e.g. click a control) and give the
handler real work to do (a trivial handler may not even produce a
measurable entry); the assertion then reads the buffered `first-input`
entry for that interaction. Page level: reads the main page, not the
step's `frame:` scope.

### Request bodies — `request_body`

An assertion on what the page *sent*, for contracts like "the save
request carried the right payload":

```yaml
record_bodies: true          # top level; default false (privacy by default)
steps:
  - name: the save request carried the payload
    expect:
      - request_body: {url: "/api/save", contains: '"pet":"fluffy"'}
```

`request_body` needs a non-empty `url` (a substring matched against
`network.jsonl` entry urls, like `network_calls`) and exactly one of
`contains` / `equals` as a string. It passes iff at least one matching
entry carries a recorded body that matches: `contains` is a substring
test, `equals` is exact. The assertion is fail-closed — with no recorded
body on any matching entry it fails (the detail says `record_bodies:
true` is required), never silently passes. The `network_calls`
"matching on request bodies is out of scope" note stays valid:
`network_calls` still counts urls only; `request_body` is the separate
assertion for body matching.

## Flow composition: `include`

A step can inline a shared fragment file instead of repeating the same
steps across flows:

```yaml
steps:
  - name: log in as alice
    include: fragments/login.yaml
    with: {user: alice, password: s3cret}
```

A fragment is a YAML mapping with a required `steps:` list and optional
`name:`/`description:`. It carries no `setup:`, `teardown:`, `services:`,
or `target:` — those are rejected at load time, fail-closed, so nothing
is silently dropped.

Rules:

- The include path resolves **relative to the including file's
  directory** (nested includes resolve relative to their own fragment's
  directory — the same convention as screenshot baselines and service
  `cwd`s). A missing fragment file is a `SpecError`.
- An include step carries **only** `include`, `with`, and `name`
  (`name` is documentation, ignored). Combining `include` with an action
  op or `expect` is a `SpecError`.
- **Cycles** (a fragment including itself, directly or through others)
  are a `SpecError` naming the full cycle, e.g.
  `include cycle detected: flows/a.yaml -> flows/fragments/b.yaml ->
  flows/a.yaml`.
- `with:` substitution is **compose-time parameter substitution from
  `with:` only**: `${name}` and `$name` occurrences in the inlined
  fragment steps' string values are replaced with `str(value)`
  (`with:` keys are strings; values are str/int/float/bool scalars).
  Longest name wins, so `${username}` does not partially match
  `${user}`.
- `with:` is **lexically scoped per include level**: a parent's `with`
  does not leak into a nested include's fragment — the nested include
  declares its own `with`. Any `${var}` not named in `with:` is left
  untouched and goes through the flow's normal `$ENV` substitution
  (missing var = error), exactly as before.
- Inlined steps keep their own names and are re-indexed at the include
  position; includes expand in `setup`, `steps`, and `teardown`.
- Boundary: `${var}` here is load-time parameter substitution from
  `with:` only — **runtime capture via `store` is a separate feature**,
  described in the next section. Fragments cannot see runtime values.

### Flow variables — `store` and `${var}` interpolation

Steps can capture page state into run-scoped variables and reuse them
later in the run:

```yaml
steps:
  - name: create an item and capture its generated id
    click: "#create"
    store: {itemId: {js: "document.querySelector('.row').dataset.itemId"}}
  - name: delete the item using the captured id
    click: ".del[data-item-id='${itemId}']"
```

Rules:

- `store` is a step-level meta key (alongside `name`, `expect`,
  `timeout_ms`, ...): a mapping of variable name to
  `{js: <expression>}`. Variable names match `[A-Za-z_][A-Za-z0-9_]*`.
  Only the `js` source is supported; `text`/`attribute` capture is
  explicitly later and rejected at load (`SpecError`), fail-closed.
- The `js` expression is evaluated with `page.evaluate` **after** the
  step's action and assertions pass; `str(result)` is captured. A
  failed step captures nothing. Re-storing a name overwrites the
  previous value.
- The variable table is run-scoped: shared across `setup`, `steps`,
  and `teardown`, in execution order.
- Before each step, `${name}` (or `$name`) in the step's **params**
  is interpolated from the table — selectors, fill text, `js` source,
  and so on. `expect`, `store`'s `js`, and other keys are not
  interpolated.
- A stored name is exempt from load-time `$ENV` expansion: where a
  name is both an environment variable and a declared store variable,
  the runtime table wins.
- A `${name}` that is neither a declared store variable nor an
  environment variable is a `SpecError` at load time.
- A `${name}` referencing a declared variable with no earlier `store`
  step is a `SpecError` at load time (use-before-store, statically
  known — including a step referencing its own `store`, which is
  evaluated after the step).
- At runtime, referencing a variable that was never captured (e.g.
  the `store` step was skipped) fails the step with an error naming
  the variable.

### Matrix — data-driven flows with `matrix:`

A top-level `matrix:` runs the whole flow once per data combination
(issue #88). Each combination's variable map seeds the run-scoped
variable table from #87, so `${name}` works in every step without any
`store` step.

```yaml
matrix:
  user: [ada, bob]        # cross product: 2 x 2 = 4 combinations
  plan: [pro, free]
```

Two forms:

- **Map of name to value list** (above): expanded as the cross product
  in key insertion order — deterministic. Every value list must be
  non-empty; every value a scalar (string, number, boolean, null),
  stringified via `str()` exactly like `store` captures.
- **List of maps**: each map is one combination verbatim.
  ```yaml
  matrix:
    - {user: ada, plan: pro}
    - {user: bob, plan: free}
  ```

Rules:

- `matrix:` must be a non-empty list or map; names match
  `[A-Za-z_][A-Za-z0-9_]*`; a name also declared by a `store` step is a
  `SpecError` (a variable is seeded by the matrix or captured — never
  both).
- Matrix names count as **pre-initialized variables**: they survive
  load-time `$ENV` expansion everywhere (step params *and* `target:`),
  and they are exempt from the use-before-store static check — the
  matrix seeds them before the first step, and they are visible to all
  phases (`setup`, `steps`, `teardown`).
- A `${name}` in `target:` that matches a matrix name is interpolated
  from each combination's map at run time, so one flow can sweep
  multiple base URLs.
- `verify` creates one parent run dir with per-combination subdirs
  (`<run_dir>/matrix/<NN>-<combo-slug>/`), each with its own
  `run.json`/`REPORT.md`, plus a combined summary in the parent
  `REPORT.md` (combination count, per-combo PASS/FAIL, links to the
  subdirs). Exit code is 0 iff every combination passed. In matrix
  mode `--investigate` is not applied per combination.

### Tags — labeling flows with `tags:`

A top-level `tags:` labels a flow for batch selection (issue #89):

```yaml
tags: [smoke, login]
```

Rules:

- `tags:` is a list of non-empty strings matching
  `^[A-Za-z0-9][A-Za-z0-9_.-]*$` (no spaces, no duplicates); anything
  else is a `SpecError`. Flows without `tags:` default to `[]`.
- Tag matching is exact and case-sensitive.

### Batch verification — `verify <dir>`

`verify` accepts a directory of flow YAMLs (issue #89): every
`*.yaml`/`*.yml` in the directory (non-recursive, sorted by name) runs
in its own subdir `<run_dir>/flows/<stem>/`, each with the full
single-flow path (services boot/teardown, `run_flow`, `REPORT.md` +
`junit.xml`, per-flow ledger entry). A combined summary lands in the
parent `REPORT.md` (per-flow PASS/FAIL table with report links) plus a
machine-readable `batch.json` (`file`, `status`, `duration_ms`,
`report`).

- `--tag T` (repeatable) selects only flows carrying at least one of
  the tags (OR semantics, exact case-sensitive match). With a single
  flow file, `--tag` runs the flow iff it carries the tag, otherwise it
  exits 2 naming the missing tag.
- An empty selection (no flow files, or no tag match) exits 2 — a
  batch never silently passes.
- Exit code is 0 iff every flow passed, 1 if any flow failed. An
  INVALID SPEC or SERVICE BOOT FAILED for one flow records that flow
  as failed and the batch continues with the rest (batch resilience).
  This deviates from single-flow's exit 2/3 codes by design.
- `--investigate` is not applied per flow in batch mode (same carve-out
  as matrix mode). `--keep-runs` pruning happens once after the batch
  report, with the parent dir as current.
- A directory run uses the same per-flow code path as single `verify`:
  a flow with `matrix:` runs in matrix mode inside its own subdir, and
  the batch table reports its aggregate PASS/FAIL (the steps column
  shows combinations passed/total; per-combination detail stays in the
  flow's own REPORT.md).
- There is no `--all` flag — the directory positional covers it.

## Conventions

- Write the spec alongside the feature; the implementer authors its own
  acceptance flow. Encode known limitations (`noop`, OD references in
  `name`) so the verifier doesn't file bugs about intentional behavior.
- Prefer `wait` with a state over `wait_ms`.
- Keep flows to one user goal each; 5–25 steps is the sweet spot.

## `qaloop validate`: flow preflight

`python3 -m qaloop.cli validate <flow.yaml>` (the `qaloop validate`
spelling used elsewhere in this spec) light-parses a flow spec without
booting services, resolving env substitution, or launching a browser —
the fast preflight before `verify` or `baselines`. It takes a single flow
path (unlike `verify`, it does not accept a directory).

Semantics, verified against `qaloop/cli.py` (`cmd_validate`):

- The spec is loaded with `strict_env=False`, so env vars like
  `${TARGET_URL}` are NOT resolved at validate time — the VALID line
  shows the target exactly as written.
- The full structural validation from `qaloop.spec` runs: required `name`
  and `target`, step schema, and per-action argument checks.
- Output contract, exit 0: `VALID: <name> — <n> steps (setup X, steps Y,
  teardown Z), target <target>` — the dash is an em-dash (U+2014).
  Example: `VALID: demo-mock-ux — 9 steps (setup 2, steps 7, teardown 0),
  target ${TARGET_URL}`.
- Output contract, exit 1: `INVALID: <reason>` when `load_spec` raises
  `SpecError` (the reason names the spec path and the failing check,
  e.g. `spec flows/x.yaml: 'name' is required`). A missing or unreadable
  file is not a `SpecError` — it propagates as a Python traceback
  (still exit 1).
- Relationship to `verify`: validate checks structure only and never
  touches the network. `verify` re-validates at runtime against the real
  environment.

## `qaloop verify`: the primary run command

`python3 -m qaloop.cli verify <flow.yaml>` (the `qaloop verify`
spelling used elsewhere in this spec) boots the flow's declared
services, runs the steps against a target, and writes the run artifacts
into a fresh run dir. It is the framework's primary entry point. The
flow positional accepts a single flow YAML or a directory of flow
YAMLs; directory runs are covered under [Batch verification](#batch-verification--verify-dir).

Semantics, verified against `qaloop/cli.py` (`cmd_verify`):

### Flags

- `--target <url>`: overrides the spec's target for this run. Without
  the flag, the flow's declared target is used.
- `--tag T` (repeatable): only runs flows carrying the tag. OR
  semantics, exact case-sensitive match. With a single flow file that
  does not carry the tag, `verify` prints
  `SKIP: <flow> does not carry tag(s) [...] (flow tags: [...])` and
  exits 2. Batch-mode selection is documented under Batch verification.
- `--headed`: show the browser. Default is headless.
- `--upload-dir <dir>` (env `QALOOP_UPLOAD_DIR`): the allowlist file
  uploads are restricted to. The flag wins when both are set; when
  neither is declared, the runner is fail-closed (issue #76) — the same
  shared guard (`qaloop.upload.check_upload_path`) referenced under
  Perform's Upload-dir restriction.
- `--executable-path <path>`: chromium executable (default:
  playwright's bundled chromium).
- `--investigate`: on a failed single-flow run, summon the agentic
  investigator (`qaloop.investigate` module). Requires both the flag
  and the flow's `investigate_on_failure: true` (the spec default). Not
  applied in batch mode — `verify` prints
  `note: --investigate is not applied in batch mode` — and not per
  combination in matrix mode. A diagnosis appends a `kind: "investigation"`
  ledger entry (model, tokens in/out, estimated cost).
- `--max-investigation-actions N`: cap on investigator actions. Default
  `None` — falls back to the flow's `max_investigation_actions` (spec
  default 20).
- `--video`: record the run as `video.webm` in the run dir (issue #90;
  opt-in, default off).
- `--keep-runs N` (env `QALOOP_KEEP_RUNS`): keep at most the N newest
  run dirs; the flag wins. Default 0 = keep everything. Negative values
  print `ERROR: ...` and exit 2. See [Run artifacts](#run-artifacts)
  for the retention semantics. Pruning prints
  `pruned <n> old run dir(s) (--keep-runs <N>)`.

### Single-flow execution order

1. `--keep-runs` resolves first; an invalid value prints `ERROR: {e}`
   and exits 2 before anything runs.
2. The flow's `services:` are light-parsed (no env substitution). A
   parse failure prints `INVALID SPEC: {e}` and exits 2.
3. A run dir `<runs>/YYYYMMDDTHHMMSSZ-<flow-name>` is created under the
   `QALOOP_RUNS` env var, or `<repo>/runs` by default. `verify` prints
   `run dir: <run_dir>` and `target:  <target>`.
4. Declared services are booted and, in a `finally`, torn down. A boot
   failure (only `TimeoutError`, `RuntimeError`, or `ValueError`) prints
   `SERVICE BOOT FAILED: <Type>: <message>` and exits 3.
5. A flow with `matrix:` runs in matrix mode: one combination per
   subdir `<run_dir>/matrix/<NN>-<combo-slug>/`, each with its own
   `run.json` and `REPORT.md`, and a combined summary in the parent
   `REPORT.md`. Exit 0 iff every combination passed.
6. Otherwise `run_flow` executes the steps, `write_report` writes
   `REPORT.md` (plus `run.json`, `junit.xml`, `network.jsonl` per
   [Run artifacts](#run-artifacts)), `verify` prints the step summary
   and `report: <report_md>`, and a `kind: "scripted"` ledger entry is
   appended (`cost_usd_est: 0.0`; matrix runs append
   `kind: "scripted-matrix"`).
7. If the run failed and both `--investigate` and
   `investigate_on_failure` are on, `verify` prints
   `step failed — summoning investigator...` and runs the investigation.

### Exit-code contract

| Code | Meaning |
| ---- | ------- |
| 0 | the flow passed (single, matrix, and batch modes: every flow passed) |
| 1 | at least one step failed, or (batch) at least one flow failed |
| 2 | `INVALID SPEC` at any stage (services light-parse or spec load), a `--tag` skip on a single file, an empty batch selection, or a bad `--keep-runs` value |
| 3 | `SERVICE BOOT FAILED` — a declared service failed to boot |

`INVALID SPEC` and `SERVICE BOOT FAILED` are exit 2/3 in single-flow
mode, but recorded as per-flow failures (exit 1 or 0 overall) in batch
mode — batch resilience is a deliberate deviation, see Batch
verification.

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
  (privacy by default). Request bodies are opt-in only: a flow sets the
  top-level `record_bodies: true` (must be a bool, default `false`), and
  then each network entry gains a `body` key with the raw request body
  capped at 5000 chars (`null` when the request had no body). The key is
  absent entirely when `record_bodies` is not `true`, so the default log
  schema is unchanged.
- `video.webm` — screen recording of the whole run (Playwright
  `record_video`). Opt-in: recorded only when the flow sets the top-level
  `video: true` (must be a bool, default `false`) or the run passes the
  `--video` CLI flag (the flag forces recording on even when the spec is
  `false`; it cannot force it off when the spec is `true`). Privacy by
  default, like `record_bodies`: videos are large, so nothing is recorded
  unless asked. The video is linked in `REPORT.md`'s header summary block
  and, on failure, next to the Screenshot/AX snapshot in the "Failing
  step" section. A video failure never fails the run by itself.

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

## Worker, failure notifications, compare, flakiness tracking

### Worker (`qaloop worker`)

The worker claims verification jobs from the queue and runs them to
completion, writing the same run artifacts as `verify` (see above).
Its flags:

- `--flows DIR` (default `<install-root>/flows` — the `flows/`
  directory inside the qaloop install root): the directory the worker
  reads flow YAML from. A `verify-flow` job whose payload `flow` is a
  relative path resolves it under DIR; a `verify-repo` flow entry that
  is a bare name falls back to DIR when it exists nowhere else under
  the install root. Absolute flow paths bypass DIR entirely.
- `--once`: exit when the queue is empty instead of polling forever.
  The worker still waits for in-flight jobs to finish before exiting
  (`--once` exits only when no job is claimable AND nothing is still
  running), so in-flight jobs are never abandoned. One-shot / CI mode.
- `--poll SECS` (default 5): how long the worker sleeps between polls
  when the queue is empty. Every wake-up re-attempts a claim, so a
  lower value makes newly enqueued jobs start sooner at the cost of
  busier polling; the default 5 seconds is the quiet middle ground.
- `--concurrency N` (default 1): the worker claims and runs jobs in
  parallel through a thread pool of N threads. Claims are atomic
  (`BEGIN IMMEDIATE` on the queue database), so parallel workers never
  double-claim a job. Each job gets its own browser — the runner
  launches and closes one per job — and its own run directory, so
  concurrent jobs never share a run dir. The default 1 is the old
  serial claim-and-run loop exactly.
- `--job-timeout SECS` (default 1800): a job still running after SECS
  is recorded failed through the queue's normal retry policy with a
  `job timeout: exceeded Ns` note, instead of surfacing only at the
  1-hour stale requeue. A per-job payload `timeout_s` overrides the CLI
  default when it is a positive number; missing, non-positive, or
  non-numeric values fall back to the default defensively. A terminal
  timeout (retry budget exhausted) follows the same failure-notification
  path as any terminal failure. The timed-out browser call is left to
  finish on its own — the runner's own cleanup closes the browser when
  the hung call returns.
- `--keep-runs N` (or the `QALOOP_KEEP_RUNS` env var; the flag wins
  when both are set): after each job's report is written, at most the
  N newest run directories are kept, oldest deleted first (see
  Retention above). The default 0 keeps everything; negative values
  (and non-integer env values) are rejected with an error (exit 2).
- `--upload-dir <dir>` (env `QALOOP_UPLOAD_DIR`): the allowlist file
  uploads are restricted to. The flag wins when both are set; when
  neither is declared, the runner is fail-closed (issue #76) — the same
  shared guard (`qaloop.upload.check_upload_path`) referenced under
  Perform's Upload-dir restriction.

### Failure notifications

When a worker job reaches a terminal failure — the retry budget is
exhausted, or the job had `max_retries=0` (the enqueue default) so the
first failure is terminal — the worker POSTs one failure notification
to the URL in the `QALOOP_NOTIFY_URL` environment variable. Attempts
that are requeued stay silent; only the terminal outcome notifies.

The POST body is JSON with the keys `job_id`, `kind`, `run_dir`, and
`summary`, sent with a JSON content type and a 5-second timeout.
Notification delivery is best-effort and never raises: an unset URL
sends nothing, and delivery failures are printed to stdout without
failing the job handling itself.

### `qaloop compare`: diffing two runs

`qaloop compare <runA> <runB>` diffs two run directories from their
`run.json` artifacts — no browser involved, and the run dirs are
treated as read-only. Each argument is either a run-dir path (used
as-is) or a run-id prefix matched against run-dir basenames inside the
runs root; an unknown or ambiguous prefix is an error.

The structured diff covers:

- the top-level status change (`status_a` vs `status_b`);
- per-step outcomes keyed by step name: each step is `added`,
  `removed`, `status_changed`, or `unchanged`;
- duration deltas at the run level and per step. A step is flagged as
  a duration regression when run B took more than 20% longer than run
  A (`DURATION_REGRESSION_THRESHOLD = 0.20`), and only when run A's
  duration is nonzero.

The runs are `identical` only when the top-level status and every
step's outcome are unchanged — duration drift alone does not break
identicalness. A human-readable text rendering of the diff is printed
with a final `identical` / `differ` line.

Exit codes: 0 when the runs are identical, 1 when they differ (like
`diff`), 2 on error (missing or corrupt run dir, ambiguous run id).
A run.json whose steps is not a list of step objects is corrupt for
compare purposes: compare prints one error line, exits 2, and prints
no diff.
A run.json whose top-level or any step's duration_ms is present and
not a number is likewise corrupt for compare purposes: compare
prints one error line, exits 2, and prints no diff; a missing
duration_ms remains valid and is treated as 0.

### Flakiness tracking: pass rate over time

`qaloop dashboard` regenerates the static dashboard from the runs
root, including a "Pass rate over time" table: runs are grouped by
flow name and, per flow, ordered by start time; the pass rate is
computed over the trailing 10 runs (`TREND_WINDOW`), so stale history
ages out of the trend. A run counts as passed only when its status is
exactly `"passed"`; corrupt or fieldless entries are tolerated and
simply count as failures. A run.json whose top level is not a JSON
object (for example `42`, `[1, 2]`, or a bare string) is tolerated
the same way as a corrupt one: it is skipped, the dashboard is
still written, and the command exits 0. A run.json object whose
`steps` is not a list of step objects, or whose `diagnosis` is
not an object, is tolerated the same way: the malformed
decorations are ignored (no retry or dx label is rendered from
them), the run row still renders and counts as a failure unless
its status is exactly `"passed"`, the dashboard is still
written, and the command exits 0. The table shows recent flakiness per flow
without opening every report.

A malformed ledger (a line that is not valid JSON, a line that is
not a JSON object, or a present `cost_usd_est` that is not numeric)
makes `qaloop dashboard` print exactly one `error: ledger line N ...`
line naming the problem and the line, write nothing, and exit 2.
Valid and missing ledger files are unchanged (exit 0, dashboard
written). This is the same malformed-ledger contract as for
`qaloop ledger` in the Cost ledger section below.

## Queue pipeline: enqueue, webhook receiver, investigator, cost ledger

The worker side is documented above; this section covers how jobs get
INTO the queue, the standalone investigator command, and the cost
ledger.

### `qaloop enqueue`: putting jobs on the queue

`qaloop enqueue` inserts one job into the sqlite queue. Its flags:

- `--kind KIND` (default `verify-flow`; choices `verify-flow`,
  `verify-repo`): the job kind.
- `--flow FLOW` (for `verify-flow`): the flow YAML. The payload is
  given the flow only when not already present (`setdefault`), and the
  target only when `--target` is given.
- `--target TARGET` (for `verify-flow`): target URL override, stored
  in the payload when given.
- `--payload JSON` (default `"{}"`): extra JSON payload. For
  `verify-repo` this is the whole job payload — e.g.
  `--payload '{"repo": "ac"}'`. The payload must be a JSON
  object: invalid JSON, or a valid JSON value that is not an
  object (array, number, string, boolean, null), is an error
  (exit 2) and nothing is enqueued.
- `--max-retries N` (int, default 0): retry the job up to N extra
  attempts after the first failure, through the queue's normal retry
  policy (see Worker above). The default 0 makes the first failure
  terminal, which is the terminal-failure condition the failure
  notifications listen for. A negative value is an error (exit 2);
  non-integer or boolean values are rejected the same way at the
  queue level.

Exit 0 on success, printing
`enqueued job #<id> (<kind>, max_retries=<N>)`.

### Webhook receiver (`qaloop webhook`)

`qaloop webhook` runs a small HTTP receiver that turns GitHub PR
events into queued `verify-repo` jobs, plus a manual `/enqueue`
endpoint. Its flags: `--port` (default 8090), `--host` (default
`127.0.0.1`). Endpoints:

- `GET /health` -> 200 `{"ok": true, "pending": <n>}` (pending queue
  depth). Anything else returns 404.
- `POST /webhook/github` — the GitHub webhook URL
  (`http://<host>:<port>/webhook/github`, content type
  `application/json`). The secret is the `QALOOP_WEBHOOK_SECRET`
  environment variable: HMAC SHA-256 over the raw request body,
  compared (constant-time) against the `X-Hub-Signature-256` header
  (which carries the `sha256=` prefix). A mismatch returns 401
  `{"ok": false, "error": "bad signature"}`. With no secret configured
  the check is skipped entirely — local dev only, never in
  production. `ping` events return 200 `{"ok": true, "msg": "pong"}`;
  malformed JSON returns 400. `pull_request` events enqueue a
  `verify-repo` job (payload `repo`, `repo_full`, `pr`, `sha`,
  `source: "github-webhook"`) and return 200
  `{"ok": true, "job_id": <id>}` — but only for the actions `opened`,
  `synchronize`, `reopened`, and `labeled`, and `labeled` only when the
  added label is `needs-qa`. Every other action or event returns 200
  `{"ok": true, "msg": "ignored ..."}` without enqueuing.
- `POST /enqueue` — manual enqueue over HTTP. When the
  `QALOOP_ENQUEUE_TOKEN` environment variable is set, the request must
  carry a matching `X-QALoop-Token` header, else 401
  `{"ok": false, "error": "bad token"}`; with no token configured the
  check is skipped. The JSON body becomes the job payload, with `kind`
  taken from the body (default `verify-flow`), enqueued with the
  default retry budget. Returns 200 `{"ok": true, "job_id": <id>}`;
  malformed JSON returns 400.

### `qaloop investigate`: the standalone investigator

`qaloop investigate` runs the agentic investigator against an
EXISTING run directory — no new verification run is launched. Its
flags:

- `--run DIR` (required): the run directory, which must contain
  `run.json`.
- `--flow FLOW` (required): the flow YAML used for the run.
- `--target TARGET`: target URL override (defaults to the target
  recorded in `run.json`).
- `--max-actions N` (default 20): the investigator's hard browser-action
  budget.
- `--headed`: show the browser instead of running headless.
- `--executable-path PATH`: chromium executable override.

It loads `run.json` as a `RunResult`, loads the flow spec, runs the
investigation, and prints the diagnosis as JSON to stdout (exit 0).
A missing run directory, a missing or corrupt `run.json`, a
structurally malformed `run.json` (valid JSON whose shape a
`RunResult` cannot be built from: a non-object top level, a
missing required key, or a `steps` value that is not a list of
step objects), or a missing or invalid flow is an error:
exactly one error line is printed and the exit code is 2, with
nothing written.
Two modes:

- No model credentials configured -> manual mode: writes
  `INVESTIGATION_BRIEF.md` into the run dir (the failure context the
  agent would have used) and returns
  `{"mode": "manual", "brief": <path>, "cost_usd_est": 0.0}`.
- Model credentials configured -> agent mode: drives the browser
  against the target within the action budget, writes the transcript
  to `INVESTIGATION.md` in the run dir, and augments the diagnosis
  with `mode: "agent"`, `model`, `tokens_in`, `tokens_out`,
  `cost_usd_est` (rounded to 4 decimals), and `actions_taken`. If the
  budget is exhausted without a conclusion the diagnosis is
  `{"diagnosis": "Investigator exhausted its budget without a
  conclusion.", "likely_cause": "unknown", "suggested_fix": ""}`.

### Cost ledger (`qaloop ledger`)

The cost ledger is an append-only JSONL file: every run appends one
entry, and `qaloop ledger` (no flags) prints `summarize()` as
indented JSON to stdout (exit 0). The ledger file is
`<checkout>/runs/ledger.jsonl`, overridable with the `QALOOP_LEDGER`
environment variable; the parent directory is created on demand.

Entries are appended by:

- `verify`: `{"kind": "scripted", ..., "cost_usd_est": 0.0}`;
- an investigator run on verify failure:
  `{"kind": "investigation", "model", "tokens_in", "tokens_out",
  "cost_usd_est"}`;
- matrix verification: `{"kind": "scripted-matrix", ..., "cost_usd_est": 0.0}`;
- `perform`: `{"kind": "perform", ..., "cost_usd_est"}` (the agent's
  real model cost);
- `evaluate`: `{"kind": "evaluate", ..., "tokens_in", "tokens_out",
  "cost_usd_est"}`.

`summarize()` reads every line and returns:

- `runs`: total entries (all kinds);
- `scripted_runs`: entries with `kind == "scripted"`;
- `investigations`: entries with `kind == "investigation"`;
- `total_cost_usd_est`: sum of `cost_usd_est` over all entries
  (entries without a cost count 0), rounded to 4 decimals;
- `investigation_cost_usd_est`: the same sum over `investigation`
  entries only, rounded to 4 decimals.

Note: `scripted-matrix`, `perform`, and `evaluate` entries count
toward `runs` and the total cost, but not toward `scripted_runs` or
`investigations`.

A malformed ledger (a line that is not valid JSON, a line that is
not a JSON object, or a present `cost_usd_est` that is not numeric)
makes `qaloop ledger` print exactly one `error: ledger line N ...`
line naming the problem and the line, print no summary, and exit 2.
Valid and missing files are unchanged (exit 0, indented JSON).

## `qaloop doctor`: environment self-check

`python3 -m qaloop.cli doctor` self-checks the environment so a run never
fails cryptically — the same traps that keep biting this loop's own
lessons (broken chromium install, missing ffmpeg). It takes no arguments
and never launches a browser.

The four checks run in this order, one PASS/FAIL line per check:

1. `chromium` — resolves the chromium executable the same way `verify`
   does (`runner.py`: the `QALOOP_EXECUTABLE_PATH` env var wins when set,
   falling back to Playwright's default). When the env var is set but
   points at a missing file, that is a FAIL. Only the `executable_path`
   property is read — no browser process starts. A FAIL names the fix
   (`python3 -m playwright install chromium`).
2. `ffmpeg` — `ffmpeg` must be on PATH. A FAIL, not a warning:
   `verify --video` records `video.webm` and cannot record without it.
3. `python` — the `yaml` and `playwright` Python packages must be
   importable.
4. `demo` — the fixtures the documented demo proof needs must be present
   in the checkout's `demo/` directory: `mock-demo.html`,
   `demo-login.html`, `demo-a11y.html`, `cookie-storage.html`.

Output: one `PASS <name>: <detail>` or `FAIL <name>: <detail>` line per
check, then a machine-readable summary line of the form
`doctor: chromium=PASS ffmpeg=PASS python=PASS demo=PASS`.

Exit codes: 0 when every check passes, 2 on any FAIL (mirrors the
batch-verify convention: exit 2 = broken configuration), so a broken
environment never silently passes.

## Evaluate: the semantic judge

`python3 -m qaloop.cli evaluate --claim ... --run <run-dir>
--diff <file|range>` judges whether the claimed behavior actually makes
sense given the diff and the run evidence (steps, assertions, AX snapshots,
console/network errors, service logs). It writes `EVALUATION.md` +
`evaluation.json` into the run dir and logs the model cost.

A missing `run.json` in the run dir is tolerated (the judge proceeds on
empty run evidence). A malformed `run.json` — not valid JSON, or a top
level that is not a JSON object — is rejected instead: `evaluate` prints
exactly one error line naming the problem, exits 2, writes no
`EVALUATION.md` / `evaluation.json`, and makes no model call. A `run.json`
whose `steps` is not a list of step objects is likewise malformed for
evaluate purposes: `evaluate` prints exactly one error line naming the
problem, exits 2, writes no evaluation files, and makes no model call.
A `run.json` whose `error` (top-level or step-level) is not a string, or
whose `console_errors` / `page_errors` / `failed_requests` /
`bad_responses` is not a list, is likewise malformed for evaluate
purposes: `evaluate` prints exactly one error line naming the problem,
exits 2, writes no evaluation files, and makes no model call.

Verdicts: `MAKES_SENSE`, `DOES_NOT_MAKE_SENSE`, `INSUFFICIENT_EVIDENCE`,
`BLOCKED`.

### Rubric scoring

The judge also scores four fixed rubric dimensions, each `0` (fails) /
`1` (partial or unclear) / `2` (solid):

- `claim_diff_fit` — the diff plausibly implements the claim.
- `evidence_exercises_claim` — the flow evidence actually exercises the
  claimed behavior, rather than passing vacuously.
- `no_contradictions` — nothing in the evidence contradicts the claim.
- `state_supports_claim` — the final browser state, logs, and service
  output support the claim.

Scores are normalized defensively: each dimension must be an integer
`0`–`2` (booleans excluded); missing, out-of-range, or wrongly-typed
values default to `0`.

The scores drive the final verdict via `_apply_rubric`, which runs before
confidence calibration:

- `0` in `claim_diff_fit` or `no_contradictions` → `DOES_NOT_MAKE_SENSE`.
- `0` in `evidence_exercises_claim` or `state_supports_claim` →
  `INSUFFICIENT_EVIDENCE`.
- Otherwise every dimension scored `1`–`2`: a total of at least `6` of `8`
  → `MAKES_SENSE`, anything less → `INSUFFICIENT_EVIDENCE`.

Carve-outs: a raw `BLOCKED` verdict is kept as-is — the run produced no
evidence to score. When the model supplies no scores object at all there
is nothing to derive from, so the raw verdict stands (the all-zero
normalized scores are still recorded for audit). The model's raw verdict
is otherwise advisory: it is kept as `verdict_raw` in `evaluation.json`
for audit (like `confidence_raw`, it is not rendered in `EVALUATION.md`),
which also carries the `scores` object. `EVALUATION.md` renders the
dimension scores as a table directly under the verdict header.

### Confidence calibration

The judge also returns a `confidence` of `high` | `medium` | `low` with
each verdict. The value is normalized case-insensitively; a missing or
unparseable value defaults to `low`. The raw value is kept as
`confidence_raw` in `evaluation.json` for audit (it is not rendered in
`EVALUATION.md`).

Calibration rules, applied to the verdict before anything is written:

- Low-confidence `MAKES_SENSE` is downgraded to `INSUFFICIENT_EVIDENCE` —
  a verdict the judge is unsure of cannot stand. The rationale is appended
  noting the downgrade and that the judge reported low confidence.
- High-confidence `MAKES_SENSE` requires at least 2 *grounded* evidence
  bullets (see below); fewer downgrades to `INSUFFICIENT_EVIDENCE` — a
  confident verdict with no cited evidence is not credible. The rationale is
  appended noting the downgrade and the missing evidence. When this
  downgrade fires, any cited bullets that could not be grounded are named
  in the rationale.
- `DOES_NOT_MAKE_SENSE` keeps its verdict at any confidence (a contradiction
  is a contradiction). Low confidence is instead surfaced prominently in
  `EVALUATION.md` with a warning callout directly under the verdict header.
- `INSUFFICIENT_EVIDENCE` and `BLOCKED` are never reclassified by
  calibration.

### Evidence-citation grounding

Before calibration, every non-empty evidence bullet is checked
deterministically against the judge's own evidence (`_ground_evidence`).
A bullet is *grounded* when it:

- (a) names a step index or step name present in the collected steps;
- (b) names a `file:line`-style path whose file appears in the diff
  headers (`diff --git a/<f> b/<f>`);
- (c) contains a quoted substring of at least 8 characters occurring
  verbatim in the judge context — the prompt message built from the claim,
  diff, and evidence by `_user_message`, so a bullet quoting the claim
  text, a log line, or an AX fragment counts. A quoted substring shorter
  than 8 characters does not qualify.

Empty bullets are skipped (not recorded). The per-bullet results are
stored as `evidence_grounding` — a list of
`{bullet, grounded, reason}` — in `evaluation.json`, and ungrounded
bullets are marked in `EVALUATION.md` as
`- <bullet> — [UNGROUNDED CITATION]` (grounded bullets render unchanged).

### Critic pass

After evidence grounding and before calibration, the pipeline makes a
second model call that tries to break the primary verdict. The critic
sees the same judge context built by `_user_message`, plus the verdict
under review — verdict, confidence, rubric scores, rationale, and a
per-bullet grounding summary — assembled by `_critic_user_message`.

Critic prompt contract: given the evidence and the judge's verdict,
return the strongest evidence-backed reason the verdict could be wrong,
or `NONE`. The response is a single JSON object,
`{"objection": "NONE" | "<one paragraph: the strongest reason the verdict could be wrong>"}`.
Bare `NONE` and unparseable output both mean "no objection"; an
unreadable critic never moves the verdict.

Deterministic coupling rule (`_critic_coupling`, no model discretion):
the objection is parsed by `_parse_critic` and grounded with the same
three rules as evidence-citation grounding — it must (a) name a real
step index or step name from the collected steps, (b) name a `file:line`
whose file appears in the diff headers (`diff --git a/<f> b/<f>`), or
(c) contain a quoted substring of at least 8 characters occurring
verbatim in the judge context. Only a *grounded* objection moves the
verdict:

- confidence downgrades one level (`high` -> `medium` only);
- the objection is appended to the rationale;
- it is recorded as `critic_objection` in `evaluation.json` and rendered
  in `EVALUATION.md`'s "Critic" section (which always renders — either
  the objection or "No grounded objection").

The verdict is never reclassified and the rubric scores are never
touched. A grounded objection on a non-`high` confidence verdict is
recorded and appended but does not move confidence. Calibration runs
after the critic; it only downgrades low-confidence (or
ungrounded-high-confidence) `MAKES_SENSE`, so a `medium` confidence
produced by the critic cannot trigger `INSUFFICIENT_EVIDENCE`.

Cost: the critic call's tokens are summed into the existing `_cost`
dict — `tokens_in`/`tokens_out` cover both calls.

## Perform: the natural-language browser agent

`python3 -m qaloop.cli perform --task "..." --target <URL>` is a
natural-language browser agent for *doing work*, not just checking it.
It takes over a fresh browser (default headless) or an already-running
one via CDP, reads the page as an accessibility tree, acts semantically
(role/name/text — never CSS trivia), paces itself like a person
(scroll-into-view, typed input, waits for state), and stops honestly
when blocked. "Human-like" means robust and legible — real clicks,
paced typing, semantic targeting, state checks — not bot-evasion or
fingerprint spoofing.

Flags:

- `--task` (required) — what the agent should do.
- `--target` — starting URL. Omit when attaching to a live browser.
- `--max-actions` (int, default 30) — the agent's action budget. Each
  model action (including `done`/`blocked` and `screenshot`) consumes
  one action; using all `max_actions` without finishing ends the run as
  `incomplete`.
- `--headed` (flag) — run the fresh browser headed (default headless).
- `--cdp-url` — connect to a live browser over CDP, e.g.
  `http://127.0.0.1:9222`, instead of launching a fresh one. The first
  existing context/page is reused when present; the browser is left
  open afterwards (`attached: true` in `perform.json`).
- `--executable-path` — chromium binary override (same resolution as
  `verify`).
- `--allow-publish` (flag) — permit external publish/post actions
  (blocked by default, see Safety below).
- `--upload-dir` — directory file uploads are restricted to. Default:
  the run dir. Only absolute paths under it are accepted (fail-closed).
- `--max-cost-usd` (float, default 0 = unlimited) — estimated model
  spend ceiling. Negative values are rejected (`ERROR: --max-cost-usd
  cannot be negative`, exit 2).

### Model config and cost estimation

Perform needs a model: any OpenAI-compatible `/chat/completions`
endpoint. Without `QALOOP_MODEL_API_KEY` the command fails fast
(`ERROR: ...`, exit 2) — no browser is launched. Config comes from env:

- `QALOOP_MODEL_BASE_URL` (default `https://api.openai.com/v1`),
- `QALOOP_MODEL_NAME` (default `gpt-4o-mini`),
- `QALOOP_MODEL_API_KEY` (default empty — required),
- `QALOOP_MODEL_PRICE_IN` (default `0.15`, USD per 1M tokens),
- `QALOOP_MODEL_PRICE_OUT` (default `0.60`, USD per 1M tokens).

Estimated USD cost is
`tokens_in/1e6 * price_in + tokens_out/1e6 * price_out`, rounded to 4
decimals (`cost_usd_est`). The **cost ceiling**: the first model call is
always issued (there is no estimate basis yet); afterwards the run stops
*before* the next model call when `cost_so_far + last_call_cost` would
exceed `--max-cost-usd`, with status `blocked` and the summary `cost
ceiling reached (max-cost-usd <n>)`. The ceiling and the stop are
recorded in `perform.json`, `PERFORM.md`, and the ledger.

### Action set and targeting

Each turn the model replies with exactly one JSON action (temperature
0.2, max_tokens 1200 per call); a non-JSON reply is re-prompted once —
no action record is added, though the turn still counts against the
`max_actions` loop budget. The actions are:

`navigate`, `click`, `dblclick`, `fill`, `press`, `select`,
`check`/`uncheck`, `hover`, `scroll`, `wait`, `screenshot`, `console`,
`upload`, plus the terminal `done` ("summary": what was accomplished)
and `blocked` ("reason": why it cannot proceed).

A target is `{"role": ..., "name": ...}` (preferred), `{"text": "..."}`,
or as a last resort `{"css": "..."}`. Resolution is strict: zero
matches or more than one match fails the action (a multi-match names
the count and asks for disambiguation) rather than guessing. An action
failure becomes the observation `FAILED: <ExceptionType>: <message>` —
the loop continues; only harness-level refusals (see Safety) raise
`PermissionError` and surface as `BLOCKED BY HARNESS`.

Semantics, verified against `qaloop/perform.py`:

- **Pacing.** A 0.25–0.75s pause follows every action (`_pace`).
  Click-like actions scroll the element into view first. `fill` types
  short text (<= 80 chars) with select-all then keystrokes at ~30–70ms
  intervals; longer blobs are set instantly.
- **Waiting.** `wait` defaults to state `visible`; a text-only target
  waits for that text, otherwise it waits for the element state (up to
  15s). A bare `wait` with no target sleeps `ms` (default 1000).
- **Reading.** Each observation is the URL, title, any auto-dismissed
  dialogs, and an AX snapshot (max 400 nodes, 25000 chars). A
  `screenshot` saves `steps/perform-NN.png` in the run dir and its PNG
  rides along on the next turn as image content, so the model sees the
  page visually. If the endpoint rejects images, the run retries
  text-only once and stays text-only.
- **Stopping.** After each action the agent must verify the outcome in
  the next observation, never assume it worked. `done` ends the run as
  `completed`; `blocked` ends it as `blocked`; exhausting the action
  budget ends it as `incomplete`. Anything ambiguous is a stop, not a
  guess.

### Safety

Safety is enforced in the harness, not just the prompt (a misbehaving
model cannot click through):

- **Destructive actions are blocked.** On `click`, `dblclick`,
  `press`, or `check`, when the target's name/text matches
  `pay | purchase | checkout | buy now | delete | remove | destroy |
  erase | wipe` (word-boundary, case-insensitive), the harness raises
  `PermissionError` — no model discretion.
- **External publishing is blocked by default.** When the target's
  name/text matches `publish | post | tweet | share | send invite`
  (word-boundary, case-insensitive), the action is refused unless
  `--allow-publish` was passed.
- **Dialogs are stop signs.** Any browser dialog is auto-dismissed
  (recorded, the last three shown to the model); a confirmation dialog
  for something destructive ends the task as `blocked`, never as a
  speed bump. The model is also instructed up front: never submit a
  payment, delete or destroy data, or publish/post externally — if the
  task or a page asks for any of these, stop with `blocked` and say
  why.

### Upload-dir restriction (fail-closed)

The `upload` action attaches a local file to a `<input type=file>`
(target the input itself). Only absolute paths under the declared
upload dir (default: the run dir) are accepted; relative paths and `..`
escapes are rejected with `ValueError`, surfacing as a FAILED action —
the same shared guard (`qaloop.upload.check_upload_path`) that the
verify runner uses (issue #76), so both code paths reject the same
violations.

### Run artifacts, reporting, exit codes

Every run creates `runs/<id>/` (via `qaloop.artifacts.new_run_dir`
with the `perform` prefix), containing:

- `perform.json` — the full result: `task`, `target`, `status`
  (`completed` / `blocked` / `incomplete`), `summary`, the action
  `transcript` (each record: `n`, the `action`, and its result string),
  `action_count`, `tokens_in`, `tokens_out`, `cost_usd_est`,
  `max_cost_usd`, `ceiling_hit`, `final_url`, `final_title`,
  `attached`.
- `PERFORM.md` — human-readable report: status line, action/token/cost
  line, final page, and the numbered transcript.
- `steps/perform-NN.png` — screenshots taken by the `screenshot`
  action.

The command prints `status`, `summary`, `actions` + `tokens` + `est $`,
the `cost ceiling` when set, and the `report:` path. It appends a ledger
entry (`kind: "perform"`) with `run_dir`, `task` (first 120 chars),
`status`, `actions`, `tokens_in`, `tokens_out`, `cost_usd_est`,
`max_cost_usd`, and `ceiling_reached`.

Exit codes: `0` when the run completed, `1` for `blocked` or
`incomplete`, `2` on configuration errors (missing model key, negative
`--max-cost-usd`).
