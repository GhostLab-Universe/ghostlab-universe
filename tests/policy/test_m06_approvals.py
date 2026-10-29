"""Approval lifecycle: bound, expiring, single-use, human-only, concurrency-safe."""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from ghostlab.contracts import ApprovalSubmission, Principal
from ghostlab.policy import (ApprovalDenied, ApprovalService, ApproverAuthority,
                             ProjectApproverAllowlist, action_digest, delegate, root_scope)

from m06_fakes import FakeApprovalStore, principal

PROJECT = "project-local"
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
OWNER = Principal.model_validate({"principal_id": "local-dev-user", "kind": "local_dev"})
ACTION = {"kind": "apply_repair", "adapter": "repaired", "world_version": "refund-world/1"}
# Explicit project approval authority (deny by default). reviewer-1 may also approve in
# project-other so cross-project lookups still exercise the store's isolation.
APPROVERS = ProjectApproverAllowlist({
    PROJECT: {"local-dev-user", *(f"reviewer-{i}" for i in range(24))},
    "project-other": {"reviewer-1"},
})


def make(store=None, *, local_dev_mode=False, ttl=timedelta(hours=1), approval_id="approval-1",
         policy_version="policy-1"):
    store = store or FakeApprovalStore()
    service = ApprovalService(store, approvers=APPROVERS, local_dev_mode=local_dev_mode,
                              policy_version=policy_version)
    analyst = delegate(root_scope(PROJECT, OWNER), "analyst", {"get_order"})
    request = service.request(
        scope=analyst, approval_id=approval_id, campaign_id="campaign-1", task_id="task-1",
        action_kind="apply_repair", action=ACTION, payload_ref_id="artifact-repair-1",
        summary="Switch to status-check-before-retry", material_risks=["Changes adapter"],
        now=NOW, ttl=ttl)
    return service, store, request


def submit(request, decision="approve", digest=None):
    return ApprovalSubmission.model_validate({
        "schema_version": 1, "approval_id": request.approval_id,
        "action_digest": digest or request.action_digest, "decision": decision, "reason": None})


def human(principal_id="reviewer-1"):
    return Principal.model_validate(principal(principal_id=principal_id))


def denied(code, fn):
    with pytest.raises(ApprovalDenied) as raised:
        fn()
    assert raised.value.code == code


def test_request_binds_exact_action_scope_policy_and_expiry():
    _, store, request = make()
    assert request.action_digest == action_digest(ACTION) == request.payload.sha256
    assert request.scope.role == "analyst" and request.policy_version == "policy-1"
    assert request.expires_at == "2026-10-29T11:00:00.000000Z"
    assert store.get(PROJECT, "approval-1").status == "pending"


def test_approve_then_consume_exactly_once():
    service, store, request = make()
    decision = service.decide(session_principal=human(), project_id=PROJECT,
                              submission=submit(request), decision_id="decision-1", now=NOW)
    assert decision.approver.principal_id == "reviewer-1"
    assert store.get_decision(PROJECT, "approval-1") == decision
    consumed = service.consume(project_id=PROJECT, approval_id="approval-1", action=ACTION,
                               now=NOW + timedelta(minutes=1))
    assert consumed.status == "consumed"
    denied("not_approved", lambda: service.consume(project_id=PROJECT, approval_id="approval-1",
                                                   action=ACTION, now=NOW))


def test_rejected_approval_cannot_resume_work():
    service, _, request = make()
    service.decide(session_principal=human(), project_id=PROJECT,
                   submission=submit(request, "reject"), decision_id="decision-1", now=NOW)
    denied("not_approved", lambda: service.consume(project_id=PROJECT, approval_id="approval-1",
                                                   action=ACTION, now=NOW))


def test_changed_action_requires_new_approval():
    service, store, request = make()
    service.decide(session_principal=human(), project_id=PROJECT, submission=submit(request),
                   decision_id="decision-1", now=NOW)
    changed = {**ACTION, "adapter": "vulnerable"}
    denied("digest_mismatch", lambda: service.consume(project_id=PROJECT,
                                                      approval_id="approval-1",
                                                      action=changed, now=NOW))
    assert store.get(PROJECT, "approval-1").status == "approved"  # denial mutated nothing


