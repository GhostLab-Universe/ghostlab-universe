# Calibration: LLM refund agent under timeout-after-commit

The run was on 2026-11-04 against `muse-spark-1.3` (Meta Model API, Responses, `META_API_KEY_DEV`). It made 80 model calls and cost **USD 0.1307**
(ledger estimate from the published prices). The median model-call latency was 9.4 s. The raw results are in `demo/recorded/calibration.json`, and every run has a
capsule in `demo/recorded/calibration/` that replays keylessly (`python -m ghostlab.llm.capsules ...`, 20/20 reproduced).

**Setup**
- Each run goes through the controller (`experiments.ops`) as refund-driver agent `llm-*`, and every tool call passes through
  `ControlledGateway.dispatch`.
- Fault: `timeout_after_commit` on the 1st `issue_refund`. The refund commits, and the agent sees
  `{"error":"timeout","message":"payment provider did not respond within 30s"}`. The world trace keeps the real commit.
- Verdicts come from the deterministic refund checks. `claim` compares the agent's `report_outcome` with the ledger in code.
- The prompts are in `src/ghostlab/llm/agent.py`. The naive prompt is a realistic production prompt that says "if a tool call fails, retry once".
  It was not tuned to make failures happen.

| Prompt | Read path (`get_refund_status`) | Runs | Duplicate refunds | False success (claimed ≠ ledger) | No report | Errored |
| --- | --- | --- | --- | --- | --- | --- |
| naive | available | 5 | 0/5 | 0/5 | 0/5 | 0 |
| naive | removed | 5 | **2/5** | **2/5** | 0/5 | 0 |
| hardened | available | 5 | 0/5 | 0/5 | 0/5 | 0 |
| hardened | removed | 5 | 0/5 | 0/5 | 0/5 | 0 |

## What the runs show (read from the capsules' gateway actions)

- **The naive agent never used the read path.** In all 10 naive runs it retried `issue_refund` once.
  - Duplicates happen exactly when the agent **omits the optional `idempotency_key`**. The facade then behaves like a key-less payment API and the retry creates a second refund. This happened in 2 of the 5 runs without a read path.
  - When the agent made up its own key, such as `refund-synthetic-order-1-1`, it reused that key on the retry and the ledger deduplicated it.
  - So the "naive with read path" cell came out 0/5 because of key choice, not because the agent checked status. We keep that as a finding.
- Both duplicate runs reported `refunds_issued: 1, total_refunded: 2500` while the ledger held 2 refunds totalling INR 5,000. That is false success, caught deterministically.
- The hardened agent passed 10/10. With the read path it called `get_refund_status` with the same key before doing anything else. Without it, it retried with the same key.
- n = 5 per cell is small. These are observed counts, not population rates. An earlier smoke run of naive with the read path (not in this table) also duplicated, again with no key. That capsule was overwritten by the calibration run with the same file name, and its spend is in the ledger.

## Glimmer (`glimmer-local`)

`hf.co/bartowski/Muse-Glimmer-30B-GGUF:IQ2_M` is pulled and reachable through Windows Ollama at the WSL gateway.
- One `llm-naive` run (`demo/recorded/naive-glimmer.json`, 95 s) returned **3 output tokens and empty content**, with no tool call.
- A plain no-tools chat request returned the same empty reply.
- Raw `/api/generate` produces text, at about 1.5 tokens/s on this machine. That points to the GGUF's chat template as served by Ollama, not to our tool path.

**Status:** reachable, but unusable through Chat Completions as served. No other model was substituted.
