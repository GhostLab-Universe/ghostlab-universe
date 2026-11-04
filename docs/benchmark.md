# Benchmark: random vs fixed vs Muse-guided exploration (Phase 2B, fair version)

Generated 2026-11-04 by `scripts/benchmark.py`. The raw per-trial JSON is in `docs/benchmark.json`. It includes the opaque-id mapping, every Muse trial's chosen cells, local rejections, USD and stop status.

## Method

- **Two environments.**
  - `refund-env`: 5 agents.
  - `booking-env`: 4 agents.
  - Every agent is deterministic. Seven of the nine carry one seeded bug:

| Opaque id | Env | Real agent | Seeded bug |
| --- | --- | --- | --- |
| agent-01 | refund | repaired | none (status check with a stable key) |
| agent-02 | refund | split-key-per-attempt | refunds in two halves and regenerates the key per attempt, which over-refunds |
| agent-03 | refund | retry-nokey | status pre-check, then a key-less retry, so the client auto-generates a key |
| agent-04 | refund | vulnerable | retries with a fresh key |
| agent-05 | refund | status-ignored | reads the status, ignores it, retries with a fresh key |
| agent-06 | booking | repaired | none |
| agent-07 | booking | vulnerable | booking duplicated after a timeout |
| agent-08 | booking | status-ignored | reads the status, ignores it, rebooks with a new key |
| agent-09 | booking | double-submit | double availability check, then resubmits on a slow response with a new key |

- **Opaque ids.** Every strategy saw only `agent-NN`, assigned by a seeded shuffle (`ID_SEED=2026`). Muse saw neither the names nor this table. A real name passed to `run_experiment` is rejected and never executed (this is tested).
- **Neutral prompt.** Muse runs with `adversary.NEUTRAL_SYSTEM`. It frames the task as "agents under test, deterministic checks decide pass/fail" and lists the four tools. It names no fault, invariant, idempotency or timing, and gives no strategy hint. The Phase 1 prompt, which hinted at "after a write commits", is kept only so the old recordings replay.
- **Space.** This is everything both envs support: hook `timeout_after_commit` × trigger tool (`*` + all 3 discovered tools) × occurrence 1–4. The schedule allows **1 fault**, because both worlds fire this hook at most once per run, so a second fault adds nothing. Every strategy samples the same space.
  - refund: 5 × 16 = **80 cells**, of which **10 fail**.
  - booking: 4 × 16 = **64 cells**, of which **6 fail**.
- **Ground truth.** Every cell was run through the controller. The failing cells match the per-variant predictions exactly (`tests/test_benchmark_fairness.py`).
- **Budget.** Every trial gets **10 experiments**. A failure is an executed run with verdict `fail` and at least one rule violated.
- **Strategies.**
  - random: a seeded shuffle of the space, seeds 0–29.
  - fixed: catalog order (agent-01 first) and the same grid with the agent order reversed.
  - Muse: `muse-spark-1.3`, effort medium, 5 runs per env, USD cap 0.6 per trial.

## Results

| Env | Strategy | Trials | Found a failure in ≤10 | Mean experiments to first failure (when found) | Max | Experiments spent per trial | USD total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| refund (80 cells, 10 fail) | random | 30 | 23/30 (exact expectation 0.759 × 30 = 22.8) | 4.57 | 10 | 5.83 mean | 0 |
| | fixed, catalog order | 1 | 0/1 | — | — | 10 | 0 |
| | fixed, reversed | 1 | 1/1 | 2 | 2 | 2 | 0 |
| | Muse-guided | 5 | **5/5** | **2.0** | 2 | 5 (one batch) | 0.0236 |
| booking (64 cells, 6 fail) | random | 30 | 24/30 (exact expectation 0.656 × 30 = 19.7) | 3.92 | 9 | 5.13 mean | 0 |
| | fixed, catalog order | 1 | 0/1 | — | — | 10 | 0 |
| | fixed, reversed | 1 | 1/1 | 3 | 3 | 3 | 0 |
| | Muse-guided | 5 | **5/5** | **2.8** | 6 | 4.8 mean | 0.0247 |

The analytic random expectation is 1 − C(N−F, 10)/C(N, 10). The 30 observed seeds sit at or above it, as expected for n = 30.

## What Muse actually did (from the raw cells)

- **refund, all 5 trials, same move.** In its first batch it called `issue_refund`, occurrence 1, on all five agents. Four of the five failed. The first failure was at experiment 2 only because agent-01 (repaired) happens to come first in id order.
- **booking.**
  - 4/5 trials did the same with `book_equipment` occurrence 1.
  - 1 trial first spent 4 experiments on `*` occurrence 1. That arms the fault on the first call, which is a read, so the fault never fires and the run fails as *untested* with no rule violated.
  - It then switched to `book_equipment` occurrence 1 and found the failure at experiment 6.
- There were 0 local rejections and no errors. Every trial stopped at `found_failure` with a stable minimized prefix.

## How to read this (caveats)

- **Muse beat random and fixed here, but a one-line heuristic would tie it.** That heuristic is "fault the mutating tool, first occurrence, on every agent".
  - Muse did not need names. It inferred the mutating tool from the tool names (`issue_refund`, `book_equipment`) and the hook name `timeout_after_commit`.
  - Every strategy sees the hook name, because it is the env's own catalog. Random and fixed simply don't use semantics.
  - So the measured advantage is *reading tool and hook semantics*, not deep search.
- **Muse spends experiments in batches.** It proposed one experiment per agent at once. "Experiments to first failure" is the index inside that batch, but it spent 4–8 experiments per trial, not 2. The table shows both numbers.
- **The space is still small and synthetic.** It has one hook, and the failing cells cluster on the first commit. None of the seeded bugs needs a later occurrence that Muse failed to find, except split-key-per-attempt's `issue_refund#2`. That variant also fails at #1, so this space never tested whether Muse can find a bug that only appears late.
- **n is small.** There were 5 Muse trials per env, and fixed has a single deterministic order per direction. These are observed counts, not rates.
- **Cost.** Muse spent USD 0.048 for all 10 trials (ledger estimate, published prices, 2–3 model calls per trial). Random and fixed cost nothing.
- **Phase 1's result leaked**, and this run removes the leak: no variant names and no timing hint. The headline result is the same (Muse finds a failure quickly), but the explanation is now visible and modest.
- **Live LLM targets** (`llm-*` agents) are not in this benchmark. They are covered by the calibration in `docs/calibration.md`.
