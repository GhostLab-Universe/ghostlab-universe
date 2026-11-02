"""T5 candidate lifecycle: records, allowlist, store, service, harness."""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from ghostlab.adapters import register_refund_adapters
from ghostlab.candidates.allowlist import (TUNABLE_FIELDS, ChangeRefusal,
                                           check_tunable, code_identity_of,
                                           validate_change)
from ghostlab.candidates.demo_targets import (
    FIXED_ID, FIXED_VERSION, NAIVE_ID, NAIVE_VERSION, WORLD_VERSION,
    register_demo_targets)
from ghostlab.candidates.evaluate import run_candidate_evaluation
from ghostlab.candidates.records import (CandidateError, candidate_id,
                                         digest_of, validate_candidate,
                                         validate_evaluation,
                                         validate_promotion)
from ghostlab.candidates.service import (PROMOTION_ACTION_KIND,
                                         CandidateService, promotion_action)
from ghostlab.candidates.store import CandidateStore, CrossProjectError
from ghostlab.contracts import ApprovalDecision, ApprovalRequest
from ghostlab.extensions.registry import AdapterRegistry
from ghostlab.policy.approvals import (ApprovalDenied, ApprovalService,
                                       ProjectApproverAllowlist,
                                       action_digest)
from ghostlab.storage.interfaces import Conflict, NotFound

PROJECT = "cand-demo"
NOW = "2026-11-02T00:00:00+00:00"


def make_registry():
    registry = AdapterRegistry()
    register_refund_adapters(registry)
    register_demo_targets(registry)
    return registry


def endpoint(target_id, version, registry, config_digest=None):
    manifest = registry.target_manifest(WORLD_VERSION, target_id)
    assert manifest.version == version
    return {"target_id": target_id, "target_version": version,
            "code_identity": code_identity_of(manifest),
            "config_digest": config_digest}


def target_diff(parent, proposed):
    return [{"kind": "target", "path": "target",
             "before": {"target_id": parent["target_id"],
                        "target_version": parent["target_version"]},
             "after": {"target_id": proposed["target_id"],
                       "target_version": proposed["target_version"]}}]


def candidate_body(registry, **over):
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    body = {"schema_version": 1, "candidate_id": "cand-x",
            "project_id": PROJECT, "mode": "developer_supplied",
            "parent": parent, "proposed": proposed,
            "diff": target_diff(parent, proposed), "evidence_refs": [],
            "status": "proposed", "rejection": None,
            "created_by": "dev-1", "created_at": NOW, "digest": "pending"}
    body.update(over)
    body["digest"] = digest_of(body)
    return body


# -- records ------------------------------------------------------------


def test_candidate_record_validates():
    body = candidate_body(make_registry())
    assert validate_candidate(body)["mode"] == "developer_supplied"


def test_candidate_rejects_unknown_fields():
    body = candidate_body(make_registry())
    body["passed"] = True  # aggregate verdicts have no place on records
    with pytest.raises(CandidateError):
        validate_candidate(body)


def test_candidate_rejection_shape_enforced():
    body = candidate_body(make_registry(), status="rejected")
    with pytest.raises(CandidateError):
        validate_candidate(body)
    body["rejection"] = {"code": "not_allowlisted", "message": "no",
                         "rejected_by": "dev-1", "rejected_at": NOW}
    body["digest"] = digest_of(body)
    assert validate_candidate(body)["status"] == "rejected"


def test_candidate_rejects_secrets():
    body = candidate_body(make_registry())
    body["evidence_refs"] = [{"kind": "run", "id": "API_KEY=abc",
                              "sha256": None}]
    with pytest.raises(CandidateError):
        validate_candidate(body)


def test_promotion_decider_shape_enforced():
    body = {"schema_version": 1, "promotion_id": "promo-1",
            "candidate_id": "cand-1", "project_id": PROJECT,
            "schema_version": 1, "approval_id": "appr-1", "action_digest": "d" * 64,
            "status": "pending", "decided_by": "dev-1",
            "decided_at": NOW, "digest": "x"}
    with pytest.raises(CandidateError):
        validate_promotion(body)


# -- allowlist ------------------------------------------------------------


def test_developer_change_accepts_registered_version_step():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    resolved = validate_change(
        mode="developer_supplied", parent=parent, proposed=proposed,
        diff=target_diff(parent, proposed), registry=registry,
        world_version=WORLD_VERSION)
    assert resolved["proposed_manifest"].version == FIXED_VERSION


