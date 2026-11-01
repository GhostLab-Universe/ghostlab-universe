# Mandatory acceptance gates

Updated 1 November 2026. M07 and M08 are merged at integration `d1fe76f`.
Application acceptance combines that integration with pending M09 `291a0cc`.
This candidate does not merge or replace the M09 branch. A candidate test result is not
an accepted release. See [application acceptance](modules/APPLICATION-ACCEPTANCE.md).

These five gates come from GhostLab's existing plan. Each one is tracked
**planned → implemented → integration-tested**. **Live-tested** is tracked
separately wherever it applies. A gate is only as complete as its
least-complete requirement.

- **Implemented:** the code exists and module tests cover it. Tests that use
  in-memory fakes count here.
- **Integration-tested:** committed tests exercise the requirement across
  modules, using the real M04 SQLite stores and the real M01 adapter (with a
  mocked transport).
- **Live-tested:** run against the real provider after explicit authorization,
  with a sanitized report. Mocked responses are never labelled live.

**Use policy:**

- Offline development continues before the live gate passes.
- **Customer-connected use waits** for gates 1–4 wherever they apply, plus
  gate 5's live evidence.
- Until authentication exists (gate 2), the application stays inside the
  documented local-development boundary: loopback only, explicit opt-in, and
  never presented as production authentication.

## Summary

| # | Gate | Owners | Status | Live-tested |
| --- | --- | --- | --- | --- |
| 1 | World enforcement | M02, M05, M06 | Refund and journey controller enforcement integration-tested; application candidate covers default and delivery-race repair. Relay remains pending | n/a |
| 2 | Authenticated sessions and project isolation | M07, M06, M04 | Local/configured-token API sessions and project-scoped artifacts tested. Real IdP login and production qualification remain pending; draft #22 is separate | n/a |
| 3 | Injection and information-access resistance | M03, M04, M06, M09 | Module protections implemented; committed mocked adversarial evaluations exist in pending M09. Public document ingestion remains absent | n/a |
| 4 | Independent evaluation, audit and retention | M09, M02, M04, M06 | Pending M09 supplies independent evidence/oracles and bounded telemetry. Audit/retention automation and provider-account review remain partial | n/a |
| 5 | Capped live Meta compatibility | M01, M05, M09 | Offline gates tested; one authenticated live connectivity request completed HTTP 200 but generation was incomplete | Started, not passed: no live schema/tool/continuation proof |

## 1. World enforcement

