# Mandatory acceptance gates

Status as of 2026-10-30, at integration commit `34737a0` (`architecture-and-refund-core`),
which includes contract set 1.1 (PR #9), the M02 journey world (PR #10), the
`group-journey/1` registry promotion (PR #12) and the v1 capsule metadata fix (PR #13).

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
| 1 | World enforcement | M02, M05, M06 | **Partly implemented.** Refund: integration-tested through the controller. Journey: world enforcement implemented and tested in M02; controller and security integration (M05, M06) pending. Service: not implemented | n/a |
| 2 | Authenticated sessions and project isolation | M07, M06, M04 | **Planned.** There is no authentication yet (M07), and artifacts aren't project-scoped | n/a |
| 3 | Injection and information-access resistance | M03, M04, M06, M09 | **Implemented**, but there is no committed end-to-end adversarial suite | n/a |
| 4 | Independent evaluation, audit and retention | M09, M02, M04, M06 | **Planned.** M09 hasn't started; retention and audit are partial | n/a |
| 5 | Capped live Meta compatibility | M01, M05, M09 | **Integration-tested offline (mocked)** | **Not started:** no authorization, key, model or spending cap provided |

## 1. World enforcement

Requirement: tests for every runnable world. Journey tests revoke permission
after a message is queued but before delivery. Service tests trigger human
takeover with actions pending. Denied actions produce no world mutation or
delivery.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Refund world: every tool call authorized before dispatch; a denial causes no mutation | Integration-tested | [test_m05_units.py](../tests/orchestration/test_m05_units.py) `test_denied_calls_leave_no_trace_or_ledger_change`, `test_policy_enforced_run_equals_core_run_when_allowed`; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_unbound_or_mismatched_identity_denies_the_run_without_mutation`, `test_unbound_principal_denies_and_leaves_no_open_reservation`; [test_m06_authorization.py](../tests/policy/test_m06_authorization.py) `test_denial_causes_no_mutation`, `test_customer_argument_cannot_spoof_identity` |
| Journey world: revocation after queue, before delivery; no delivery on denial | **World enforcement implemented**, tested in M02 only. **Controller and security integration pending:** no journey run goes through M05 or M06 yet, and M06 tool permissions don't cover journey tools | [test_m02_journey.py](../tests/simulation/test_m02_journey.py) `test_revocation_race_gets_through_the_lab_world_and_only_the_guard_stops_it`, `test_guard_denials_record_an_error_event_and_change_nothing`, `test_fault_matrix` |
| Service world: owner takeover with actions pending; no mutation after takeover | Integration-tested | [test_m05_relay_campaign.py](../tests/orchestration/test_m05_relay_campaign.py) `test_the_takeover_race_is_caught_in_the_lab_and_closed_only_by_the_boundary`, `test_a_guarded_run_denies_a_confirmation_at_the_takeover_event`; [test_m02_relay.py](../tests/simulation/test_m02_relay.py) `test_takeover_race_gets_through_the_lab_world_and_only_the_guard_stops_it`, `test_no_timing_lets_a_guarded_run_violate_an_invariant` |
| Non-runnable worlds refused by controllers | Integration-tested | All three registered packs are implemented, so the tests mark one specified: [test_m05_journey_campaign.py](../tests/orchestration/test_m05_journey_campaign.py) `test_a_pack_that_is_only_specified_is_refused_at_start`, `test_a_driver_cannot_make_an_unimplemented_pack_runnable`, `test_a_world_without_a_driver_is_refused_even_if_implemented`; [test_m06_world.py](../tests/policy/test_m06_world.py) `test_a_pack_that_is_only_specified_does_not_pass_the_gate` |

## 2. Authenticated sessions and project isolation

Requirement: identity comes from authenticated sessions. Tests block
cross-project campaign reads, resumes, approvals, memory retrieval and artifact
downloads.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Identity from authenticated sessions | **Planned (M07)** | M05 takes a `TrustedSession` from its caller, and M07 must derive it from authentication. Nothing authenticates yet |
| Local-development boundary until authentication | Implemented (guard only; there is no application yet) | [test_m06_security.py](../tests/policy/test_m06_security.py) `test_local_dev_allowed_on_loopback_with_opt_in`, `test_local_dev_refused_off_loopback_without_opt_in_or_when_hosted`, `test_local_dev_is_never_production_authentication` |
| Cross-project campaign reads, resumes, restarts | Integration-tested | [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_state_and_record_are_owner_only`, `test_pause_survives_restart_and_other_sessions_cannot_resume`, `test_start_cannot_take_over_or_restart_an_existing_campaign` |
| Cross-project approvals | Integration-tested | [test_m04_approvals.py](../tests/storage/test_m04_approvals.py) `test_other_project_cannot_see_or_act`; [test_m06_approvals.py](../tests/policy/test_m06_approvals.py) `test_cross_project_approval_is_not_found`, `test_authority_in_another_project_does_not_transfer`, `test_human_without_project_authority_cannot_approve`; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_stranger_and_wrong_digest_cannot_approve` |
| Cross-project memory retrieval | Implemented (store-level, SQLite) | [test_m04_memory_store.py](../tests/storage/test_m04_memory_store.py) `test_project_isolation`; [test_m03_context.py](../tests/context/test_m03_context.py) `test_store_refuses_queries_for_another_project`, `test_stale_and_cross_project_records_are_excluded_even_if_store_leaks`. Not yet exercised through an API session |
| Cross-project artifact downloads | **Planned** | M04 artifacts aren't project-scoped, because `ArtifactRef` has no project field. This needs an M00/M04 change, then M07 route tests |

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
| End-to-end adversarial campaign suite | **Planned (M09)** | The cross-store runs so far were scratch checks, not committed tests |

## 4. Independent evaluation, audit and retention

Requirement: independent scenarios test requirements beyond the agent's own
plan. Audit records cover attempts, denials, approvals and recovery. Local
retention and deletion are documented and tested, and provider-held data and
its limits are explicitly documented.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Independent evaluation scenarios | **Planned (M09)** | M09 hasn't started. Comparisons of random, one-pass and adaptive exploration under equal budgets are also M09's |
| Audit: attempts | Implemented (budget ledger) | Every HTTP attempt is a durable reservation: [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_provider_errors_have_bounded_recovery` |
| Audit: denials, approvals, recovery | Partial | Recorded as `ExperimentEvent`s and errors in graph checkpoints, plus approval rows in M04. There is no queryable audit store yet (M09/M04) |
| Local retention and deletion | Implemented | [test_m04_pipeline.py](../tests/storage/test_m04_pipeline.py) `test_expiry_respects_audit_hold_and_purge_keeps_references`; artifact and checkpoint deletion is not yet documented |
| Provider-held data documented | Partial | [M01.md](modules/M01.md) records that Meta `store` defaults to `true` and that continuation relies on it. A retention decision and user-facing documentation are pending (M06/M09) |

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
| Live verification of provider, endpoint, model and model-facing schemas | **Live: not started** | Needs an authorized key, the exact model and an explicit spending cap. The model-facing schemas to verify are `experiment_proposal/1`, `world_experiment_proposal/1`, `evidence_interpretation/1` and `repair_suggestion/1` |

## Changing a status

- To move a requirement forward, link the committed test or sanitized report
  that proves it, in the same PR that changes the status.
- To mark something live-tested, link the sanitized M01 compatibility report
  for the exact provider, endpoint and model.
- A status never advances because of mocked responses, a scratch run or a
  module claim without a linked test.