def test_developer_change_accepts_allowlisted_tunable_overlay():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry,
                        config_digest="a" * 64)
    diff = target_diff(parent, proposed) + [
        {"kind": "field", "path": "run.max_tool_calls",
         "before": 20, "after": 50}]
    validate_change(mode="developer_supplied", parent=parent,
                    proposed=proposed, diff=diff, registry=registry,
                    world_version=WORLD_VERSION)


def test_out_of_allowlist_change_rejected():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    diff = target_diff(parent, proposed) + [
        {"kind": "field", "path": "run.evil_knob", "before": 1,
         "after": 2}]
    with pytest.raises(ChangeRefusal) as error:
        validate_change(mode="developer_supplied", parent=parent,
                        proposed=proposed, diff=diff, registry=registry,
                        world_version=WORLD_VERSION)
    assert error.value.code == "not_allowlisted"


def test_escape_attempt_refused():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    for path in ("agent.endpoint", "run.tools", "code.patch",
                 "run.model", "agent.system_prompt"):
        diff = target_diff(parent, proposed) + [
            {"kind": "field", "path": path, "before": 1, "after": 2}]
        with pytest.raises(ChangeRefusal) as error:
            validate_change(mode="developer_supplied", parent=parent,
                            proposed=proposed, diff=diff, registry=registry,
                            world_version=WORLD_VERSION)
        assert error.value.code == "escape_attempt", path


def test_phantom_version_refused():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = dict(endpoint(FIXED_ID, FIXED_VERSION, registry))
    proposed["target_version"] = "demo-refund-targets/9"
    with pytest.raises(ChangeRefusal) as error:
        validate_change(mode="developer_supplied", parent=parent,
                        proposed=proposed,
                        diff=target_diff(parent, proposed),
                        registry=registry, world_version=WORLD_VERSION)
    assert error.value.code == "version_mismatch"


def test_unregistered_target_refused():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = dict(parent)
    proposed["target_id"] = "ghost-target"
    with pytest.raises(ChangeRefusal) as error:
        validate_change(mode="developer_supplied", parent=parent,
                        proposed=proposed,
                        diff=target_diff(parent, proposed),
                        registry=registry, world_version=WORLD_VERSION)
    assert error.value.code == "unknown_target"


def test_no_change_refused():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    with pytest.raises(ChangeRefusal) as error:
        validate_change(mode="developer_supplied", parent=parent,
                        proposed=dict(parent),
                        diff=[{"kind": "field",
                               "path": "run.max_tool_calls", "before": 20,
                               "after": 50}],
                        registry=registry, world_version=WORLD_VERSION)
    assert error.value.code == "no_change"


def test_generated_config_contract_is_config_only():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry,
                      config_digest="b" * 64)
    proposed = endpoint(NAIVE_ID, NAIVE_VERSION, registry,
                        config_digest="c" * 64)
    validate_change(
        mode="generated_config", parent=parent, proposed=proposed,
        diff=[{"kind": "field", "path": "agent.max_retries", "before": 0,
               "after": 2}],
        registry=registry, world_version=WORLD_VERSION)
    fixed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    with pytest.raises(ChangeRefusal) as error:
        validate_change(mode="generated_config", parent=parent,
                        proposed=fixed,
                        diff=target_diff(parent, fixed),
                        registry=registry, world_version=WORLD_VERSION)
    assert error.value.code == "target_change_forbidden"


def test_generated_code_always_refused():
    registry = make_registry()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    fixed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    diff = target_diff(parent, fixed) + [
        {"kind": "code", "path": "agent.patch", "before": "",
         "after": "print(1)"}]
    for mode in ("developer_supplied", "generated_config"):
        same = parent if mode == "generated_config" else fixed
        use_diff = ([{"kind": "code", "path": "agent.patch",
                      "before": "", "after": "x"}]
                    if mode == "generated_config" else diff)
        with pytest.raises(ChangeRefusal) as error:
            validate_change(mode=mode, parent=parent, proposed=same,
                            diff=use_diff, registry=registry,
                            world_version=WORLD_VERSION)
        assert error.value.code == "generated_code_refused"


def test_tunable_bounds_checked():
    check_tunable("run.max_tool_calls", 50)
    with pytest.raises(ChangeRefusal) as error:
        check_tunable("run.max_tool_calls", 5000)
    assert error.value.code == "out_of_bounds"
    assert set(TUNABLE_FIELDS) == {
        "run.max_tool_calls", "run.max_wall_clock_ms", "run.repetitions",
        "agent.timeout_ms", "agent.max_retries"}


