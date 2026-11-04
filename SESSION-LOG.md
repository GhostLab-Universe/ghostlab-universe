# SESSION-LOG.md — session log

Updated at the end of every session and read at the start of every session. Newest entry is at the top.

---

## Current state

**Updated:** 2026-11-04 (~22:45 IST)
**Branch:** `hackathon/final` (from `origin/feature/isolated-backend` f73f919 + ported providers)
**Agents active:** Claude Code (solo owner Abhiram-0910). The previous co-author is gone and cannot push.
**Status:** The deterministic product works end to end: tests 785 pass with 2 Linux-only failures, tsc is clean,
the build works, and the proofs pass. No live model is wired into the planner yet.
**Next:** Phase 1. Wire a live Meta model into `model_guided` planning (see TODO Now). Feature freeze is 02:00 IST,
and submission closes 07:30 IST on Nov 5.

---

## Sessions

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
