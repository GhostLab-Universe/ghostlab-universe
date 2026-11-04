# SESSION-LOG.md — session log

Updated at the end of every session and read at the start of every session. Newest entry is at the top.

---

## Current state

**Updated:** 2026-11-04 (~23:05 IST)
**Branch:** `hackathon/final`
**Agents active:** Claude Code (solo owner Abhiram-0910).
**Status:** Live Meta models are wired in.
- A Muse-driven refund agent runs under test through the controller and gateway.
- A Muse adversary runs as the `model_guided` explorer.
- Wonder diagnosis, a spend ledger, keyless replay of every recording, and API routes are all in place.
- Tests: 813–815 pass. Failures are pre-existing and also seen at c1c5040: the 2 Linux-only ones, the api_e4 body-cap race, and the timing-flaky api_e4 budget_409. tsc and the build are clean.
- Spend is USD 0.2209 on dev, 0 on public.

**Next:** UI for the new routes (step 6 explorer), README rewrite, and the public deploy. Feature freeze is 02:00 IST.

---

## Sessions

### 2026-11-04 — Phase 2B: honest evidence (eval/fairness worktree) — *Claude Code, session 2B*

**Done**
- **Ledger-enforced session cap.** `GHOSTLAB_DEV_CAP_USD` can only lower the dev cap. It was set to start + 8 USD. The session spent USD 0.37: calibration 0.32 and benchmark 0.048.
- **Four new seeded-bug deterministic agents:** `status-ignored` (refund + booking), `retry-nokey`, `split-key-per-attempt` and `double-submit`.
  - Each agent's failing cells were verified exhaustively.
  - `plan` now validates non-refund fixtures through the env driver, so booking runs go through `execute_plan`.
- **Adversary.**
  - New `NEUTRAL_SYSTEM` prompt.
  - Opaque `agent-NN` aliases; real names are rejected and never echoed.
  - Env profiles, so booking can be explored.
  - `max_faults`.
  - The defaults are unchanged, so the old explorations still replay.
- **Fair benchmark** (`docs/benchmark.md`): budget 10, refund has 80 cells (10 fail) and booking has 64 (6 fail).
  - Muse found a failure in 5/5 trials per env at mean experiment 2.0 (refund) and 2.8 (booking).
  - Random found one in 23/30 and 24/30 trials.
  - Fixed found nothing in catalog order and found one when reversed.
  - Caveat: the "mutating tool, occurrence 1" heuristic ties Muse.
- **Calibration 2B** (n=20/20/10, `docs/calibration.md`).
  - Duplicates: naive/no-read 2/20, naive/read 1/20, hardened/no-read 0/10.
  - Pooled with Phase 1: a key omitted on the first call led to a duplicate in 5/5 runs. A supplied key led to a duplicate in 0/65.
  - All 75 capsules and both explorations replay keylessly.
- **Glimmer cut.** Its recordings moved to `docs/open-weights-attempt/`. `/v1/models/compare` still returns 200 with `run: null`.

**Decided**
- The benchmark uses 1 fault per schedule, because both worlds fire the hook at most once per run. It is run per env.
- No edits under `api/` or `web/`. Dropping the compare route's `glimmer` key is a merge step (TODO).

**Hit**
- Sequential calibration was about 1 run/min. It was split into parallel workers writing into one batch dir with exclusive create. One stopped worker's in-flight run spent tokens without a capsule: the ledger has 206 calibration calls against 201 in the capsules.
- `test_drivers_checks_t2` hard-codes the driver agent lists. It was updated.

**Incomplete**
- The UI does not show the new benchmark or calibration tables. That belongs to the UI session on hackathon/final.

---

### 2026-11-04 — Phase 1: live Meta models — *Claude Code*

**Done**
- `src/ghostlab/llm/` contains:
  - `spend`: ledger plus caps.
  - `client`: the Responses and Chat clients on `providers/http`, plus Replay and Recording clients.
  - `agent`: the `llm-*` refund agents plus the claimed-vs-actual check.
  - `capsules`: keyless replay.
  - `adversary`: the ModelExplorer bridge, explore loop and diagnosis.
- `glimmer_local` is the only provider allowed to use http, and only on loopback or the WSL gateway.
- The refund driver, configs and controller now know the `llm-*` agents, and `model_calls` is recorded from the driver.
- API routes are in `api/llm_routes.py`, documented in `docs/api.md`.
- Live results:
  - Calibration on Spark (`docs/calibration.md`): naive-noread duplicated 2/5 and claimed success both times. The other cells had 0 duplicates. Cost USD 0.14.
  - Adversary vs deterministic agents: found_failure in 1 experiment. The Wonder diagnosis was followed by a repaired rerun that passed.
  - Adversary vs live LLM agents: found_failure at experiment 2 (`llm-naive-noread`).
  - Benchmark: `docs/benchmark.md`, with caveats.
- All recordings are in `demo/recorded/`, and all 25 replay with 0 live calls.

**Decided**
- The LLM agent is a controller agent, not a separate runner. The client is reused from `providers/http`, not the openai SDK.
- The agent sees a realistic timeout message. See ARCHITECTURE Decisions.

**Hit**
- Glimmer IQ2_M via Ollama returns empty chat replies (3 tokens), both with and without tools. Raw `/api/generate` works at about 1.5 tok/s, so this is
  a chat-template issue. It is reported as unusable as served, and nothing was substituted.