def test_decision_for_a_different_digest_fails():
    service, store, request = make()
    denied("digest_mismatch", lambda: service.decide(
        session_principal=human(), project_id=PROJECT,
        submission=submit(request, digest=action_digest({"other": 1})),
        decision_id="decision-1", now=NOW))
    assert store.get(PROJECT, "approval-1").status == "pending"


def test_expired_request_cannot_be_decided():
    service, store, request = make()
    denied("expired", lambda: service.decide(session_principal=human(), project_id=PROJECT,
                                             submission=submit(request), decision_id="d-1",
                                             now=NOW + timedelta(hours=1)))
    assert store.get(PROJECT, "approval-1").status == "expired"


def test_approval_expiring_before_use_cannot_be_consumed():
    service, store, request = make()
    service.decide(session_principal=human(), project_id=PROJECT, submission=submit(request),
                   decision_id="decision-1", now=NOW)
    denied("expired", lambda: service.consume(project_id=PROJECT, approval_id="approval-1",
                                              action=ACTION, now=NOW + timedelta(hours=2)))
    assert store.get(PROJECT, "approval-1").status == "approved"


def test_replayed_decision_fails():
    service, _, request = make()
    service.decide(session_principal=human(), project_id=PROJECT, submission=submit(request),
                   decision_id="decision-1", now=NOW)
    denied("not_pending", lambda: service.decide(session_principal=human("reviewer-2"),
                                                 project_id=PROJECT,
                                                 submission=submit(request, "reject"),
                                                 decision_id="decision-2", now=NOW))


@pytest.mark.parametrize("kind,local_dev_mode,allowed", [
    ("service", False, False), ("service", True, False),
    ("local_dev", False, False), ("local_dev", True, True), ("human", False, True),
])
def test_only_human_or_active_local_dev_principals_approve(kind, local_dev_mode, allowed):
    service, _, request = make(local_dev_mode=local_dev_mode)
    approver = Principal.model_validate(principal(kind=kind))
    decide = lambda: service.decide(session_principal=approver, project_id=PROJECT,  # noqa: E731
                                    submission=submit(request), decision_id="d-1", now=NOW)
    if allowed:
        assert decide().approver.kind == kind
    else:
        denied("approver_not_allowed", decide)


def test_cross_project_approval_is_not_found():
    service, _, request = make()
    denied("not_found", lambda: service.decide(session_principal=human(),
                                               project_id="project-other",
                                               submission=submit(request), decision_id="d-1",
                                               now=NOW))
    denied("not_found", lambda: service.consume(project_id="project-other",
                                                approval_id="approval-1", action=ACTION,
                                                now=NOW))


def test_roles_without_capability_cannot_request():
    service = ApprovalService(FakeApprovalStore(), approvers=APPROVERS)
    explorer = delegate(root_scope(PROJECT, OWNER), "explorer", set())
    denied("capability_denied", lambda: service.request(
        scope=explorer, approval_id="a-1", campaign_id="c-1", task_id="t-1",
        action_kind="apply_repair", action=ACTION, payload_ref_id="p-1", summary="s",
        material_risks=["r"], now=NOW, ttl=timedelta(minutes=5)))


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(hours=25)])
def test_ttl_bounds(ttl):
    denied("invalid_ttl", lambda: make(ttl=ttl))


def test_duplicate_approval_id_conflicts():
    service, store, _ = make()
    denied("conflict", lambda: make(store, approval_id="approval-1"))


def test_policy_change_invalidates_pending_and_approved():
    service, store, request = make()
    newer = ApprovalService(store, approvers=APPROVERS, policy_version="policy-2")
    denied("policy_changed", lambda: newer.decide(session_principal=human(), project_id=PROJECT,
                                                  submission=submit(request), decision_id="d-1",
                                                  now=NOW))
    service.decide(session_principal=human(), project_id=PROJECT, submission=submit(request),
                   decision_id="d-1", now=NOW)
    denied("policy_changed", lambda: newer.consume(project_id=PROJECT, approval_id="approval-1",
                                                   action=ACTION, now=NOW))