# -- store ------------------------------------------------------------


def test_store_round_trip_and_idempotent_propose(tmp_path):
    store = CandidateStore(tmp_path / "cand.db")
    registry = make_registry()
    first = store.save_candidate(project_id=PROJECT,
                                 body=candidate_body(registry))
    second = store.save_candidate(project_id=PROJECT,
                                  body=candidate_body(registry))
    assert first["candidate_id"] == second["candidate_id"]
    assert store.get_candidate(PROJECT, first["candidate_id"]) == first
    assert [c["candidate_id"] for c in store.list_candidates(PROJECT)] == [
        first["candidate_id"]]


def test_store_refuses_cross_project(tmp_path):
    store = CandidateStore(tmp_path / "cand.db")
    body = candidate_body(make_registry())
    store.save_candidate(project_id=PROJECT, body=body)
    with pytest.raises(CrossProjectError):
        store.get_candidate("other-project", body["candidate_id"])
    assert store.list_candidates("other-project") == []


def test_store_transition_edges(tmp_path):
    store = CandidateStore(tmp_path / "cand.db")
    body = candidate_body(make_registry())
    stored = store.save_candidate(project_id=PROJECT, body=body)
    moved = store.transition_candidate(
        PROJECT, stored["candidate_id"], expected="proposed",
        new_status="validated")
    assert moved["status"] == "validated"
    with pytest.raises(CandidateError):
        store.transition_candidate(PROJECT, stored["candidate_id"],
                                   expected="proposed",
                                   new_status="promoted")
    with pytest.raises(CandidateError):
        store.transition_candidate(PROJECT, stored["candidate_id"],
                                   expected="validated",
                                   new_status="proposed")


# -- evaluation harness -------------------------------------------------


ORIGINAL = {"case_id": "timeout-after-commit",
            "fixture": {"order_id": "synthetic-order-1",
                        "customer_id": "synthetic-customer-1",
                        "paid_minor": 250_000, "currency": "INR"},
            "scenario": {"timeout_after_commit": True, "max_tool_calls": 20},
            "actor_id": "synthetic-customer-1",
            "task": {"task_id": "refund-1",
                     "params": {"order_id": "synthetic-order-1",
                                "customer_id": "synthetic-customer-1",
                                "amount_minor": 250_000}}}

REGRESSIONS = [{"case_id": "clean-refund",
                "fixture": dict(ORIGINAL["fixture"]),
                "scenario": {"timeout_after_commit": False,
                             "max_tool_calls": 20},
                "actor_id": "synthetic-customer-1",
                "task": {"task_id": "refund-2",
                         "params": dict(ORIGINAL["task"]["params"])}}]


def run_demo_evaluation(registry, candidate_id="cand-demo-1"):
    return run_candidate_evaluation(
        registry=registry, world_version=WORLD_VERSION, project_id=PROJECT,
        candidate_id=candidate_id, parent_id=NAIVE_ID, proposed_id=FIXED_ID,
        original=ORIGINAL, regressions=REGRESSIONS, cap=20,
        evaluated_by="dev-1", now_iso=NOW)


def test_harness_shows_fix_and_keeps_dimensions():
    evaluation = run_demo_evaluation(make_registry())
    validate_evaluation(evaluation)
    parent_checks = {c["name"]: c["passed"]
                     for c in evaluation["original"]["parent"]["checks"]}
    cand_checks = {c["name"]: c["passed"]
                   for c in evaluation["original"]["candidate"]["checks"]}
    assert parent_checks["total_refund_within_payment"] is False
    assert parent_checks["no_duplicate_full_refund"] is False
    assert all(cand_checks.values())
    assert "passed" not in evaluation  # no aggregate hiding dimensions
    assert evaluation["completion"]["candidate"]["status"] == "completed"
    assert evaluation["usage"]["cap"] == 20
    assert evaluation["usage"]["parent"]["tool_calls"] > 0
    assert evaluation["fault_coverage"]["scenario_faults"] == [
        "timeout_after_commit"]
    assert len(evaluation["regressions"]) == 1


def test_harness_is_deterministic():
    first = run_demo_evaluation(make_registry())
    second = run_demo_evaluation(make_registry())
    assert first == second


# -- service ------------------------------------------------------------


