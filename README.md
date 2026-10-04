# qaloop — agentic QA loop for repos

A code agent can implement a feature. qaloop checks whether the feature
**actually works** — by driving it in a real browser, every time, cheaply.

The design is a hybrid:

1. **Deterministic flows** (Playwright, zero LLM cost) run the happy path:
   declarative YAML specs — goto, click, fill, assert. This is ~80% of
   verification and costs nothing in tokens.
2. **Agentic investigator** (bounded, text-first) is summoned **only when a
   step fails**: it explores with the accessibility tree (not
   screenshot-every-step), within a hard action budget, and returns a
   diagnosis a code agent can act on.
3. **The bug report writes itself** — screenshot, console errors, failed
   requests, AX snapshot at the failure point — and becomes the next
   implementer task. Loop until green or N rounds, then escalate.

   A failed `screenshot_matches` comparison also leaves a diff-highlight
   PNG in the run's `steps/` dir (`assert-<phase>-<idx>-diff.png`): the
   actual screenshot with drifted pixels painted red, named in the
   assertion detail. Green runs produce no diff artifacts.

```
implementer → reviewer → verifier (qaloop) → merge
                              ↳ fail → bug report → implementer (fix round)
```

## Quickstart

```bash
# one-time
pip install playwright pyyaml
python3 -m playwright install chromium

# validate a flow spec (no env needed)
python3 -m qaloop.cli validate flows/game-loading-frames.yaml

# serve the game client and verify it
cd ~/workspace/stoneage-reimagined/apps/game-client
python3 -m http.server 8901 --bind 127.0.0.1 &   # one terminal
cd ~/workspace/qaloop
TARGET_URL=http://127.0.0.1:8901 python3 -m qaloop.cli verify flows/game-loading-frames.yaml
# → [PASS] ... + runs/<stamp>/REPORT.md
```

With the investigator on failure:

```bash
export QALOOP_MODEL_API_KEY=...            # any OpenAI-compatible endpoint
export QALOOP_MODEL_BASE_URL=https://api.openai.com/v1   # or OpenRouter / Ollama
export QALOOP_MODEL_NAME=gpt-4o-mini       # cheap model for navigation
TARGET_URL=http://127.0.0.1:8901 python3 -m qaloop.cli verify flows/x.yaml --investigate
```

No model key? The investigator writes `INVESTIGATION_BRIEF.md` (full evidence
bundle + bounded brief) for a human or another agent instead of failing.

## Flow specs

See [SPEC.md](SPEC.md). The implementer authors its own acceptance flow
alongside the feature. Conventions:

- One user goal per flow, 5–25 steps.
- Prefer `wait` with a state over `wait_ms`.
- Encode known limitations (`expect: {noop: true}`, OD references in step
  names) so the verifier doesn't file bugs about intentional behavior.
