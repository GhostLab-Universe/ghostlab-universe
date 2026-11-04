# AGENTS.md

Cross-tool rules for GhostLab Universe. Read by Claude Code, Antigravity, OpenCode and other
AGENTS.md-aware agents at session start. This file holds standing rules. Session history lives
in `SESSION-LOG.md`.

Read `ARCHITECTURE.md` and `mvp-plan.md` before making architectural changes. The full cited decisions are in
`docs/architecture-decisions.md`. When evidence changes a decision, update that record, including what
was rejected and why.

---

## Stack

- Python 3.13 (`.python-version` pins 3.13.18; any 3.13.x works). Install extras `contracts`, `service`,
  `orchestration` and the `pytest` dev dependency. The simulation core is standard-library only.
- FastAPI/Pydantic service boundary (`src/ghostlab/api/`). LangGraph StateGraph with custom provider
  nodes. LangChain components are optional, and there is no second orchestration framework.
- Web: Vite + TypeScript. React is used through `React.createElement` in plain `.ts`, with no JSX
  plugin. Node ≥ 24.22.1 is declared, but 22.x builds with an EBADENGINE warning.
- SQLite for the serial local MVP. Postgres is only for a hosted multiworker service, which is cut for the hackathon.
- Primary inference is direct Meta Responses (`src/ghostlab/providers/meta_responses.py`), and it
  preserves the documented opaque continuation state. The conditional Baseten Chat Completions
  fallback uses its own adapter. Changing provider counts as an explicit new experiment.
- Graph working state, structured experiment findings and authoritative evidence are kept separate.
  There is no vector database initially.
- Only trusted, checked-in local adapters run. There is one `ExecutionBackend` interface
  (`src/ghostlab/backends/`).
- Engine, tool and world enforcement is mandatory. NeMo Guardrails is optional. OpenShell is deferred from the
  Windows critical path.

These are decisions, not claims that every component exists. The deterministic core may use
standard-library types. Add Pydantic only at model and service boundaries, and do not couple replay to an LLM library.

## Commands

WSL, run from the repo root. The venv lives at `.venv`.

```
setup:      uv --no-config venv --python 3.13 .venv
            uv --no-config pip install --python .venv/bin/python -e '.[contracts,service,orchestration]' pytest==9.2.0
            (cd web && npm ci)
test:       PYTHONPATH=src .venv/bin/python -m pytest -q tests --ignore=tests/test_backend_docker_b4.py
test (unittest; skips tests/providers, INCLUDES docker_b4 which needs the daemon and is slow):
            PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
typecheck:  cd web && npx tsc --noEmit -p tsconfig.app.json
build:      cd web && npx vite build
service:    PYTHONPATH=src .venv/bin/python -m uvicorn ghostlab.api.app:create_app --factory --host 127.0.0.1 --port 8917
proofs:     PYTHONPATH=src .venv/bin/python examples/equipment_booking/walkthrough.py        # failure + fix
            PYTHONPATH=src .venv/bin/python examples/equipment_booking/backend_acceptance.py # 9/9 stages
            PYTHONPATH=src .venv/bin/python examples/booking_service/demo.py                 # proxy proof
probe:      PYTHONPATH=src .venv/bin/python scripts/probe_provider.py --provider meta_responses   # dry run, 0 calls
ui accept:  node web/tests/acceptance.mjs --service=http://127.0.0.1:8917
lint:       none configured
```

## Conventions

### Product proof
- The refund world comes first. It uses a synthetic INR 2,500 order, a timeout after commit, a vulnerable retry,
  independent detection, Reality Diff, deterministic replay and an idempotency repair. Synthetic fixtures are
  intentional, but mutations and checks must actually execute.
- Do not claim live inference, real payments, security enforcement or benchmark superiority without evidence.
- An authoritative verdict comes only from executable checks over state evidence. Model text is explanation, never evidence.

### Compatibility gates
- Direct Meta documentation identifies `https://api.meta.ai/v1`, `MODEL_API_KEY` and `muse-spark-1.3`.
  Baseten documents `https://inference.baseten.co/v1/chat/completions`, `BASETEN_API_KEY` and
  `meta-models/muse-spark-open`. The hackathon `meta-models/muse-spark-open-1026` comes from the
  announcement and needs an authenticated access check. Never invent or silently substitute model identifiers.
