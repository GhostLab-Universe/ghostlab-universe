"""M05 campaign integration against REAL M04 SQLite stores (mocked inference only)."""

import pytest

from ghostlab.contracts import CampaignRecord, MemoryQuery
from ghostlab.orchestration import CampaignExists, TrustedSession, ledger_key
from ghostlab.policy import ApprovalDenied, PolicyDenied, StaticPrincipalBinding, root_scope

from m05_harness import (BUSY, OPERATOR, PROJECT, REVIEWER, SECRET, STRANGER, Lab, Script,
                         meta_reply, proposal, spec)

CID = "campaign-1"
KEY = ledger_key(PROJECT, CID)  # budget ledger rows are namespaced by project


def paused_lab(tmp_path, **options):
    lab = Lab(tmp_path, Script(proposal()), **options)
    with lab.controller() as controller:
        state = controller.start(CID, spec())
    return lab, state


def test_failure_found_then_paused_for_approval(tmp_path):
    lab, state = paused_lab(tmp_path)
    assert state["paused"] and state["status"] == "awaiting_approval"
    (experiment,) = state["experiments"].values()
    diff = experiment["diff"]
    assert diff["baseline_refunded_minor"] == 250_000
    assert diff["faulted_refunded_minor"] == 500_000
    assert diff["first_divergence"]["event_id"] == 2
    assert "total_refund_within_payment" in diff["violated_invariants"]
    assert lab.engine.calls == [(False, "vulnerable"), (True, "vulnerable")]
    assert len(lab.script.requests) == 1
    approval = lab.approval_store.get(PROJECT, state["approval"]["approval_id"])
    assert approval.status == "pending" and approval.scope.project_id == PROJECT
    # Verified findings were written to real memory with the supervisor scope.
    scope = root_scope(PROJECT, OPERATOR)
    records = lab.memory_store.query(scope, MemoryQuery.model_validate({
        "schema_version": 1, "project_id": PROJECT, "campaign_id": CID,
        "world_version": "refund-world/1", "kinds": ["finding"], "statuses": ["verified"],
        "as_of": "2026-10-29T13:00:00Z", "limit": 20}))
    assert any("total_refund_within_payment failed" in r.content for r in records)
    # Artifacts are content-addressed and readable back.
    assert lab.artifacts.read(lab.artifacts.ref(experiment["fork"]["id"]))


def test_approval_resumes_once_and_consumes_single_use(tmp_path):
    lab, state = paused_lab(tmp_path)
    lab.approve(state)
    with lab.controller() as controller:
        done = controller.resume(CID)
        again = controller.resume(CID)  # finished: a second resume does nothing
        record = controller.record(CID)
    assert done["status"] == "completed" and done["outcome"] == "repair_verified"
    assert done["repair"]["refunded_minor"] == 250_000 and not done["paused"]
    assert again["outcome"] == "repair_verified"
    assert lab.engine.calls.count((True, "repaired")) == 1
    assert lab.approval_store.get(PROJECT, state["approval"]["approval_id"]).status == "consumed"
    with pytest.raises(ApprovalDenied) as replay:
        lab.approvals.consume(project_id=PROJECT, approval_id=state["approval"]["approval_id"],
                              action=state["approval"]["action"], now=lab.clock())
    assert replay.value.code == "not_approved"  # already consumed; no second use
    assert isinstance(record, CampaignRecord) and record.status == "completed"
    assert record.usage.model_calls == 1 and record.usage.tool_calls == 8  # 2 + 3 + 3 calls
    statuses = {t["task_id"]: t["status"] for t in done["tasks"]}
    assert set(statuses.values()) == {"succeeded"}


def test_resume_without_decision_stays_paused_and_executes_nothing(tmp_path):
    lab, state = paused_lab(tmp_path)
    with lab.controller() as controller:
        again = controller.resume(CID)
    assert again["paused"] and again["status"] == "awaiting_approval"
    assert lab.engine.calls.count((True, "repaired")) == 0


def test_rejected_approval_stops_without_repair(tmp_path):
    lab, state = paused_lab(tmp_path)
    lab.approve(state, decision="reject")
    with lab.controller() as controller:
        done = controller.resume(CID)
    assert done["status"] == "completed" and done["outcome"] == "repair_rejected"
    assert lab.engine.calls.count((True, "repaired")) == 0


