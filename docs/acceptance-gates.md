# Mandatory acceptance gates

Status as of 2026-11-01, at integration commit `abeff6f` (`architecture-and-refund-core`),
which includes contract set 1.1 (PR #9), the M02 journey world (PR #10), the
`group-journey/1` registry promotion (PR #12), the v1 capsule metadata fix (PR #13),
M04 world persistence (PR #14), M06 world policy (PR #15), the M03 journey skill
(PR #16), M05 world campaigns (PR #17), M00 web tooling (PR #19), the M07 control
plane (PR #21), the M08 interface (PR #20), M09 quality and operations (PR #18) and
the failure minimizer (PR #23).

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

Full-application acceptance on this head — create a campaign, find the failure,
inspect the evidence and Reality Diff, approve the repair, verify it, then export
and replay the capsule through the real routes and the real interface — is still
6 skipped placeholders in
[test_m09_full_application.py](../tests/integration/test_m09_full_application.py),
not evidence. The rows below claim only what committed running tests prove.

**Use policy:**

- Offline development continues before the live gate passes.
- **Customer-connected use waits** for gates 1–4 wherever they apply, plus
  gate 5's live evidence. The release record agrees:
  `customer_connected_use_allowed` is false (see [M09.md](modules/M09.md),
  Release evidence).
- Sessions exist, but production authentication does not. The application stays
  inside the documented local-development boundary: loopback only, explicit
  opt-in, operator-configured tokens at most, and never presented as production
  authentication.

## Summary

| # | Gate | Owners | Status | Live-tested |
| --- | --- | --- | --- | --- |
| 1 | World enforcement | M02, M05, M06 | **Integration-tested** for both runnable worlds. Service: still specified and non-runnable, and refused by the controllers | n/a |
| 2 | Authenticated sessions and project isolation | M07, M06, M04 | **Integration-tested on the local boundary.** Production authentication is still open, and memory has no API route | n/a |
| 3 | Injection and information-access resistance | M03, M04, M06, M09 | **Integration-tested**, with a committed end-to-end adversarial suite. Adversarial documents still have no ingestion path | n/a |
| 4 | Independent evaluation, audit and retention | M09, M02, M04, M06 | **Independent evaluation integration-tested; audit still partial; retention implemented with documented limits** | n/a |
| 5 | Capped live Meta compatibility | M01, M05, M09 | **Integration-tested offline (mocked)** | **Not started:** no authorization, key, model or spending cap provided |

## 1. World enforcement

Requirement: tests for every runnable world. Journey tests revoke permission
after a message is queued but before delivery. Service tests trigger human
takeover with actions pending. Denied actions produce no world mutation or
delivery.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Refund world: every tool call authorized before dispatch; a denial causes no mutation | Integration-tested | [test_m05_units.py](../tests/orchestration/test_m05_units.py) `test_denied_calls_leave_no_trace_or_ledger_change`, `test_policy_enforced_run_equals_core_run_when_allowed`; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_unbound_or_mismatched_identity_denies_the_run_without_mutation`, `test_unbound_principal_denies_and_leaves_no_open_reservation`; [test_m06_authorization.py](../tests/policy/test_m06_authorization.py) `test_denial_causes_no_mutation`, `test_customer_argument_cannot_spoof_identity` |
| Journey world: revocation after queue, before delivery; no delivery on denial | **Integration-tested through the controller.** M05 runs journey campaigns under M06 policy: lab runs expose the failure, the guarded repair holds it, and policy denials stop the campaign with no mutation. Reads need a bound identity but not sharing permission (an M06 gap; nothing leaves the synthetic run) | World enforcement: [test_m02_journey.py](../tests/simulation/test_m02_journey.py) `test_revocation_race_gets_through_the_lab_world_and_only_the_guard_stops_it`, `test_guard_denials_record_an_error_event_and_change_nothing`, `test_fault_matrix`; through the controller: [test_m05_journey_campaign.py](../tests/orchestration/test_m05_journey_campaign.py) `test_lab_experiment_detects_revoked_delivery_and_pauses_for_approval`, `test_the_race_is_caught_in_the_lab_and_closed_only_by_the_boundary`, `test_approved_repair_runs_guarded_and_records_its_configuration`, `test_a_guarded_run_denies_delivery_after_revocation`, `test_m06_can_stop_a_delivery_the_guard_would_allow`, `test_a_repair_stops_when_m06_refuses_a_delivery`, `test_an_identity_outside_the_journey_is_denied_not_a_clean_run`, `test_ownership_holds_in_the_lab_too`, `test_a_principal_bound_to_the_viewer_cannot_change_the_world`, `test_an_unbound_principal_runs_nothing`; the read nuance is an [M06.md](modules/M06.md) documented gap |
| Service world: owner takeover with actions pending; no mutation after takeover | Planned | `service-relay/1` stays specified and non-runnable |
| Non-runnable worlds refused by controllers | Integration-tested | [test_m05_journey_campaign.py](../tests/orchestration/test_m05_journey_campaign.py) `test_service_relay_is_refused_at_start`, `test_a_driver_cannot_make_an_unimplemented_pack_runnable`, `test_a_proposal_for_another_world_is_rejected`, `test_the_gate_is_rechecked_on_resume_and_recovery`, `test_the_gate_is_rechecked_before_execution`, `test_the_gate_is_rechecked_before_the_repair`; [test_m07_security.py](../tests/api/test_m07_security.py) `test_service_relay_is_refused_before_anything_exists`. `WorldCampaignSpec` admits the runnable worlds, and the runnable gate consults `is_executable()` |

## 2. Authenticated sessions and project isolation

Requirement: identity comes from authenticated sessions. Tests block
cross-project campaign reads, resumes, approvals, memory retrieval and artifact
downloads.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Identity from authenticated sessions | **Integration-tested on the local boundary; production authentication still open.** M07 resolves one principal in one project from the session: operator-configured tokens (`configured_tokens`) or the local-dev principal. No path, query, header or body names identity, and nothing claims production authentication | [test_m07_security.py](../tests/api/test_m07_security.py) `test_session_reports_who_the_server_resolved`, `test_token_sessions_keep_no_token`, `test_a_body_cannot_carry_identity_or_authority`, `test_a_full_local_dev_flow_is_labelled_as_not_production`; [test_m07_contract.py](../tests/api/test_m07_contract.py) `test_no_request_can_name_a_project_principal_role_or_scope`; [test_m07_review_fixes.py](../tests/api/test_m07_review_fixes.py) `test_nothing_in_the_published_contract_claims_authentication` |
| Local-development boundary until authentication | Integration-tested | [test_m06_security.py](../tests/policy/test_m06_security.py) `test_local_dev_allowed_on_loopback_with_opt_in`, `test_local_dev_refused_off_loopback_without_opt_in_or_when_hosted`, `test_local_dev_is_never_production_authentication`; [test_m07_security.py](../tests/api/test_m07_security.py) `test_local_dev_requires_the_explicit_loopback_opt_in`, `test_non_production_sessions_serve_loopback_only`, `test_every_route_refuses_a_missing_or_wrong_session`, `test_a_session_is_required_before_a_body_is_even_parsed` |
| Cross-project campaign reads, resumes, restarts | Integration-tested | [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_state_and_record_are_owner_only`, `test_pause_survives_restart_and_other_sessions_cannot_resume`, `test_start_cannot_take_over_or_restart_an_existing_campaign`; [test_m07_security.py](../tests/api/test_m07_security.py) `test_campaigns_read_as_absent_outside_their_owner`, `test_headers_and_queries_cannot_choose_a_project`; [test_m07_lifecycle.py](../tests/api/test_m07_lifecycle.py) `test_only_the_owner_may_resume_or_cancel` |
| Cross-project approvals | Integration-tested | [test_m04_approvals.py](../tests/storage/test_m04_approvals.py) `test_other_project_cannot_see_or_act`; [test_m06_approvals.py](../tests/policy/test_m06_approvals.py) `test_cross_project_approval_is_not_found`, `test_authority_in_another_project_does_not_transfer`, `test_human_without_project_authority_cannot_approve`; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_stranger_and_wrong_digest_cannot_approve`; [test_m07_security.py](../tests/api/test_m07_security.py) `test_approvals_are_visible_to_owner_and_approvers_only`, `test_only_an_authorized_approver_in_the_project_can_decide` |
| Cross-project memory retrieval | Implemented (store-level, SQLite) | [test_m04_memory_store.py](../tests/storage/test_m04_memory_store.py) `test_project_isolation`; [test_m03_context.py](../tests/context/test_m03_context.py) `test_store_refuses_queries_for_another_project`, `test_stale_and_cross_project_records_are_excluded_even_if_store_leaks`. No API route serves memory yet |
| Cross-project artifact downloads | Integration-tested | M04 project views: [test_m04_world_persistence.py](../tests/storage/test_m04_world_persistence.py) `test_a_project_reads_its_own_artifacts`, `test_another_project_cannot_see_or_read_an_artifact`, `test_the_same_name_in_two_projects_stays_separate`, `test_identical_bytes_share_a_blob_but_not_ownership`, `test_clearing_the_owner_column_does_not_expose_an_artifact`, `test_the_unscoped_api_cannot_squat_or_probe_a_project_name`; through the routes: [test_m07_security.py](../tests/api/test_m07_security.py) `test_artifacts_are_project_scoped`, [test_m07_flows.py](../tests/api/test_m07_flows.py) `test_a_capsule_travels_between_projects_as_data`; [test_m05_journey_campaign.py](../tests/orchestration/test_m05_journey_campaign.py) `test_a_run_artifact_is_invisible_to_another_project` |

## 3. Injection and information-access resistance

Requirement: adversarial documents, tool output and contaminated memory can't
grant authority, forge approval, change verdicts or expose unauthorized
evidence. Stale-memory and cross-project cases are included.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Tool output can't become instructions or widen scope | Implemented | [test_m03_context.py](../tests/context/test_m03_context.py) `test_injected_tool_text_cannot_become_instructions_or_widen_scope`; [test_m01_meta_responses.py](../tests/providers/test_m01_meta_responses.py) `test_untrusted_context_never_reaches_instructions`, `test_instruction_items_come_only_from_trusted_controller` |
| Model output can't grant authority or spoof identity | Implemented | [test_m06_authorization.py](../tests/policy/test_m06_authorization.py) `test_model_output_cannot_widen_or_spoof`, `test_delegation_escalation_denied`; [test_m05_journey_campaign.py](../tests/orchestration/test_m05_journey_campaign.py) `test_a_proposal_carrying_enforcement_or_authority_never_runs` |
| Approvals can't be forged | Integration-tested | Gate 2 approval rows; [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_resume_without_decision_stays_paused_and_executes_nothing` (the resume payload grants nothing); [test_m07_review_fixes.py](../tests/api/test_m07_review_fixes.py) `test_can_approve_matches_what_decide_accepts` (the session's answer matches the decision); [test_m07_security.py](../tests/api/test_m07_security.py) `test_the_reviewer_sees_the_action_the_digest_binds` (the shown action matches the bound digest) |
| Contaminated memory can't become a verified fact | Integration-tested | [test_m04_pipeline.py](../tests/storage/test_m04_pipeline.py) `test_status_comes_from_provenance_not_the_caller`; [test_m04_memory_store.py](../tests/storage/test_m04_memory_store.py) `test_untrusted_record_cannot_be_verified_in_place`; end to end in both worlds: [cases.py](../evals/cases.py) `adversarial.memory_injection.refund` and `adversarial.memory_injection.journey`, run by [test_m09_evaluations.py](../tests/integration/test_m09_evaluations.py) `test_the_suite_runs_clean_and_matches_the_reviewed_expectations` |
| Stale memory excluded | Integration-tested | [test_m03_context.py](../tests/context/test_m03_context.py) `test_stale_and_cross_project_records_are_excluded_even_if_store_leaks`; end to end: [cases.py](../evals/cases.py) `adversarial.stale_intent`, run by [test_m09_evaluations.py](../tests/integration/test_m09_evaluations.py) `test_the_suite_runs_clean_and_matches_the_reviewed_expectations` |
| Verdicts can't be changed by models | Implemented | Verdicts are deterministic code ([test_m05_units.py](../tests/orchestration/test_m05_units.py) `test_findings_cite_ledger_evidence_and_validate`). Analyst outputs carry no verdict or code: [test_contracts_world.py](../tests/contracts/test_contracts_world.py) `test_analyst_outputs_cannot_carry_verdicts_code_or_foreign_names` |
| Adversarial documents | **Planned** | No document-ingestion path exists yet |
| End-to-end adversarial campaign suite | Integration-tested | 18 committed adversarial cases with independent oracles: [test_m09_evaluations.py](../tests/integration/test_m09_evaluations.py) `test_the_suite_runs_clean_and_matches_the_reviewed_expectations`, `test_both_complete_flows_are_covered`, `test_the_cli_exit_code_is_what_blocks_ci`; [cases.py](../evals/cases.py), [gate.py](../evals/gate.py), [expected.json](../evals/expected.json). Limits: an adversarial fixture stands in for a model that obeyed the attack, so nothing here measures how a real model behaves; the random, one-pass and adaptive comparison is declared deferred (`test_comparison_is_declared_deferred_not_claimed`); the CI workflow exists but has not run on GitHub ([M09.md](modules/M09.md), CI) |

## 4. Independent evaluation, audit and retention

Requirement: independent scenarios test requirements beyond the agent's own
plan. Audit records cover attempts, denials, approvals and recovery. Local
retention and deletion are documented and tested, and provider-held data and
its limits are explicitly documented.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Independent evaluation scenarios | Integration-tested | 23 cases, 382 expectations, all measured from stored evidence with mocked inference. The oracles recompute every verdict from stored runs and share no code with the verifiers ([oracles.py](../evals/oracles.py)); regressions block promotion: [test_m09_evaluations.py](../tests/integration/test_m09_evaluations.py) `test_the_suite_runs_clean_and_matches_the_reviewed_expectations`, `test_both_complete_flows_are_covered`, `test_a_real_behaviour_regression_blocks_promotion`, `test_a_report_claiming_live_inference_is_refused`. Limits: the exploration comparison is deferred, CI has not run on GitHub, and full-application acceptance is still placeholders (see the note above) |
| Audit: attempts | Implemented (budget ledger) | Every HTTP attempt is a durable reservation: [test_m05_campaign.py](../tests/orchestration/test_m05_campaign.py) `test_provider_errors_have_bounded_recovery` |
| Audit: denials, approvals, recovery | Partial | Recorded as `ExperimentEvent`s and errors in graph checkpoints, plus approval rows in M04. Telemetry is operational evidence, not the audit of record: [test_m09_telemetry.py](../tests/integration/test_m09_telemetry.py) `test_span_and_event_carry_every_required_field`; [test_m09_campaign_trace.py](../tests/integration/test_m09_campaign_trace.py) `test_a_complete_refund_campaign_is_traced_without_secret_leakage`, `test_a_complete_journey_campaign_is_traced_without_secret_leakage`, `test_a_rebuilt_trace_is_the_same_trace`. Events and tasks still live in graph state, so there is no queryable audit store yet |
| Local retention and deletion | Implemented | [test_m04_pipeline.py](../tests/storage/test_m04_pipeline.py) `test_expiry_respects_audit_hold_and_purge_keeps_references`; backup and restore are documented in [recovery-drill.md](../deploy/recovery-drill.md) and automated as [cases.py](../evals/cases.py) `ops.recovery_drill`; telemetry files are byte-bounded with rotation and pruned by age: [test_m09_telemetry.py](../tests/integration/test_m09_telemetry.py) `test_file_sink_never_exceeds_its_bound_and_rotates`, `test_retention_prunes_only_expired_files`. Limits: nothing schedules `prune_expired` yet, and Windows ACLs are documented only ([M09.md](modules/M09.md), Retention and access) |
| Provider-held data documented | Partial | [M01.md](modules/M01.md) records that Meta `store` defaults to `true` and that continuation relies on it. The retention decision is still open ([M09.md](modules/M09.md), Gaps and requests) |

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
| Live verification of provider, endpoint, model and model-facing schemas | **Live: not started** | Needs an authorized key, the exact model and an explicit spending cap. `live_capability` is `not_started` with no report in [release-targets.json](../deploy/release-targets.json), and the evaluation gate refuses any report claiming live inference ([test_m09_evaluations.py](../tests/integration/test_m09_evaluations.py) `test_a_report_claiming_live_inference_is_refused`). The model-facing schemas to verify are `experiment_proposal/1`, `world_experiment_proposal/1`, `evidence_interpretation/1` and `repair_suggestion/1` |

## Changing a status

- To move a requirement forward, link the committed test or sanitized report
  that proves it, in the same PR that changes the status.
- To mark something live-tested, link the sanitized M01 compatibility report
  for the exact provider, endpoint and model.
- A status never advances because of mocked responses, a scratch run or a
  module claim without a linked test.