- Mock the data, not the UI: use `mock` in `setup` to feed deterministic
  API responses so flows exercise the real rendering path without a backend.
  To prove the UI actually called the API, assert `expect: {mock_calls: {url: "**/api/x", equals: 1}}` —
  it counts how often each registered mock served a request (method-mismatch
  and `times`-exceeded fallthroughs don't count; an unhit URL is 0).
  For debugging failed mocks or `mock_calls` misses, every run also writes
  `network.jsonl` into the run dir: one line per observed response or failed
  request (status, ms timing, no bodies), so you can see exactly what traffic
  the page actually saw. To assert on that traffic directly, use
  `expect: {network_calls: {url: "/api/x", equals: 1}}` — it counts log
  entries whose url contains the given substring (mock-served responses and
  failed requests count too). Add a `status: {equals: 200}` (or `gte`/`lte`)
  sub-filter to count only entries with that response status; a status
  filter never matches failed requests (`status: null`).
- Pin the pixels that matter: `screenshot_matches` baselines for key
  screens, `ax` assertions for the user's perceivable contract.
- Flaky UI: `retry: N` on a `steps`-phase step re-runs it up to N more times
  on failure (first success wins, no delay between attempts).

## CLI

| command | what it does |
|---|---|
| `validate <flow>` | check spec structure (env vars not required) |
| `verify <flow> [--target URL] [--investigate] [--headed] [--executable-path PATH] [--max-investigation-actions N]` | run once, print card, write `runs/<id>/REPORT.md` + `junit.xml` |
| `baselines <flow> [--target URL]` | **update mode**: save `screenshot_matches` baselines instead of comparing |
| `perform --task "..." --target URL [--executable-path PATH]` | natural-language browser agent: performs the task like a person (see below) |
| `evaluate --claim "..." --run <dir> --diff <file\|range> [--repo DIR]` | semantic judge: does the change make sense given the diff + flow evidence? writes `EVALUATION.md` |
| `enqueue --kind verify-flow --flow F --target U` | queue a one-off verification |
| `enqueue --kind verify-repo --payload '{"repo":"stoneage-reimagined"}'` | queue a repo run (boots env per `repos.yaml`) |
| `worker [--once] [--poll 5] [--flows DIR] [--executable-path PATH]` | claim jobs → boot env → run flows → report |
| `webhook [--port 8090] [--host 127.0.0.1]` | GitHub PR webhook → queue (`/webhook/github`); manual `/enqueue` |
| `investigate --run <dir> --flow <yaml> [--target URL] [--max-actions N] [--headed] [--executable-path PATH]` | run the investigator on an existing run |
| `dashboard [--out dir]` | regenerate static `index.html` |
| `ledger` | cost summary JSON |

- `--executable-path` is available on `verify`, `perform`, `investigate`, and
  `worker`: chromium executable (default: Playwright's). The flag wins over the
  `QALOOP_EXECUTABLE_PATH` env var — use it when the system browser is unusable.
- `--max-investigation-actions N` (on `verify`) caps the failure investigator's
  actions; defaults to the flow spec's `max_investigation_actions` (default 20).
- `--host` (on `webhook`, default `127.0.0.1`) is the bind host of the GitHub
  receiver — set it to `0.0.0.0` for containerized deploys.
- `--flows DIR` (on `worker`, default the `flows/` directory inside the qaloop
  checkout) is the directory the worker resolves bare flow names against when it
  claims verify-flow jobs — point it elsewhere to serve flows from another copy.
- `--repo DIR` (on `evaluate`, default: the current directory) is the repo root
  qaloop runs `git diff <range>` in when `--diff` is a git range rather than a
  diff file.
- `--max-actions N` (on `investigate`, default 20) caps the investigator's
  ReAct-loop action budget — when the budget is exhausted the investigator is
  told to finish with its best diagnosis.

## Demo: mock + UX in one flow

`demo/mock-demo.html` is a self-contained page (button fetches a mocked API,
signup form posts to a mocked endpoint). `flows/demo-mock-ux.yaml` mocks
both APIs, then proves the mocked data renders (`text_contains`), the button
is perceivable (`ax: {role: button}`), the pixels match a checked-in baseline
(`screenshot_matches`), and the signup confirmation appears. No backend:

```bash
cd ~/workspace/qaloop
python3 -m http.server 8931 --bind 127.0.0.1 &   # serves /demo/mock-demo.html
TARGET_URL=http://127.0.0.1:8931 python3 -m qaloop.cli baselines flows/demo-mock-ux.yaml
TARGET_URL=http://127.0.0.1:8931 python3 -m qaloop.cli verify flows/demo-mock-ux.yaml
```

## Real services: boot, run against, tear down

A flow can declare `services:` (see [SPEC.md](SPEC.md)) — real commands
qaloop boots before the run, waits for readiness (`port` / `http` /
`log_contains`), exposes as `QALOOP_SERVICE_<NAME>_URL`, then tears down
afterwards with logs captured under `<run_dir>/services/`:

```bash
python3 -m qaloop.cli verify flows/demo-services.yaml   # boots demo/proof_server.py, no manual server needed
```

`flows/demo-services.yaml` proves it against a real backend: the flow's
`target: ${QALOOP_SERVICE_API_URL}` resolves only after the service is
ready, and the run passes 5/5 against the live server.

## Evaluate: the semantic judge

`verify` tells you the flow passed; `evaluate` tells you whether the
change *makes sense* — given the claimed behavior, the diff, and the
flow's evidence (steps, assertions, AX snapshots, console/network errors,
service logs). It writes `EVALUATION.md` + `evaluation.json` into the run
dir and logs the model cost:

```bash
python3 -m qaloop.cli evaluate \
  --claim "The profile editor now saves the display name" \
  --run runs/<run-id> \
  --diff HEAD~1..HEAD
```

Verdicts: `MAKES_SENSE` · `DOES_NOT_MAKE_SENSE` ·
`INSUFFICIENT_EVIDENCE` · `BLOCKED`. Uses the same `QALOOP_MODEL_*`
model config as `perform`/investigator.

The judge also returns a confidence — `high`, `medium`, or `low` — and
confidence is coupled to the verdict before anything is written: a
low-confidence `MAKES_SENSE` is downgraded to `INSUFFICIENT_EVIDENCE`
(anything unparseable defaults to `low`, and the raw value is kept as
`confidence_raw` in `evaluation.json`); a high-confidence `MAKES_SENSE`
with fewer than 2 cited evidence bullets is downgraded too — a confident
verdict with no cited evidence is not credible. `DOES_NOT_MAKE_SENSE`
keeps its verdict at any confidence (a contradiction is a contradiction),
but low confidence is flagged prominently in `EVALUATION.md`. See SPEC.md
for the full calibration rules.

## Perform mode: the agent takes over a browser

`qaloop perform` is a natural-language browser agent for *doing work*, not
just checking it. Give it a task; it reads the page's accessibility tree,
acts semantically (role/name/text — never CSS trivia), paces itself like a
person (scroll-into-view, typed input, waits for state), and stops honestly
when blocked.

The agent's actions are `navigate`, `click`, `dblclick`, `fill`, `press`,
`select`, `check`/`uncheck`, `hover`, `scroll`, `wait`, `screenshot`,
`console`, and `upload`: `{"action": "upload", "target": {...},
"path": "/abs/path/file"}` attaches a file through a file input (target the
input itself, e.g. `{"css": "#resume"}`).

Uploads are restricted to a declared upload dir: `--upload-dir` (default:
the run dir) — only absolute paths under that directory are accepted,
so a model can't reach elsewhere on disk.

```bash
python3 -m qaloop.cli perform \
  --task "Open the profile editor, change the display name to Maya, and save" \
  --target http://localhost:3000 --max-actions 30
```

Take over a browser that's already running (debugging port on) instead of
launching a fresh one:

```bash
python3 -m qaloop.cli perform --task "..." --cdp-url http://127.0.0.1:9222
```

Safety is enforced in the harness, not just the prompt: payment submission,
deletion/destruction, and external publishing are blocked (`--allow-publish`
opts into publishing); a confirmation dialog for anything destructive is a
stop sign. Every run writes `runs/<id>/PERFORM.md` (transcript + final
state) and a `perform.json`, and logs tokens/cost to the ledger.

Human-like means robust and legible — real clicks, paced typing, semantic
targeting, state checks — not bot-evasion or fingerprint spoofing.

Needs a model: any OpenAI-compatible `/chat/completions` endpoint via
`QALOOP_MODEL_BASE_URL` / `QALOOP_MODEL_NAME` / `QALOOP_MODEL_API_KEY`
(same knobs as the investigator). Proved against `demo/mock-demo.html`:
the agent loaded the page, clicked through, typed a signup form, submitted,
and verified the confirmation — in a fresh browser and via CDP attach.

`--max-cost-usd` caps the run's estimated model spend (default 0 =
unlimited). The first model call is always issued — there's no estimate
yet — then after each call the run stops *before* the next model call
would exceed the ceiling, with status `blocked` and the ceiling named in
the summary. The ceiling and the stop are recorded in `perform.json`,
`PERFORM.md`, and the ledger.

