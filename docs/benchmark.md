# Benchmark: random vs fixed vs Muse-guided exploration

Generated 2026-11-04T17:26:20.011032+00:00 by `scripts/benchmark.py`. Raw per-trial JSON: `docs/benchmark.json`.

**Setup**
- Equal experiment budget: **8 experiments** per trial.
- Search space: the adversary's catalog. That is agent ∈ {vulnerable, repaired} × trigger tool ∈ {*, get_order, get_refund_status,
  issue_refund} × occurrence 1–4, with one `timeout_after_commit` fault, for **32 cells**.
- **2 cells fail**: ('vulnerable', '*', 2), ('vulnerable', 'issue_refund', 1). This was established by running every cell.
- Targets are the deterministic seeded agents, so every experiment is a real controller run and only the adversary's tokens cost money.
- A failure is a run with verdict `fail` and at least one refund rule violated.

| Strategy | Trials | Found a failure | Mean experiments to first failure | Max | USD (total) |
| --- | --- | --- | --- | --- | --- |
| random (seeded shuffle, seeds 0–19) | 20 | 11/20 | 5.2727 | 8 | 0.000000 |
| fixed (catalog order, vulnerable first) | 1 | 1/1 | 2 | 2 | 0.000000 |
| fixed (same grid, repaired first) | 1 | 0/1 | — | — | 0.000000 |
| Muse-guided (muse-spark-1.3, effort medium) | 5 | 5/5 | 1 | 1 | 0.013621 |

## How to read this (the caveats matter more than the table)

- **The space is tiny and easy.** It has 32 cells and the failure is the textbook one, so this is a sanity check, not evidence that model guidance is better in general.
- **The answer leaks through the names.** The catalog lists the agent variant `vulnerable`. The adversary's system prompt also says to think about "after a write commits but before the caller learns it did".
  - In every Muse trial, the first experiment already hit one of the two failing cells, and both of those are on `vulnerable`. The per-trial cell is not stored in the raw JSON.
  - Neither random nor fixed reads names or prompts.
  - A fair comparison needs opaque variant names and a larger, multi-hook space. We did not run that.
- **Fixed depends entirely on the order.** It took 2 experiments with `vulnerable` first and found nothing within 8 with `repaired` first.
- **Random:** 11/20 seeds found a failure within 8. When it did, it took 5.27 experiments on average.
- **Cost:** Muse's total was USD 0.013621 across 5 trials (ledger estimate from the published prices, 2 model calls per trial).
- **Not benchmarked here:** the harder target, the live LLM agent (`llm-*` variants).
  - One recorded exploration (`demo/recorded/adversary-exploration-llm.json`) tried `llm-naive` first. It passed, because Spark chose a stable key itself.
  - It then tried `llm-naive-noread`, which duplicated, at experiment 2. That is n = 1, so it is anecdotal.
