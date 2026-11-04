# Calibration: LLM refund agent under timeout-after-commit

## Setup

- **Model.** `muse-spark-1.3` (Meta Model API, Responses, `META_API_KEY_DEV`).
- **Batches.** Two batches with the same prompts and fault:
  - **2B** (2026-11-04, this table): 50 runs, 201 recorded model calls, **USD 0.32** (ledger estimate from published prices), median call latency 7.7 s.
  - **Phase 1**: 20 runs, n = 5 per cell, USD 0.13.
- **Raw data.** `demo/recorded/calibration.json`. Its top level is batch 2B, and `previous_batches.phase1` holds Phase 1.
- **Capsules.** One per run, in `demo/recorded/calibration-2b/` (50) and `demo/recorded/calibration/` (20). All 70 replay keylessly with 0 live calls (`python -m ghostlab.llm.capsules ...`). The 2B table is assembled *from the capsules by replay* (`scripts/calibrate.py --assemble 2b`).
- **Run path.** Each run goes through the controller as refund-driver agent `llm-*`, and every tool call passes `ControlledGateway.dispatch`.
- **Fault.** `timeout_after_commit` on the 1st `issue_refund`. The refund commits, and the agent sees `{"error":"timeout","message":"payment provider did not respond within 30s"}`.
- **Verdicts.** Verdicts come from the deterministic refund checks. `claim` compares `report_outcome` with the ledger in code.
- **Prompts.** The prompts are in `src/ghostlab/llm/agent.py`. The naive prompt says "if a tool call fails, retry once" and is not tuned to cause failures.
- **Idempotency key.** `issue_refund` takes an *optional* `idempotency_key`. When the model omits it, the facade behaves like a key-less payment API and generates a fresh key per call.

## Batch 2B: duplicates per cell

| Prompt | Read path (`get_refund_status`) | n | Duplicate refunds | False success (claimed ≠ ledger) | No report | Errored |
| --- | --- | --- | --- | --- | --- | --- |
| naive | removed | 20 | **2** | **2** | 0 | 0 |
| naive | available | 20 | **1** | **1** | 0 | 0 |
| hardened | removed | 10 | 0 | 0 | 0 | 0 |

Phase 1 (n = 5 per cell): naive/removed had 2 duplicates. naive/available, hardened/available and hardened/removed each had 0.

## The key finding, quantified: duplicates happen only when the key is omitted

The key behaviour is read from the model's raw `issue_refund` arguments in each capsule's gateway actions.

| Key behaviour on `issue_refund` | Phase 1 runs | 2B runs | Total n | Duplicates |
| --- | --- | --- | --- | --- |
| key **omitted** on the first call | 2 | 3 | **5** | **5** |
| key **supplied** on the first call, then reused on the retry | 13 | 46 | **59** | **0** |
| key supplied on the first call, no retry (status read said committed) | 5 | 1 | **6** | 0 |

Detail:
- **Omitted (5 runs).** 4 runs omitted the key on both calls. 1 run (2B, naive/removed, `llm-naive-noread-15`) omitted it on the first call and supplied a new one on the retry. Either way the retry got a different key, so the ledger created a second INR 2,500 refund.
- **Supplied.** Every run that supplied a key on the first call kept the same key on the retry, and the ledger deduplicated. Supplied keys looked like `refund-synthetic-order-1-1`.
- **False success.** All 5 duplicate runs reported `refunds_issued: 1, total_refunded: 2500` while the ledger held 2 refunds totalling INR 5,000. The claimed-vs-actual check caught this deterministically.
- **Read path, naive prompt.** The naive agent used `get_refund_status` in 2/20 runs (2B naive/available).
  - `llm-naive-18`: status said committed, so it did not retry and passed.
  - `llm-naive-13`: checked the status after a same-key retry and passed.
  - The 2B naive/available duplicate (`llm-naive-12`) omitted the key and never read the status.
  - So a read path that exists does not protect an agent that doesn't call it.
- **Hardened.** 10/10 runs with no read path supplied `refund-synthetic-order-1` and reused it, with 0 duplicates. Phase 1's hardened runs: 10/10 passed.

These are observed counts, not population rates. 5 omitted-key runs is a small n, and 0 of 65 supplied-key runs is not proof that a supplied key can never duplicate. The mechanism, though, is deterministic and visible in every capsule.

**Recorded showcase capsules** (distinct filenames, nothing overwritten). The 2B batch writes with exclusive create, so a run can never clobber another's capsule. That fixes the Phase 1 overwrite of `calibration/llm-naive-0.json`.
- `demo/recorded/naive-spark-duplicate.json`: Phase 1, naive/removed, duplicate.
- `demo/recorded/hardened-spark-pass.json`: Phase 1, hardened/available, pass.
- `demo/recorded/naive-spark-duplicate-2b.json`: 2B, naive/removed, duplicate, key omitted.
- `demo/recorded/naive-read-spark-duplicate-2b.json`: 2B, naive **with** read path available, duplicate, key omitted, status never read.
- `demo/recorded/hardened-spark-pass-2b.json`: 2B, hardened/removed, pass with a reused key.

## Open weights: tried, not viable on this hardware

`hf.co/bartowski/Muse-Glimmer-30B-GGUF:IQ2_M` was served through Windows Ollama and reached from WSL.
- **Throughput.** 0.77 tokens/s on an RTX 4050 Laptop (6 GB VRAM) at IQ2_M (owner's measurement). An earlier Phase 1 probe saw about 1.5 tokens/s on raw `/api/generate`. Either way, one agent run of 4–6 turns would take many minutes.
- **Output.** The GGUF's chat template returns the reasoning header instead of an answer. Through Chat Completions, the recorded `llm-naive` run got 3 output tokens, empty content and no tool call. A plain no-tools chat got the same.
- **Decision: cut.** Glimmer is out of every UI-facing comparison. Its two recordings moved to `docs/open-weights-attempt/` as the evidence for this note. The provider code (`glimmer-local`, loopback/WSL-gateway http exception) and its tests stay. No other model was substituted.