Env knobs: `QALOOP_RUNS`, `QALOOP_KEEP_RUNS` (keep at most N newest run
dirs after verify/worker writes its report; `--keep-runs` flag wins;
default 0 = keep everything), `QALOOP_DB`, `QALOOP_EXECUTABLE_PATH` (chromium
binary override), `QALOOP_LEDGER` (cost-ledger JSONL path override, default
`runs/ledger.jsonl` under the qaloop checkout), `QALOOP_WEBHOOK_SECRET`, `QALOOP_ENQUEUE_TOKEN`,
`QALOOP_MODEL_*` (base url, name, key, per-1M prices).

## Queue → worker → webhook

The unattended path: GitHub sends `pull_request` events to `/webhook/github`
(verified with `QALOOP_WEBHOOK_SECRET`; the `needs-qa` label also triggers).
The worker claims jobs, boots the repo's env from `repos.yaml`, runs its
flows, writes reports, appends to the ledger, and marks the job done/failed.
`GET /health` shows pending depth. Stale claims (>1h) are requeued.

`enqueue --max-retries N` gives a job up to N extra attempts after the first
failure (default 0 = today's behavior: one failure and the job is terminal).
A job with `--max-retries 2` that fails twice then passes finishes with
3 claims; one that fails three times ends `failed` with attempts=3.

## Investigator

`qaloop/investigate.py` — a ReAct loop with 8 tools
(`ax_snapshot`, `goto`, `click`, `fill`, `press`, `screenshot`, `console`,
`network`, `done`), JSON-in-text protocol (no function-calling API needed),
default budget 20 actions. Every turn and the final diagnosis land in
`INVESTIGATION.md`; token usage and estimated cost go to the ledger.

## Dashboard & ledger

`qaloop dashboard` scans `runs/*/run.json` and writes a static
`dashboard/index.html`: pass counts, run table with report links, and the
cost ledger summary — so you can watch the hybrid working (scripted runs at
~$0, investigations itemized).

## The 15-minute loop's verifier role

`~/workspace/agentic-stone-age/repo-iteration/VERIFIER_BRIEF.md` is the brief
template; `PROMPT.md` v6 inserts the verifier between reviewer-APPROVE and
merge (step 8b), with one shared fix round. The implementer brief now asks
client-facing changes to ship a flow spec.

## Repo layout

```
qaloop/            the framework
  spec.py          YAML loader + validator (schema v1)
  runner.py        deterministic Playwright runner
  perform.py       natural-language browser task agent (reads the AX tree, acts semantically, stops honestly when blocked)
  artifacts.py     screenshots, AX snapshots (CDP), console/network collectors
  report.py        REPORT.md + run.json + junit.xml + stdout card
  env.py           boot static/command targets, wait-for-ready
  queue.py         sqlite job queue
  worker.py        claim → boot → run → report
  webhook.py       GitHub webhook + /enqueue
  investigate.py   bounded agentic investigator
  evaluate.py      semantic judge: does the change actually make sense (claim + diff + run evidence)
  dashboard.py     static HTML dashboard
  ledger.py        JSONL cost ledger
  cli.py           CLI
flows/             flow specs (game-loading-frames.yaml, ...)
repos.yaml         repo presets: env boot + default flows
runs/              run outputs (gitignored-worthy)
dashboard/         generated index.html
```

## Troubleshooting

- **Playwright can't find a browser**: `python3 -m playwright install chromium`.
  Or point at a system build: `QALOOP_EXECUTABLE_PATH=/path/to/chrome`.
- **This VM's bundled Chromium** (`/opt/meta-chromium/chrome`) enforces Local
  Network Access checks that block programmatic navigation to `127.0.0.1`
  from an opaque origin — use the Playwright-downloaded Chromium instead.
- **Flaky flows**: prefer `wait` states over `wait_ms`; re-run once before
  calling it a product bug; quarantine known-flaky assertions with a comment.
- **Auth**: inject session tokens via `seed: {js: ...}` setting localStorage /
  cookies instead of scripting a login every run — unless login is under test.
