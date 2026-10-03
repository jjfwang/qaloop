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
```

## Action ops (one per step; a step may also carry `expect`)

| op | params | notes |
|---|---|---|
| `goto` | URL string | `/path` resolves against `target` |
| `click` / `dblclick` | selector | CSS, `text=`, `role=` all work |
| `fill` | `{target, text}` | clears first |
| `press` | `{target, key}` | e.g. `{target: "#q", key: "Enter"}` |
| `check` / `uncheck` | selector | checkboxes |
| `select` | `{target, value}` | `<select>` value |
| `wait` | `{target?, state?, text?, timeout_ms?}` | state: visible\|hidden\|attached\|detached; `text` waits for the selector to contain the text |
| `wait_ms` | int | fixed pause (prefer `wait`) |
| `reload` / `back` | `true` | navigation |
| `seed` | `{http: {url, method?, json?}}` or `{js: "..."}` | test-data / state setup |
| `script` | `{js: "..."}` | `page.evaluate`; result ignored |

Step meta keys: `name`, `expect`, `continue_on_fail` (default false —
a failed step aborts the run), `timeout_ms` (overrides `timeouts.step_ms`).

## Assertions (`expect:` mapping)

| assertion | params |
|---|---|
| `visible` / `hidden` | selector |
| `text_contains` | `{selector, text}` |
| `text_matches` | `{selector, pattern}` (regex) |
| `count` | `{selector, equals: n}` / `{selector, gte: n}` / `{selector, lte: n}` |
| `url_contains` / `title_contains` | string |
| `noop` | `true` — documents an intentionally dead control |
| `console_clean` | `true` — no new console/page errors since the step started |
| `js` | `{script, contains}` — evaluate JS, output must contain text |

## Conventions

- Write the spec alongside the feature; the implementer authors its own
  acceptance flow. Encode known limitations (`noop`, OD references in
  `name`) so the verifier doesn't file bugs about intentional behavior.
- Prefer `wait` with a state over `wait_ms`.
- Keep flows to one user goal each; 5–25 steps is the sweet spot.
