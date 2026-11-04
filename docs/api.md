# Live-model API (Phase 1)

These routes extend the existing service in `src/ghostlab/api/app.py`. The live-model routes are in `src/ghostlab/api/llm_routes.py`.

Start the service with `PYTHONPATH=src .venv/bin/python -m uvicorn ghostlab.api.app:create_app --factory --port 8917`. Keys are read only from the
environment: `META_API_KEY_DEV`, `META_API_KEY_PUBLIC` and `GLIMMER_BASE_URL`. No route returns a key value. When there is no key, the
default is keyless replay of the recordings committed in `demo/recorded/`.

Exploration state lives in process memory, the same as run groups, so a restart drops it.

Errors use the service-wide shape `{"error": code, "detail": text}`:

| Status | Code |
| --- | --- |
| 400 | `invalid_request` |
| 404 | `not_found` |
| 409 | `not_resumable` |
| 503 | `provider_unavailable` |

## `GET /v1/providers`
```json
{"providers": [{"provider": "meta-dev", "status": "available", "key_label": "dev"},
               {"provider": "meta-public", "status": "unavailable", "reason": "META_API_KEY_PUBLIC not set"},
               {"provider": "glimmer-local", "status": "available", "key_label": "glimmer"}]}
```
When a provider is unavailable, the reason is given. GhostLab never substitutes another provider or model.

## `POST /v1/explorations`
Starts a Muse adversary exploration. Every field is optional.
```json
{"mode": "live|replay", "provider": "meta-dev|meta-public", "max_experiments": 12, "max_usd": 1.0,
 "llm_targets": false, "diagnose": true, "recording": "adversary-exploration.json"}
```
- `mode` defaults to `live` when the provider key is present and `replay` otherwise. Asking for live without a key returns 503.
- `max_experiments` is 1–25 and `max_usd` is in (0, 3]. With `llm_targets: true`, the experiments cap is 6.
- `recording` (replay only) must be a name listed by `GET /v1/capsules`.

Response:
```json
{"exploration_id": "exp-…", "mode": "replay", "status": "running", "provider": "replay",
 "recording": "adversary-exploration.json"}
```

## `GET /v1/explorations/{id}`
Poll this until `status` is `stopped`.
```json
{"exploration_id": "exp-…", "mode": "live", "provider": "meta-dev", "status": "running|stopped",
 "stop_status": "found_failure|coverage_complete|budget_exhausted|paused_on_error|model_stopped|null",
 "experiments": [{"run_id": "run-000001", "agent": "vulnerable",
                  "faults": [{"hook": "timeout_after_commit", "params": {}, "required": true,
                              "trigger": {"type": "occurrence", "tool": "issue_refund", "occurrence": 1}}],
                  "outcome": "executed|rejected|failed", "reason": null, "status": "completed",
                  "verdict": "pass|fail|incomplete", "rules_failed": ["no_duplicate_full_refund"],
                  "faults_fired": [true], "tool_calls": 3, "model_calls": 0,
                  "reality_diff": {"first_divergence": {"event_id": 2, "baseline": {"tool": "issue_refund", "outcome": {}},
                                                        "faulted": {"tool": "issue_refund", "outcome": {}}},
                                   "baseline_refunded_minor": 250000, "faulted_refunded_minor": 500000,
                                   "violated_invariants": ["total_refund_within_payment", "no_duplicate_full_refund"]}}],
 "budget": {"usd_spent": 0.0045, "usd_cap": 1.0, "tokens": 2693, "max_tokens": 200000,
            "experiments": 1, "max_experiments": 12},
 "model_calls": [{"provider": "meta-dev", "model": "muse-spark-1.3", "request_id": "resp_…",
                  "latency_ms": 5123, "input_tokens": 900, "output_tokens": 300, "reasoning_tokens": 210,
                  "usd": 0.0024, "status": "tool_calls", "replayed": true}],
 "local_rejections": [{"tool": "run_experiment", "arguments": {}, "reason": "hook 'x' is not in the catalog"}],
 "ops_rejections": 0,
 "failure": {"run_id": "run-000001", "rules_failed": ["no_duplicate_full_refund"],
             "minimized": {"required_prefix": ["get_order", "issue_refund", "issue_refund"], "dropped": 0, "stable": true, "actions": []}},
 "diagnosis": {"label": "explanation, not evidence", "model": "muse-wonder-1.0", "root_cause": "…", "proposed_patch": "…",
               "rerun": {"agent": "repaired", "run_id": "run-000003", "verdict": "pass", "rules_failed": [], "evidence": "controller verdict"}},
 "reproduced": true, "final_text": "…", "error": null, "created_at": "…"}
```
- `verdict` and `rules_failed` come only from the controller's deterministic checks.
- `diagnosis` is model text and is always labelled `explanation, not evidence`. The evidence is `diagnosis.rerun`.
- `reproduced` is set only for replays.
- `replayed: true` marks a model call served from a recording, which costs 0 USD.