def test_expired_approval_stops_and_is_marked_expired(tmp_path):
    lab, state = paused_lab(tmp_path)
    lab.clock.advance(hours=2)
    with lab.controller() as controller:
        done = controller.resume(CID)
    assert done["status"] == "failed" and done["outcome"] == "approval_expired"
    assert lab.approval_store.get(PROJECT, state["approval"]["approval_id"]).status == "expired"
    assert lab.engine.calls.count((True, "repaired")) == 0


def test_stranger_and_wrong_digest_cannot_approve(tmp_path):
    lab, state = paused_lab(tmp_path)
    with pytest.raises(ApprovalDenied) as stranger:
        lab.approve(state, principal=STRANGER)
    assert stranger.value.code == "approver_not_authorized"
    with pytest.raises(ApprovalDenied) as changed:
        lab.approve(state, digest="0" * 64)
    assert changed.value.code == "digest_mismatch"
    with pytest.raises(ApprovalDenied):  # the controller itself is not an approver
        lab.approve(state, principal=OPERATOR)
    with lab.controller() as controller:
        assert controller.resume(CID)["paused"]
    altered = {**state["approval"]["action"], "adapter": "vulnerable"}
    lab.approve(state)
    with pytest.raises(ApprovalDenied) as other_action:
        lab.approvals.consume(project_id=PROJECT, approval_id=state["approval"]["approval_id"],
                              action=altered, now=lab.clock())
    assert other_action.value.code == "digest_mismatch"


def test_pause_survives_restart_and_other_sessions_cannot_resume(tmp_path):
    lab, state = paused_lab(tmp_path)
    other_project = TrustedSession(OPERATOR, "project-other")
    with lab.controller(session=other_project) as intruder, pytest.raises(PolicyDenied):
        intruder.resume(CID)
    with lab.controller(session=TrustedSession(REVIEWER, PROJECT)) as other_user, \
            pytest.raises(PolicyDenied):
        other_user.resume(CID)
    lab.approve(state)
    with lab.controller() as restarted:  # new saver connection and controller
        done = restarted.resume(CID)
    assert done["outcome"] == "repair_verified"
    assert len(lab.script.requests) == 1  # resuming made no new model call


def test_crash_after_execute_recovers_without_repeating_runs_or_calls(tmp_path):
    crashes = []

    def crash_once(name, state):
        if name == "verify" and not crashes:
            crashes.append(name)
            raise RuntimeError("simulated crash before verification")

    lab = Lab(tmp_path, Script(proposal()), on_node=crash_once)
    with lab.controller() as controller, pytest.raises(RuntimeError):
        controller.start(CID, spec())
    assert len(lab.engine.calls) == 2 and len(lab.script.requests) == 1
    with lab.controller() as restarted:  # graph resume from the last checkpoint
        state = restarted.recover(CID)
    assert state["paused"] and state["status"] == "awaiting_approval"
    assert len(lab.engine.calls) == 2       # execute was checkpointed: not repeated
    assert len(lab.script.requests) == 1    # propose was checkpointed: no new model call
    assert len(state["experiments"]) == 1


def test_invalid_proposal_never_executes(tmp_path):
    lab = Lab(tmp_path, Script(proposal(max_tool_calls=500)))
    with lab.controller(max_iterations=1) as controller:
        state = controller.start(CID, spec())
    assert lab.engine.calls == []
    assert state["status"] == "completed" and state["outcome"] == "no_failure_found"
    assert any(e["code"] == "validation_failed" for e in state["errors"])
    statuses = {t["kind"]: t["status"] for t in state["tasks"]}
    assert statuses["validate_proposal"] == "failed" and statuses["execute_run"] == "skipped"


def test_malformed_model_output_is_retried_within_bounds(tmp_path):
    lab = Lab(tmp_path, Script(meta_reply("not a proposal"), proposal()))
    with lab.controller() as controller:
        state = controller.start(CID, spec())
    assert state["paused"] and len(lab.script.requests) == 2
    assert state["errors"][0]["code"] == "provider_invalid_output"