Requirement: tests for every runnable world. Journey tests revoke permission
after a message is queued but before delivery. Service tests trigger human
takeover with actions pending. Denied actions produce no world mutation or
delivery.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Refund world: every tool call authorized before dispatch; a denial causes no mutation | Integration-tested | [test_m05_units.py](../tests/orchestration/test_m05_units.py) `test_denied_calls_leave_no_trace_or_ledger_change`, `test_policy_enforced_run_equals_core_run_when_allowed`; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_unbound_or_mismatched_identity_denies_the_run_without_mutation`, `test_unbound_principal_denies_and_leaves_no_open_reservation`; [test_m06_authorization.py](../tests/policy/test_m06_authorization.py) `test_denial_causes_no_mutation`, `test_customer_argument_cannot_spoof_identity` |
| Journey world: revocation after queue, before delivery; no delivery on denial | Integration-tested through controller/policy; application candidate tested | [test_m09_full_application.py](../tests/integration/test_m09_full_application.py) journey default and revocation_race; [application.spec.ts](../web/tests/browser/application.spec.ts) real API/UI race and guarded-boundary repair |
| Service world: owner takeover with actions pending; no mutation after takeover | Planned | `service-relay/1` stays specified and non-runnable |
| Non-runnable worlds refused by controllers | Integration-tested | [test_m07_security.py](../tests/api/test_m07_security.py) `test_service_relay_is_refused_before_anything_exists`; world-aware M05 registry refuses service-relay/1 until implemented and wired |

## 2. Authenticated sessions and project isolation

Requirement: identity comes from authenticated sessions. Tests block
cross-project campaign reads, resumes, approvals, memory retrieval and artifact
downloads.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Identity from authenticated sessions | Local/configured-token sessions implemented; real IdP login pending | [test_m09_full_application.py](../tests/integration/test_m09_full_application.py) project/principal derived from configured token, never body; `/v1/session` still reports production_authenticated false |
| Local-development boundary until authentication | Integration-tested | [test_m07_security.py](../tests/api/test_m07_security.py), [test_m07_process.py](../tests/api/test_m07_process.py); loopback and explicit opt-in preserved |
| Cross-project campaign reads, resumes, restarts | Integration-tested | [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_state_and_record_are_owner_only`, `test_pause_survives_restart_and_other_sessions_cannot_resume`, `test_start_cannot_take_over_or_restart_an_existing_campaign` |
| Cross-project approvals | Integration-tested | [test_m04_approvals.py](../tests/storage/test_m04_approvals.py) `test_other_project_cannot_see_or_act`; [test_m06_approvals.py](../tests/policy/test_m06_approvals.py) `test_cross_project_approval_is_not_found`, `test_authority_in_another_project_does_not_transfer`, `test_human_without_project_authority_cannot_approve`; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_stranger_and_wrong_digest_cannot_approve` |
| Cross-project memory retrieval | Implemented (store-level, SQLite) | [test_m04_memory_store.py](../tests/storage/test_m04_memory_store.py) `test_project_isolation`; [test_m03_context.py](../tests/context/test_m03_context.py) `test_store_refuses_queries_for_another_project`, `test_stale_and_cross_project_records_are_excluded_even_if_store_leaks`. Not yet exercised through an API session |
| Cross-project artifact downloads | Integration-tested through API | [test_m09_full_application.py](../tests/integration/test_m09_full_application.py) verifies own-project artifact hash and denies cross-project campaign/approval/artifact/replay access. M04 artifact ownership is stored separately from ArtifactRef |

## 3. Injection and information-access resistance

Requirement: adversarial documents, tool output and contaminated memory can't
grant authority, forge approval, change verdicts or expose unauthorized
evidence. Stale-memory and cross-project cases are included.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Tool output can't become instructions or widen scope | Implemented | [test_m03_context.py](../tests/context/test_m03_context.py) `test_injected_tool_text_cannot_become_instructions_or_widen_scope`; [test_m01_meta_responses.py](../tests/providers/test_m01_meta_responses.py) `test_untrusted_context_never_reaches_instructions`, `test_instruction_items_come_only_from_trusted_controller` |
| Model output can't grant authority or spoof identity | Implemented | [test_m06_authorization.py](../tests/policy/test_m06_authorization.py) `test_model_output_cannot_widen_or_spoof`, `test_delegation_escalation_denied` |
| Approvals can't be forged | Integration-tested | Gate 2 approval rows; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_resume_without_decision_stays_paused_and_executes_nothing` (the resume payload grants nothing) |
| Contaminated memory can't become a verified fact | Implemented | [test_m04_pipeline.py](../tests/storage/test_m04_pipeline.py) `test_status_comes_from_provenance_not_the_caller`; [test_m04_memory_store.py](../tests/storage/test_m04_memory_store.py) `test_untrusted_record_cannot_be_verified_in_place` |
| Stale memory excluded | Implemented | [test_m03_context.py](../tests/context/test_m03_context.py) `test_stale_and_cross_project_records_are_excluded_even_if_store_leaks` |
| Verdicts can't be changed by models | Implemented | Verdicts are deterministic code ([test_m05_units.py](../tests/orchestration/test_m05_units.py) `test_findings_cite_ledger_evidence_and_validate`). Analyst outputs carry no verdict or code: [test_contracts_world.py](../tests/contracts/test_contracts_world.py) `test_analyst_outputs_cannot_carry_verdicts_code_or_foreign_names` |
| Adversarial documents | **Planned** | No document-ingestion path exists yet |
| End-to-end adversarial campaign suite | Implemented in pending M09 | [test_m09_evaluations.py](../tests/integration/test_m09_evaluations.py), [evals/cases.py](../evals/cases.py): independent stored-evidence checks with mocked inference; no claim about model attack resistance |

## 4. Independent evaluation, audit and retention

