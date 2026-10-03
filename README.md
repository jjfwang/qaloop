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

## CLI

| command | what it does |
|---|---|
| `validate <flow>` | check spec structure (env vars not required) |
| `verify <flow> [--target URL] [--investigate] [--headed]` | run once, print card, write `runs/<id>/REPORT.md` |
| `enqueue --kind verify-flow --flow F --target U` | queue a one-off verification |
| `enqueue --kind verify-repo --payload '{"repo":"stoneage-reimagined"}'` | queue a repo run (boots env per `repos.yaml`) |
| `worker [--once] [--poll 5]` | claim jobs → boot env → run flows → report |
| `webhook [--port 8090]` | GitHub PR webhook → queue (`/webhook/github`); manual `/enqueue` |
| `investigate --run <dir> --flow <yaml>` | run the investigator on an existing run |
| `dashboard [--out dir]` | regenerate static `index.html` |
| `ledger` | cost summary JSON |

Env knobs: `QALOOP_RUNS`, `QALOOP_DB`, `QALOOP_EXECUTABLE_PATH` (chromium
binary override), `QALOOP_WEBHOOK_SECRET`, `QALOOP_ENQUEUE_TOKEN`,
`QALOOP_MODEL_*` (base url, name, key, per-1M prices).

## Queue → worker → webhook

The unattended path: GitHub sends `pull_request` events to `/webhook/github`
(verified with `QALOOP_WEBHOOK_SECRET`; the `needs-qa` label also triggers).
The worker claims jobs, boots the repo's env from `repos.yaml`, runs its
flows, writes reports, appends to the ledger, and marks the job done/failed.
`GET /health` shows pending depth. Stale claims (>1h) are requeued.

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
  artifacts.py     screenshots, AX snapshots (CDP), console/network collectors
  report.py        REPORT.md + run.json + stdout card
  env.py           boot static/command targets, wait-for-ready
  queue.py         sqlite job queue
  worker.py        claim → boot → run → report
  webhook.py       GitHub webhook + /enqueue
  investigate.py   bounded agentic investigator
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