class FakeApprovalStore:
    """Minimal ghostlab-storage/1 approval store for lifecycle tests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = {}
        self._decisions = {}

    def create(self, request):
        with self._lock:
            key = (request.scope.project_id, request.approval_id)
            if request.status != "pending" or key in self._requests:
                raise Conflict("exists")
            self._requests[key] = request

    def get(self, project_id, approval_id):
        with self._lock:
            try:
                return self._requests[(project_id, approval_id)]
            except KeyError:
                raise NotFound("missing") from None

    def get_decision(self, project_id, approval_id):
        with self._lock:
            return self._decisions.get((project_id, approval_id))

    def _move(self, project_id, approval_id, expected, new, digest=None):
        key = (project_id, approval_id)
        current = self._requests.get(key)
        if current is None:
            raise NotFound("missing")
        if current.status != expected or (
                digest is not None and digest != current.action_digest):
            raise Conflict("precondition")
        updated = ApprovalRequest.model_validate(
            {**current.model_dump(), "status": new})
        self._requests[key] = updated
        return updated

    def record_decision(self, project_id, decision, *, new_status):
        with self._lock:
            updated = self._move(project_id, decision.approval_id,
                                 "pending", new_status,
                                 decision.action_digest)
            self._decisions[(project_id, decision.approval_id)] = decision
            return updated

    def mark_expired(self, project_id, approval_id):
        with self._lock:
            return self._move(project_id, approval_id, "pending", "expired")

    def consume(self, project_id, approval_id, action_digest):
        with self._lock:
            return self._move(project_id, approval_id, "approved",
                              "consumed", action_digest)


def make_scope(project_id=PROJECT, principal_id="dev-1"):
    from ghostlab.contracts import Principal, TrustedScope
    return TrustedScope.model_validate({
        "project_id": project_id,
        "principal": {"principal_id": principal_id, "kind": "human"},
        "role": "analyst", "delegated_by": None, "allowed_tools": [],
        "policy_version": "policy-1", "issued_by": "controller"})


def make_service(tmp_path, approvers=None):
    registry = make_registry()
    store = CandidateStore(tmp_path / "cand.db")
    approvals = ApprovalService(
        FakeApprovalStore(),
        approvers=approvers or ProjectApproverAllowlist(
            {PROJECT: ["dev-1"]}))
    return CandidateService(store, registry, WORLD_VERSION,
                            approvals=approvals), approvals


def propose_demo(service, scope, registry, **over):
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    return service.propose(
        scope=scope, mode="developer_supplied", parent=parent,
        proposed=proposed, diff=target_diff(parent, proposed),
        evidence_refs=[], now_iso=NOW, **over)


def test_service_lifecycle_to_promotion(tmp_path):
    service, approvals = make_service(tmp_path)
    registry = make_registry()
    scope = make_scope()
    candidate = propose_demo(service, scope, registry)
    assert candidate["status"] == "proposed"
    assert candidate["created_by"] == "dev-1"
    assert service.validate(
        scope=scope, candidate_id=candidate["candidate_id"],
        now_iso=NOW)["status"] == "validated"
    evaluation = run_candidate_evaluation(
        registry=registry, world_version=WORLD_VERSION, project_id=PROJECT,
        candidate_id=candidate["candidate_id"], parent_id=NAIVE_ID,
        proposed_id=FIXED_ID, original=ORIGINAL, regressions=REGRESSIONS,
        cap=20, evaluated_by="dev-1", now_iso=NOW)
    service.record_evaluation(scope=scope, evaluation=evaluation)
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    promotion = service.request_promotion(
        scope=scope, candidate_id=candidate["candidate_id"],
        evaluation_id=evaluation["evaluation_id"], approval_id="appr-1",
        campaign_id="camp-1", task_id="task-1",
        summary="promote demo fix", material_risks=["none"],
        now=now, ttl=timedelta(hours=1))
    assert promotion["status"] == "pending"
    action = promotion_action(
        promotion_id=promotion["promotion_id"],
        candidate_id=candidate["candidate_id"], project_id=PROJECT,
        parent=candidate["parent"], proposed=candidate["proposed"],
        evaluation_digest=evaluation["digest"])
    assert promotion["action_digest"] == action_digest(action)
    from ghostlab.contracts import ApprovalSubmission
    approvals.decide(
        session_principal=scope.principal, project_id=PROJECT,
        submission=ApprovalSubmission.model_validate({
            "schema_version": 1, "approval_id": "appr-1",
            "action_digest": promotion["action_digest"],
            "decision": "approve", "reason": "fix verified"}),
        decision_id="dec-1", now=now + timedelta(minutes=1))
    done = service.complete_promotion(
        project_id=PROJECT, candidate_id=candidate["candidate_id"],
        promotion_id=promotion["promotion_id"], action=action,
        now=now + timedelta(minutes=2), decided_by="dev-1",
        decided_at=NOW)
    assert done["status"] == "completed"
    assert service.store.get_candidate(
        PROJECT, candidate["candidate_id"])["status"] == "promoted"


def test_promotion_is_single_use(tmp_path):
    service, approvals = make_service(tmp_path)
    registry = make_registry()
    scope = make_scope()
    candidate = propose_demo(service, scope, registry)
    service.validate(scope=scope, candidate_id=candidate["candidate_id"],
                     now_iso=NOW)
    evaluation = run_candidate_evaluation(
        registry=registry, world_version=WORLD_VERSION, project_id=PROJECT,
        candidate_id=candidate["candidate_id"], parent_id=NAIVE_ID,
        proposed_id=FIXED_ID, original=ORIGINAL, regressions=REGRESSIONS,
        cap=20, evaluated_by="dev-1", now_iso=NOW)
    service.record_evaluation(scope=scope, evaluation=evaluation)
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    promotion = service.request_promotion(
        scope=scope, candidate_id=candidate["candidate_id"],
        evaluation_id=evaluation["evaluation_id"], approval_id="appr-1",
        campaign_id="camp-1", task_id="task-1", summary="promote",
        material_risks=["demo promotion; no production effect"], now=now, ttl=timedelta(hours=1))
    action = promotion_action(
        promotion_id=promotion["promotion_id"],
        candidate_id=candidate["candidate_id"], project_id=PROJECT,
        parent=candidate["parent"], proposed=candidate["proposed"],
        evaluation_digest=evaluation["digest"])
    from ghostlab.contracts import ApprovalSubmission
    approvals.decide(
        session_principal=scope.principal, project_id=PROJECT,
        submission=ApprovalSubmission.model_validate({
            "schema_version": 1, "approval_id": "appr-1",
            "action_digest": promotion["action_digest"],
            "decision": "approve", "reason": "ok"}),
        decision_id="dec-1", now=now + timedelta(minutes=1))
    service.complete_promotion(
        project_id=PROJECT, candidate_id=candidate["candidate_id"],
        promotion_id=promotion["promotion_id"], action=action,
        now=now + timedelta(minutes=2), decided_by="dev-1",
        decided_at=NOW)
    with pytest.raises(ApprovalDenied):
        service.complete_promotion(
            project_id=PROJECT, candidate_id=candidate["candidate_id"],
            promotion_id=promotion["promotion_id"], action=action,
            now=now + timedelta(minutes=3), decided_by="dev-1",
            decided_at=NOW)


def test_changed_action_needs_new_approval(tmp_path):
    service, approvals = make_service(tmp_path)
    registry = make_registry()
    scope = make_scope()
    candidate = propose_demo(service, scope, registry)
    service.validate(scope=scope, candidate_id=candidate["candidate_id"],
                     now_iso=NOW)
    evaluation = run_candidate_evaluation(
        registry=registry, world_version=WORLD_VERSION, project_id=PROJECT,
        candidate_id=candidate["candidate_id"], parent_id=NAIVE_ID,
        proposed_id=FIXED_ID, original=ORIGINAL, regressions=REGRESSIONS,
        cap=20, evaluated_by="dev-1", now_iso=NOW)
    service.record_evaluation(scope=scope, evaluation=evaluation)
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    promotion = service.request_promotion(
        scope=scope, candidate_id=candidate["candidate_id"],
        evaluation_id=evaluation["evaluation_id"], approval_id="appr-1",
        campaign_id="camp-1", task_id="task-1", summary="promote",
        material_risks=["demo promotion; no production effect"], now=now, ttl=timedelta(hours=1))
    action = promotion_action(
        promotion_id=promotion["promotion_id"],
        candidate_id=candidate["candidate_id"], project_id=PROJECT,
        parent=candidate["parent"], proposed=candidate["proposed"],
        evaluation_digest="f" * 64)  # tampered evaluation binding
    from ghostlab.contracts import ApprovalSubmission
    approvals.decide(
        session_principal=scope.principal, project_id=PROJECT,
        submission=ApprovalSubmission.model_validate({
            "schema_version": 1, "approval_id": "appr-1",
            "action_digest": promotion["action_digest"],
            "decision": "approve", "reason": "ok"}),
        decision_id="dec-1", now=now + timedelta(minutes=1))
    with pytest.raises(ApprovalDenied) as error:
        service.complete_promotion(
            project_id=PROJECT, candidate_id=candidate["candidate_id"],
            promotion_id=promotion["promotion_id"], action=action,
            now=now + timedelta(minutes=2), decided_by="dev-1",
            decided_at=NOW)
    assert error.value.code == "digest_mismatch"
    assert service.store.get_promotion(
        PROJECT, promotion["promotion_id"])["status"] == "pending"


def test_service_rejects_out_of_allowlist_candidate(tmp_path):
    service, _ = make_service(tmp_path)
    registry = make_registry()
    scope = make_scope()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    candidate = service.propose(
        scope=scope, mode="developer_supplied", parent=parent,
        proposed=proposed,
        diff=target_diff(parent, proposed) + [
            {"kind": "field", "path": "run.evil_knob", "before": 1,
             "after": 2}],
        evidence_refs=[], now_iso=NOW)
    rejected = service.validate(scope=scope,
                                candidate_id=candidate["candidate_id"],
                                now_iso=NOW)
    assert rejected["status"] == "rejected"
    assert rejected["rejection"]["code"] == "not_allowlisted"
    assert rejected["rejection"]["rejected_by"] == "dev-1"


def test_service_authority_comes_from_scope(tmp_path):
    service, _ = make_service(tmp_path)
    registry = make_registry()
    scope = make_scope()
    parent = endpoint(NAIVE_ID, NAIVE_VERSION, registry)
    proposed = endpoint(FIXED_ID, FIXED_VERSION, registry)
    candidate = service.propose(
        scope=scope, mode="developer_supplied", parent=parent,
        proposed=proposed, diff=target_diff(parent, proposed),
        evidence_refs=[], now_iso=NOW)
    service.validate(scope=scope, candidate_id=candidate["candidate_id"],
                     now_iso=NOW)
    honest = run_candidate_evaluation(
        registry=registry, world_version=WORLD_VERSION, project_id=PROJECT,
        candidate_id=candidate["candidate_id"], parent_id=NAIVE_ID,
        proposed_id=FIXED_ID, original=ORIGINAL, regressions=REGRESSIONS,
        cap=20, evaluated_by="dev-1", now_iso=NOW)
    other = make_scope(principal_id="mallory")
    with pytest.raises(CandidateError):
        service.record_evaluation(scope=other, evaluation=honest)
    # mallory's project cannot even see the candidate
    with pytest.raises(CrossProjectError):
        service.store.get_candidate("mallory-project",
                                    candidate["candidate_id"])


def test_rejected_approval_returns_candidate_to_evaluated(tmp_path):
    service, approvals = make_service(tmp_path)
    registry = make_registry()
    scope = make_scope()
    candidate = propose_demo(service, scope, registry)
    service.validate(scope=scope, candidate_id=candidate["candidate_id"],
                     now_iso=NOW)
    evaluation = run_candidate_evaluation(
        registry=registry, world_version=WORLD_VERSION, project_id=PROJECT,
        candidate_id=candidate["candidate_id"], parent_id=NAIVE_ID,
        proposed_id=FIXED_ID, original=ORIGINAL, regressions=REGRESSIONS,
        cap=20, evaluated_by="dev-1", now_iso=NOW)
    service.record_evaluation(scope=scope, evaluation=evaluation)
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    promotion = service.request_promotion(
        scope=scope, candidate_id=candidate["candidate_id"],
        evaluation_id=evaluation["evaluation_id"], approval_id="appr-1",
        campaign_id="camp-1", task_id="task-1", summary="promote",
        material_risks=["demo promotion; no production effect"], now=now, ttl=timedelta(hours=1))
    from ghostlab.contracts import ApprovalSubmission
    approvals.decide(
        session_principal=scope.principal, project_id=PROJECT,
        submission=ApprovalSubmission.model_validate({
            "schema_version": 1, "approval_id": "appr-1",
            "action_digest": promotion["action_digest"],
            "decision": "reject", "reason": "not yet"}),
        decision_id="dec-1", now=now + timedelta(minutes=1))
    dropped = service.abandon_promotion(
        scope=scope, candidate_id=candidate["candidate_id"],
        promotion_id=promotion["promotion_id"], now_iso=NOW)
    assert dropped["status"] == "rejected"
    assert service.store.get_candidate(
        PROJECT, candidate["candidate_id"])["status"] == "evaluated"


def test_promotion_action_kind_is_stable():
    assert PROMOTION_ACTION_KIND == "execute_consequential_action"