Requirement: independent scenarios test requirements beyond the agent's own
plan. Audit records cover attempts, denials, approvals and recovery. Local
retention and deletion are documented and tested, and provider-held data and
its limits are explicitly documented.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Independent evaluation scenarios | Implemented in pending M09 | [evals/cases.py](../evals/cases.py), [evals/oracles.py](../evals/oracles.py); random/one-pass/adaptive model comparison remains deferred |
| Audit: attempts | Implemented (budget ledger) | Every HTTP attempt is a durable reservation: [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_provider_errors_have_bounded_recovery` |
| Audit: denials, approvals, recovery | Partial | Recorded as `ExperimentEvent`s and errors in graph checkpoints, plus approval rows in M04. There is no queryable audit store yet (M09/M04) |
| Local retention and deletion | Implemented | [test_m04_pipeline.py](../tests/storage/test_m04_pipeline.py) `test_expiry_respects_audit_hold_and_purge_keeps_references`; artifact and checkpoint deletion is not yet documented |
| Provider-held data documented | Partial; live API privacy qualification pending | M01 records default stateful continuation. A single synthetic connectivity request used store:false, which is not proof of account-wide ZDR. Draft #22 deliberately disables live API content pending a qualified boundary |

## 5. Capped live Meta compatibility

Requirement: after explicit authorization, verify the exact provider,
endpoint, model and model-facing schemas used. Enforce a total HTTP-attempt cap
including retries, token limits and a deadline. Record sanitized results and
unknown cost honestly.

| Requirement | Offline status | Evidence |
| --- | --- | --- |
| Explicit authorization bound to exact provider, endpoint and model; re-checked on resume and recovery | Integration-tested | [test_m05_review_fixes.py](../tests/orchestration/test_m05_review_fixes.py) `test_recover_refuses_an_unauthorized_non_mock_transport`, `test_recover_refuses_a_changed_model_or_endpoint`, `test_incomplete_or_unbound_report_is_refused`; [test_m05_units.py](../tests/orchestration/test_m05_units.py) `test_live_authorization_requires_every_gate`, `test_real_http_transport_is_not_treated_as_a_mock` |
| Total HTTP-attempt cap including retries | Integration-tested | [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_budget_counts_every_http_attempt_including_retries`; [test_m05_review_fixes.py](../tests/orchestration/test_m05_review_fixes.py) `test_authorized_recovery_uses_the_smaller_live_cap`, `test_explorer_ceiling_holds_across_task_retries`; [test_m01_review_fixes.py](../tests/providers/test_m01_review_fixes.py) `test_probe_cap_counts_retries_as_attempts` |
| Token limits | Integration-tested | Per-attempt output and input reservations: [test_m05_units.py](../tests/orchestration/test_m05_units.py) `test_task_ceiling_applies_per_task` |
| Deadline | Integration-tested | [test_m05_review_fixes.py](../tests/orchestration/test_m05_review_fixes.py) `test_retries_stop_at_the_campaign_deadline`; [test_m01_review_fixes.py](../tests/providers/test_m01_review_fixes.py) `test_absolute_deadline_is_enforced_during_the_read` |
| Unknown cost recorded as unknown | Implemented | [test_m01_meta_responses.py](../tests/providers/test_m01_meta_responses.py) `test_usage_omissions_stay_unknown` (cost is always `null`) |
| Sanitized report; mocked runs never labelled live | Implemented | [test_m01_probe.py](../tests/providers/test_m01_probe.py) `test_mock_probe_respects_call_cap_and_is_not_reported_live`, `test_dry_run_is_default_and_sends_nothing` |
| Live verification of provider, endpoint, model and model-facing schemas | **Started, not passed** | [connectivity result](evidence/meta-connectivity-2026-11-01.json): direct Meta muse-spark-1.3 returned HTTP 200/incomplete at a 128-output-token cap, 15 input/128 output tokens. No completed text/schema/tool/continuation result. Full capped compatibility remains required for all four model-facing schemas |

## Changing a status

- To move a requirement forward, link the committed test or sanitized report
  that proves it, in the same PR that changes the status.
- To mark something live-tested, link the sanitized M01 compatibility report
  for the exact provider, endpoint and model.
- A status never advances because of mocked responses, a scratch run or a
  module claim without a linked test.