Each experiment also carries `hypothesis`, which is the adversary's text from the turn that proposed it, and `proposed_by`, which is that turn's
`model`, `provider`, `request_id`, `latency_ms`, tokens, `usd` and `replayed` flag. A replay reports its recorded latency and cost, and it is not billed.
In replay, `recorded_budget` is the budget of the original live run.

## `GET /v1/explorations/{id}/runs/{run_id}`
Fork Reality detail for one experiment. This returns 409 `not_ready` while the exploration is running, and 404 for an unknown run.
```json
{"run_id": "run-000002", "agent": "llm-naive-noread", "faults": [{"hook": "timeout_after_commit", "trigger": {}}],
 "baseline": {"run_id": "…", "verdict": "pass", "ledger": [], "order": {}, "total_refunded_minor": 250000, "trace": [{"event_id": 1, "tool": "get_order", "arguments": {}, "outcome": {}}]},
 "faulted": {"…": "same shape"}, "first_divergence_event_id": 2,
 "violated_invariants": ["no_duplicate_full_refund"],
 "claim_check": {"verdict": "false_success", "claim": {"refunds_issued": 1, "total_refunded": 2500}, "actual": {"refunds_issued": 2, "total_refunded_minor": 500000}}}
```
`baseline` is the no-fault run of the same agent on the same fixture. `claim_check` applies only to the `llm-*` agents and is null for the seeded agents.
It is also null when the agent's run log cannot be matched to the run order, because a missing claim is better than a wrong one.

## `GET /v1/evidence`
Read-only. It returns `{"calibration": {…, "table"}, "calibration_notes": "<markdown>", "benchmark": {"budget", "space_cells", "failing_cells",
"strategies": {"random": {"trials", "discovered", …}}}, "benchmark_caveats": "<markdown>"}`.
The data is read at request time from `demo/recorded/calibration.json` and `docs/benchmark.json`. The notes are quoted verbatim from
`docs/calibration.md` and `docs/benchmark.md`.

## `POST /v1/explorations/{id}/resume`
Resumes a stopped exploration whose `stop_status` is `paused_on_error`. Otherwise it returns 409 `not_resumable`.
Response: `{"exploration_id", "status": "running"}`.

## `GET /v1/calibration`
Returns the recorded calibration of the LLM agent under test (`scripts/calibrate.py`).
```json
{"generated_at": "…", "model": "muse-spark-1.3", "provider": "meta-dev", "fault": {"hook": "timeout_after_commit"},
 "usd_spent": 0.2, "model_calls": 80,
 "table": [{"variant": "llm-naive", "prompt": "naive", "read_path": "read", "runs": 5, "errored_runs": 0,
            "duplicates": 0, "false_success": 0, "no_report": 0}],
 "runs": [{"variant": "llm-naive", "capsule": "demo/recorded/calibration/llm-naive-0.json", "run_id": "…",
           "status": "completed", "verdict": "pass", "duplicate": false, "claim": "match",
           "model_calls": 4, "error": null, "stop": "reported", "tools": ["get_order", "issue_refund", "…"]}]}
```
`claim` is the deterministic comparison of the agent's `report_outcome` against the ledger. It is `match`, `false_success` or `no_report`.

## `GET /v1/capsules`
`{"capsules": ["adversary-exploration.json", "calibration/llm-naive-0.json", …]}`. These are the only names replay accepts.

## `POST /v1/capsules/replay`
The request body is `{"name": "<listed name>"}`. Replay is keyless and makes zero network calls.
- An LLM-run capsule returns `{"name", "kind": "ghostlab-llm-run/1", "reproduced", "matches": {"evidence", "verdict",
  "claim_check", "status", "all_turns_used"}, "verdict", "claim_check", "duplicate_refund", "model_calls_replayed",
  "model_calls_live": 0, "model_log", "summary"}`.
- An exploration recording returns `{"name", "kind": "ghostlab-exploration/1", "reproduced", "matches", "summary", "diagnosis",
  "model_calls_live": 0}`.

## `GET /v1/models/compare`
Spark's recorded latency and cost on the calibration task (`llm-naive` under timeout-after-commit).
```json
{"task": "…", "spark": {"model": "muse-spark-1.3", "provider": "meta-dev", "runs": [], "median_latency_ms": 5000, "usd_per_call": 0.002}}
```
There is no Glimmer comparison. The open-weights attempt was cut, and its recordings are in `docs/open-weights-attempt/`.

## `GET /v1/spend`
`{"totals": {"dev": {"calls", "usd", "input_tokens", "output_tokens", "cap_usd"}}, "caps_usd": {"dev": 120, "public": 50, "glimmer": null},
"prices_usd_per_mtok": {}, "prices_fetched": "2026-11-04"}`. These totals come from `var/spend.json`, which is git-ignored.
