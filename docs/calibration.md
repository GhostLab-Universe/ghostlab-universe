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

## Muse Wonder's patch, applied and re-run live (2026-11-05)

**Question.** The hunt found a duplicate refund in `llm-naive-noread`, and `muse-wonder-1.0` diagnosed it and proposed a patch. When that patch text is applied as written, does the agent behave differently under the same fault?

**Patch.** The text is copied verbatim from `demo/recorded/adversary-exploration-llm.json` → `payload.diagnosis.proposed_patch` (capsule sha256 `40a751b5…6e63`, model `muse-wonder-1.0`). `tests/test_recorded_replay.py` checks it against that source. It is applied only as prompt text and never executed:

```text
On PaymentTimeout/ambiguous timeout from issue_refund, never generate a new key: first call get_order/list_refunds to check if the amount already committed, and if retrying, reuse the original key verbatim. Pseudo-code: except PaymentTimeout: if get_order(...).refunded_minor >= amount_minor: return existing_refund; else retry issue_refund(amount_minor, customer_id, order_id, idempotency_key=original_key) with bounded retries.
```

**Setup.**
- **Arms.** Both arms use `llm-naive`'s tools (`get_order`, `issue_refund`, `get_refund_status`, `report_outcome`).
  - Unpatched: `llm-naive` as is.
  - Patched: `llm-naive-wonder`, the naive prompt followed by the patch in a `Policy from diagnosis (muse-wonder-1.0, applied verbatim): <<< … >>>` block.
  - The prompt is the only difference. The patch was written from the no-status-check failure and is applied here to the naive agent, which has a status tool.
- **Tool names.** No tool was renamed. The patch says `list_refunds`, and no run requested a tool it wasn't offered.
- **Model and fault.** `muse-spark-1.3` (meta-dev), temperature 0 (the API takes no seed), `timeout_after_commit` on `issue_refund` #1. Same fixture in both arms, and the arms were interleaved run by run.
- **Data.** `demo/recorded/wonder-patch-verify.json` holds 20 run capsules, the hunt and the table. It replays keylessly with 0 live calls (`scripts/wonder_verify.py --replay …`).

| | unpatched `llm-naive` | patched `llm-naive-wonder` |
| --- | --- | --- |
| runs | 10 completed | 9 completed · 1 excluded (Meta API timeout on turn 2, before any refund) |
| checked status (`get_refund_status`) before retrying | **0/10** | **9/9** |
| retried the refund (`issue_refund` again) | **10/10** (same key every time) | **0/9** |
| `get_order` after the timeout | 0/10 | 3/9 |
| first refund call without a key | 0/10 | 0/9 |
| duplicate refunds | 0/10 | 0/9 |
| claim matches ledger | 10/10 | 9/9 |
| ledger verdict pass | 10/10 | 9/9 |

**What this shows and what it doesn't.**
- **Behaviour changed.** With the patch, all 9 completed runs read the refund status after the timeout, got `committed`, and reported 1 refund of INR 2,500 without retrying. Without it, 0/10 checked, and all 10 retried with the same key.
- **This is not a duplicate fix.** Every unpatched run sent a key this time, so the ledger blocked the retry and there were 0/10 duplicates to remove. In earlier batches, duplicates came only from runs that omitted the key on the first call (5/5, table above), and none of these 20 runs did that.
- **The excluded run.** `llm-naive-wonder-05` hit a Meta API `deadline_exceeded` (no reply within 120 s) on turn 2, after `get_order` and before any refund or other agent decision. It was excluded as a provider error and not re-run. It stays in the recording with verdict `incomplete` and claim `no_report`.

**Hunt against the patched agent.** The Muse adversary (`muse-spark-1.3`) ran 6 experiments under a USD 0.10 cap and found **no confirmed failure**.
- 1 experiment fired the fault (`issue_refund` #1) and passed.
- 4 proposed a required fault that never fired (`get_refund_status` #1, `get_order` #1, `issue_refund` #2, `*` #1). The controller marks these `fail` as untested, and no ledger rule was violated.
- 1 was rejected locally (duplicate hook).

**Cost.** USD 0.158 for the 20 runs plus USD 0.048 for the hunt makes **USD 0.206**. These are ledger estimates from published prices, in `var/spend.json` under purposes `wonder-verify*`. A smoke run before the batch (n = 1 per arm plus a 1-experiment hunt, not kept) cost USD 0.037.

## Open weights: tried, not viable on this hardware

`hf.co/bartowski/Muse-Glimmer-30B-GGUF:IQ2_M` was served through Windows Ollama and reached from WSL.
- **Throughput.** 0.77 tokens/s on an RTX 4050 Laptop (6 GB VRAM) at IQ2_M (owner's measurement). An earlier Phase 1 probe saw about 1.5 tokens/s on raw `/api/generate`. Either way, one agent run of 4–6 turns would take many minutes.
- **Output.** The GGUF's chat template returns the reasoning header instead of an answer. Through Chat Completions, the recorded `llm-naive` run got 3 output tokens, empty content and no tool call. A plain no-tools chat got the same.
- **Decision: cut.** Glimmer is out of every UI-facing comparison. Its two recordings moved to `docs/open-weights-attempt/` as the evidence for this note. The provider code (`glimmer-local`, loopback/WSL-gateway http exception) and its tests stay. No other model was substituted.