def test_provider_errors_have_bounded_recovery(tmp_path):
    lab = Lab(tmp_path, Script(BUSY, BUSY, BUSY, BUSY), retry_attempts=2)
    with lab.controller() as controller:
        state = controller.start(CID, spec())
    assert state["status"] == "failed" and state["outcome"] == "explorer_failed"
    assert len(lab.script.requests) == 4  # 2 task attempts x 2 HTTP attempts, then stop
    assert lab.engine.calls == []
    used, unknown = lab.ledger.committed(KEY, "model_calls")
    assert used == 4  # every HTTP attempt was reserved and accounted
    assert SECRET not in str(state)


def test_budget_counts_every_http_attempt_including_retries(tmp_path):
    lab = Lab(tmp_path, Script(BUSY, BUSY, proposal()), retry_attempts=3)
    with lab.controller() as controller:
        state = controller.start(CID, spec(max_model_calls=2))
    assert len(lab.script.requests) == 2  # the third (retry) attempt was never sent
    assert state["status"] == "budget_exhausted" and state["outcome"] == "model_calls"
    assert lab.ledger.committed(KEY, "model_calls")[0] == 2
    assert lab.engine.calls == []


def test_tool_budget_is_reserved_before_runs(tmp_path):
    lab = Lab(tmp_path, Script(proposal()))
    with lab.controller(max_iterations=1) as controller:
        state = controller.start(CID, spec(max_tool_calls=30))  # two runs at cap 20 = 40
    assert lab.engine.calls == [] and state["outcome"] == "no_failure_found"
    assert "remaining tool budget" in state["errors"][0]["message"]


def test_cancellation_terminates_cleanly(tmp_path):
    lab = Lab(tmp_path, Script(proposal()))
    with lab.controller() as controller:
        controller.cancel.set()
        state = controller.start(CID, spec())
    assert state["status"] == "cancelled" and lab.script.requests == []
    assert lab.engine.calls == []


def test_unbound_or_mismatched_identity_denies_the_run_without_mutation(tmp_path):
    binding = StaticPrincipalBinding({(PROJECT, OPERATOR.principal_id): "someone-else"})
    lab = Lab(tmp_path, Script(proposal()), binding=binding)
    with lab.controller() as controller:
        state = controller.start(CID, spec())
    assert state["status"] == "failed" and state["outcome"] == "policy_denied"
    assert state["errors"][-1]["code"] == "forbidden"
    assert state["experiments"] == {}
    assert lab.ledger.committed(KEY, "tool_calls")[0] == 0  # reserved, then reconciled to 0


def test_campaign_budget_above_supervisor_ceiling_is_refused(tmp_path):
    lab = Lab(tmp_path, Script())
    with lab.controller() as controller, pytest.raises(PolicyDenied):
        controller.start(CID, spec(max_model_calls=10_000))


# --- review fixes: no takeover, owner-only reads, no leaked reservations ------------

def test_start_cannot_take_over_or_restart_an_existing_campaign(tmp_path):
    lab, state = paused_lab(tmp_path)
    with lab.controller() as same, pytest.raises(CampaignExists):
        same.start(CID, spec())
    with lab.controller(session=TrustedSession(REVIEWER, PROJECT)) as other_user, \
            pytest.raises(CampaignExists):
        other_user.start(CID, spec())
    with lab.controller() as owner:
        kept = owner.state(CID)
    assert kept["paused"] and kept["principal_id"] == OPERATOR.principal_id
    assert kept["approval"]["approval_id"] == state["approval"]["approval_id"]


def test_state_and_record_are_owner_only(tmp_path):
    lab, _ = paused_lab(tmp_path)
    for session in (TrustedSession(OPERATOR, "project-other"), TrustedSession(REVIEWER, PROJECT)):
        with lab.controller(session=session) as other:
            with pytest.raises(PolicyDenied):
                other.state(CID)
            with pytest.raises(PolicyDenied):
                other.record(CID)


def test_unbound_principal_denies_and_leaves_no_open_reservation(tmp_path):
    lab = Lab(tmp_path, Script(proposal()), binding=StaticPrincipalBinding({}))
    with lab.controller() as controller:
        state = controller.start(CID, spec())
    assert state["status"] == "failed" and state["outcome"] == "policy_denied"
    assert lab.engine.calls  # the engine was reached and refused before any world call
    assert lab.ledger.committed(KEY, "tool_calls") == (0, 0)  # released, nothing open