def _race(worker, count=24):
    barrier = threading.Barrier(count)
    results = []

    def run(index):
        barrier.wait()
        try:
            worker(index)
            results.append("ok")
        except ApprovalDenied as error:
            results.append(error.code)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_concurrent_consumers_succeed_exactly_once():
    service, store, request = make()
    service.decide(session_principal=human(), project_id=PROJECT, submission=submit(request),
                   decision_id="d-1", now=NOW)
    results = _race(lambda _: service.consume(project_id=PROJECT, approval_id="approval-1",
                                              action=ACTION, now=NOW))
    assert results.count("ok") == 1
    assert set(results) - {"ok"} <= {"conflict", "not_approved"}
    assert store.get(PROJECT, "approval-1").status == "consumed"


def test_concurrent_deciders_record_exactly_one_decision():
    service, store, request = make()
    results = _race(lambda i: service.decide(
        session_principal=human(f"reviewer-{i}"), project_id=PROJECT,
        submission=submit(request, "approve" if i % 2 else "reject"),
        decision_id=f"d-{i}", now=NOW))
    assert results.count("ok") == 1
    assert store.get(PROJECT, "approval-1").status in {"approved", "rejected"}


# --- review finding 4: approval authority is project-scoped, not implied by id/digest --------

def test_human_without_project_authority_cannot_approve():
    service, store, request = make()
    outsider = human("outsider-1")
    denied("approver_not_authorized", lambda: service.decide(
        session_principal=outsider, project_id=PROJECT, submission=submit(request),
        decision_id="d-x", now=NOW))
    assert store.get(PROJECT, "approval-1").status == "pending"
    assert store.get_decision(PROJECT, "approval-1") is None


def test_authority_in_another_project_does_not_transfer():
    # reviewer-1 may approve in project-other, but this request is owned by PROJECT;
    # a reviewer listed only for project-other cannot approve it by naming PROJECT.
    other_only = ProjectApproverAllowlist({"project-other": {"reviewer-1"}})
    store = FakeApprovalStore()
    _, _, request = make(store)
    service = ApprovalService(store, approvers=other_only)
    denied("approver_not_authorized", lambda: service.decide(
        session_principal=human("reviewer-1"), project_id=PROJECT, submission=submit(request),
        decision_id="d-x", now=NOW))
    assert store.get(PROJECT, "approval-1").status == "pending"


def test_unauthorized_caller_learns_nothing_about_approval_ids():
    service, _, request = make()
    for approval_id in ("approval-1", "approval-does-not-exist"):
        submission = submit(request).model_validate(
            {**submit(request).model_dump(), "approval_id": approval_id})
        denied("approver_not_authorized", lambda: service.decide(
            session_principal=human("outsider-1"), project_id=PROJECT, submission=submission,
            decision_id="d-x", now=NOW))


def test_authorized_owner_may_approve_own_action():
    # Separation of duties stays deferred: the listed owner can approve their own request.
    service, _, request = make(local_dev_mode=True)
    decision = service.decide(session_principal=OWNER, project_id=PROJECT,
                              submission=submit(request), decision_id="d-1", now=NOW)
    assert decision.approver == OWNER


def test_approver_authority_is_required_and_deny_by_default():
    with pytest.raises(TypeError):
        ApprovalService(FakeApprovalStore())  # no authority injected
    with pytest.raises(TypeError):
        ApprovalService(FakeApprovalStore(), approvers=object())
    empty = ProjectApproverAllowlist({})
    assert isinstance(empty, ApproverAuthority)
    assert not empty.may_approve(human(), PROJECT)
    listed = ProjectApproverAllowlist({PROJECT: ["reviewer-1"]})
    assert listed.may_approve(human(), PROJECT)
    assert not listed.may_approve(human(), "project-other")