- Documented extensibility is not the same as live compatibility. Before exploration, record sanitized proof of
  model access, tool-call/result pairing, schema validation, continuation, usage, timeout/cancel/rate-limit
  handling and budgets (`scripts/probe_provider.py`). Enable streaming only after the partial-argument and
  interruption tests pass.
- Keys stay out of Git, browser code, prompts, capsules and test logs.

### Live inference budget (confirmed 2026-11-04 by the owner)
This is the "explicit confirmed budget" that live inference requires.
- `META_API_KEY_DEV`: at most **USD 120 total**, for local development and probes.
- `META_API_KEY_PUBLIC`: at most **USD 50 total**, used **only** by the public deploy.
- Local Muse Glimmer through Ollama costs nothing and has no budget limit.
- Every live call records model, provider, key label (`dev`/`public`/`ollama`, never the key itself),
  input and output tokens, a cost estimate and the provider request id.
- Stop and ask before any action that would go past either cap.

### Invariants and replay
- The controller owns ordering, evidence and pass/fail. Model proposals and tool text are untrusted data.
  Validate allowlisted tool names and parameters before dispatch. The first tools are `get_order`, `issue_refund`
  and `get_refund_status`. Host boundaries and customer-ownership checks stay enabled while vulnerable
  business behaviour is being tested.
- Baseline and faulted runs start from the same immutable fixture in independent state. LangGraph checkpoints
  do not snapshot external worlds. Graph replay can repeat model calls and side effects. Failure Capsule replay
  uses recorded decisions and order, or a deterministic adapter, without new inference. A fresh model call is a new experiment.
- A required fault that never fired means the run is untested, never a pass. There are no silent fallbacks.
  An unknown or unavailable backend is refused (HTTP 503), and an unsupported capability gets a structured
  validation error. The dashboard shows real execution or an error, never fixture data as a fallback.
- Manifest import is data only. It installs no package, executes no code, and accepts no arbitrary URLs or paths.
  A digest identifies content and never authenticates it.

### Delivery
- Resolve dependencies together in a clean environment, run meaningful tests and commit a lockfile before
  claiming a supported combination. Do not invent latest pins. Keep optional integrations outside the engine.
- Share contracts before making parallel edits. Use separate temp DBs per test area, and never share live SQLite files.
- Reopen a decision only for a failed probe, a changed requirement or a demonstrated limitation.
- Any sign-in must stay optional. Judges never hit a login wall.
- Commits use conventional messages and named `git add <path>` only.

### Rejected or deferred
- No second agent harness (LangChain `create_agent`, CrewAI, AutoGen). The explicit graph owns control flow.
- No vector database until structured retrieval shows a gap.
- NeMo policies do not replace isolation. OpenShell's Windows limitations exclude it as an MVP prerequisite.
- Local subprocesses and default containers are not a security boundary for unknown code. Uploaded agents are deferred
  until network, filesystem, resource, secrets and evidence boundary tests pass on one qualified backend.

## Deny rules

- Never read, print or commit `.env` or any file containing a secret. Only the key **names** may appear in code and docs.
- Never add AI-attribution trailers (`Co-Authored-By` etc.) to commit messages.
- No paid services and no card-required signups. The only spend is the two Meta keys within the budget above.
- No live inference outside the confirmed budget, and no live call without the recording above.
- No fake data, placeholder model output or simulated live behaviour in anything that is demoed.
- Never claim a "secure sandbox" or qualified isolation from Docker startup alone.
- Never touch other sessions' branches, worktrees or running processes.

## Files not to touch

- `schemas/v1/` and `schemas/typescript/contracts.ts`: frozen contracts. Regenerate with `npm --prefix web run contracts:generate` only after a deliberate contract change.
- `uv.lock`: change it only by re-resolving dependencies together.
- `docs/handover/HANDOVER.md`: the historical record. Correct it in `ARCHITECTURE.md` or `SESSION-LOG.md` instead.
- `artifacts/local/`: git-ignored run output and probe reports.

## Multi-agent

More than one agent may work this project. Before starting:

1. Read `SESSION-LOG.md` to see what is in progress and which agent is doing it.
2. Work on a **separate git worktree or branch**. Never share a working tree.
3. Record what you did in `SESSION-LOG.md`, including which agent you are.

## Skills and tools

- Knowledge graph: `code-review-graph build` (already run, output in the ignored `.code-review-graph/`).
  Query it to narrow scope before reading files.
- Global skills directory, plus Playwright CLI for browser verification.