- `tests/test_api_e4.py::test_body_cap_json_and_no_registration` fails on WSL even at c1c5040, because of a broken-pipe race.
- The smoke-run capsule `calibration/llm-naive-0.json`, which was a duplicate, was overwritten by the calibration run with the same name. Its spend is still in the ledger.

**Incomplete**
- The web UI does not call the new routes yet.
- The benchmark space is tiny and the variant names leak the answer, so a fair comparison needs opaque names and more hooks.

---

### 2026-11-04 — Phase 0: inventory, branch, docs — *Claude Code*

**Done**
- Fetched every remote branch. `origin/feature/isolated-backend` contains all 10 handover commits
  (a5d3e9d … c40a41f), and `feature/byoe-config-experiments` is a strict ancestor of it.
- Created `hackathon/final` from the handover tip.
- Ported `src/ghostlab/providers/`, `src/ghostlab/security/`, `scripts/probe_provider.py` and `tests/providers/`
  verbatim from PR #49's tip `9f5162d`. These are new paths only. 128/128 provider tests pass, and the probe dry run makes 0 calls.
- `.gitignore`: added `.code-review-graph/` and `var/`.
- Docs moved to the template kit: `AGENTS.md` (all rules kept, plus the budget and no-paid-services deny rule), `CLAUDE.md`,
  `ARCHITECTURE.md` (renamed from `architecture.md`), `SESSION-LOG.md` and `TODO.md`. HANDOVER moved to
  `docs/handover/`, and the concept brief and research prompts were added. Team-era `setup-status.md` and
  `CONTRIBUTING.md` were removed.
- Ran `code-review-graph build`.
- Verified on WSL (Python 3.13.14):
  - pytest: 785 pass, 2 fail (Linux-only).
  - unittest discover without docker_b4: 281/283.
  - tsc 0 errors, vite build OK.
  - walkthrough OK, backend_acceptance 9/9, booking_service demo OK.

**Decided**
- Base = handover line. PR branches (#49, #24, #22, #1) were not merged, because they diverged at `10c3a3b` and
  carry 2–11 conflicts each with duplicate api/experiments/web designs. Only the provider and security modules were
  taken, because they are new paths. Rejected: merging #49 for its optional sign-in and GitHub inspection, which are tied
  to that line's API. Leaving them out keeps the app free of any login wall.
- Live budget confirmed: DEV ≤ USD 120 and PUBLIC ≤ USD 50 (public deploy only). Ollama is free. Every call is recorded.

**Hit**
- uv 0.12.7 is below the pin of `>=0.13.6`, so we use `uv --no-config`. Node 22 is below the engine requirement of 24, which only causes a warning.
- Two tests fail on Linux only (`LC_CTYPE` env leak, POSIX `sub/..` traversal). They are logged in TODO Bugs.
- HANDOVER says live inference is unproven. That is true on this line, but the integration line's
  `docs/modules/M01.md` records a Meta probe that passed 5/5 on `muse-spark-1.3` on 2026-11-01.
- `docs/modules/backend-final-report.md` was never written, and HANDOVER admits this.

**Incomplete**
- README is still stale. It describes the Oct 29 core and team, and only its links were fixed. It needs a rewrite before submission.

---

### 2026-10-29 → 2026-11-03 — Prior build (on the co-author's machine) — *Claude Code*

Summarised from `docs/handover/HANDOVER.md`.

**Done**
- Refund core: synthetic INR 2,500 refund world, timeout-after-commit, vulnerable and repaired agents, independent
  checks, Reality Diff, deterministic replay and the idempotency repair (`src/ghostlab/core.py`).
- Track A `a5d3e9d`: environment registration, data-only manifest import, capability discovery, explicit
  qualification and versioned reports (`envs/`).
- Track B `7e036e0`: target-agent gateway with limits, cancel and version capture, plus the equipment-booking external example.
- Track C `643ba16`: definition vs immutable resolved configs, project-scoped persistence, controller and comparison.
- Track D `927160a`: guided 7-step UI, developer guide and browser acceptance.
- Integration `841b1fe`: the booking world runs through the shared env pipeline with no core edits.
- E1–E4:
  - late fault arming
  - loopback boundary proxy
  - occurrence triggers, minimization and regression export
  - fidelity manifests
  - live service layer
- Dashboard fix `9412e3c`/`5ea1a4d`: service-first UI. Fixtures are dev-only and never used as a fallback.
- Testing pipeline (`b855bd7` … `83df9b3`):
  - frozen contracts
  - defective-adapter battery
  - domain-independent checks
  - bounded exploration planner
  - booking end-to-end proof
- Isolated backend `0e26730`, `c40a41f`, `f73f919`: threat model, `ExecutionBackend`, local and Docker backends,
  wiring, and the handover doc.
- A parallel integration line (`architecture-and-refund-core`, PRs #1/#22/#24/#49) built M01 providers, M07 API,
  orchestration, storage, React views and a deploy package. It was never merged with the handover line.

**Decided**
- Contracts first, with one integration owner. Data-only manifests. Digests identify and never authenticate. Event-triggered
  faults instead of sleeps. No silent fallbacks. Deterministic first, with live inference only behind a confirmed budget. See `ARCHITECTURE.md`.

**Hit**
- The UI showed copied fixture results as real, which was fixed on real routes.
- Docker B4 daemon tests were still failing at handover:
  - a test-side `self.start(...)` bug
  - a forged-output rejection that emits no `failed` event
  - unverified adversarial subtests

**Incomplete**
- The Docker B4 daemon tests, the final trust-boundary report, the independent security review of `backends/`, and the merge decision.
  The merge was resolved in Phase 0 above.
