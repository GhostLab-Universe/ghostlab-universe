"""World-aware authorization: boundaries hold in every mode, business rules only relax
for a controller-labelled experiment, and nothing untrusted can grant authority."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from m06_fakes import FakeApprovalStore
from pydantic import ValidationError

from ghostlab.contracts import ApprovalSubmission, Principal, TrustedScope
from ghostlab.contracts.primitives import AgentRole
from ghostlab.contracts.world import (WorldExperimentProposal, WorldToolInvocation,
                                      WorldTrustedScope)
from ghostlab.contracts.world_packs import PACKS, WorldToolSpec, pack
from ghostlab.policy import (ENFORCED, LAB, POLICY_VERSION, ROLE_EFFECT_CEILINGS,
                             ApprovalDenied, ApprovalService, ExecutionMode, PolicyDenied,
                             ProjectApproverAllowlist, StaticWorldBinding, authorize_capability,
                             authorize_delivery, authorize_world, authorize_world_tool,
                             delegate_world, execution_mode_for, root_scope, supervisor_scope,
                             world_root_scope)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "contracts"
J = "group-journey/1"
PROJECT = "project-local"
OPERATOR = Principal(principal_id="operator-1", kind="human")
REVIEWER = Principal(principal_id="reviewer-1", kind="human")
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
BINDING = StaticWorldBinding({(PROJECT, "operator-1", J): "traveller-a"})
ALL_TOOLS = [t.name for t in pack(J).tools]
DELIVER = {"message_id": "msg-1"}
INSPECT = {"journey_id": "journey-1"}
TOOL_ARGUMENTS = {"inspect_constraints": INSPECT,
                  "hold_availability": {"journey_id": "j", "option_id": "o", "intent_version": 2},
                  "release_availability": {"hold_id": "h"},
                  "confirm_reservation": {"hold_id": "h", "intent_version": 2},
                  "update_calendar_event": {"participant_id": "p", "event_id": "e",
                                            "intent_version": 2},
                  "queue_message": {"recipient_id": "r", "template_id": "t"},
                  "deliver_message": DELIVER}


def example(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def target(tools=ALL_TOOLS):
    return delegate_world(world_root_scope(PROJECT, OPERATOR, J), "target", tools)


def decide(scope, tool, arguments, *, binding=BINDING, owner="traveller-a", project=PROJECT):
    return authorize_world_tool(scope, tool, arguments, project_id=project, binding=binding,
                                owner_id=owner)


# --- the runnable-world gate ----------------------------------------------------------

def test_only_implemented_worlds_pass_the_gate():
    assert authorize_world("refund-world/1").allowed and authorize_world(J).allowed
    assert authorize_world("service-relay/1").allowed
    assert authorize_world("creator-release/1").code == "world_not_runnable"


def only_specified(monkeypatch, world):
    designed = PACKS[world].model_copy(update={"status": "specified"})
    monkeypatch.setattr("ghostlab.contracts.world_packs.PACKS", {**PACKS, world: designed})


def test_a_pack_that_is_only_specified_does_not_pass_the_gate(monkeypatch):
    only_specified(monkeypatch, "service-relay/1")
    refused = authorize_world("service-relay/1")
    assert (refused.allowed, refused.code) == (False, "world_not_runnable")


def test_no_scope_is_issued_for_a_world_that_cannot_run(monkeypatch):
    only_specified(monkeypatch, "service-relay/1")
    with pytest.raises(PolicyDenied) as refused:
        world_root_scope(PROJECT, OPERATOR, "service-relay/1")
    assert refused.value.decision.code == "world_not_runnable"


def test_a_tool_is_refused_if_its_world_stops_being_runnable(monkeypatch):
    scope = target()
    monkeypatch.setattr("ghostlab.policy.world.is_executable", lambda world: False)
    assert decide(scope, "inspect_constraints", INSPECT).code == "world_not_runnable"


# --- tools ----------------------------------------------------------------------------

def test_the_bound_owner_may_call_every_tool_of_the_pack():
    assert set(TOOL_ARGUMENTS) == set(ALL_TOOLS)
    for tool, args in TOOL_ARGUMENTS.items():
        assert decide(target(), tool, args).allowed, tool


@pytest.mark.parametrize("tool, arguments, code", [
    ("issue_refund", {}, "unknown_tool"),
    (["deliver_message"], DELIVER, "unknown_tool"),
    ("deliver_message", {}, "invalid_arguments"),
    ("deliver_message", ["msg-1"], "invalid_arguments"),
    ("deliver_message", {"message_id": "msg-1", "actor_id": "traveller-b"}, "invalid_arguments"),
    ("hold_availability", {"journey_id": "j", "option_id": "o", "intent_version": True},
     "invalid_arguments"),
])
def test_unknown_tools_and_bad_arguments_are_denied(tool, arguments, code):
    assert decide(target(), tool, arguments).code == code


def test_a_tool_outside_the_scope_is_denied():
    assert decide(target(["inspect_constraints"]), "deliver_message",
                  DELIVER).code == "tool_not_in_scope"


def test_arguments_cannot_change_who_is_acting():
    for field in ("actor_id", "principal", "organizer", "owner_id"):
        assert decide(target(), "inspect_constraints",
                      {**INSPECT, field: "traveller-a"}).code == "invalid_arguments"


def test_ownership_stays_on_for_every_effect():
    viewer = StaticWorldBinding({(PROJECT, "operator-1", J): "participant-c"})
    assert decide(target(), "inspect_constraints", INSPECT, binding=viewer).allowed
    assert decide(target(), "deliver_message", DELIVER, binding=viewer).code == "identity_mismatch"
    assert decide(target(), "queue_message", {"recipient_id": "r", "template_id": "t"},
                  binding=viewer).code == "identity_mismatch"


def test_an_unbound_principal_is_denied_even_for_reads():
    assert decide(target(), "inspect_constraints", INSPECT,
                  binding=StaticWorldBinding({})).code == "unbound_principal"


def test_a_binding_for_one_world_or_project_does_not_carry_over():
    other_world = StaticWorldBinding({(PROJECT, "operator-1", "refund-world/1"): "traveller-a"})
    assert decide(target(), "inspect_constraints", INSPECT,
                  binding=other_world).code == "unbound_principal"
    other_project = StaticWorldBinding({("project-b", "operator-1", J): "traveller-a"})
    assert decide(target(), "inspect_constraints", INSPECT,
                  binding=other_project).code == "unbound_principal"


def test_a_scope_for_another_project_is_denied():
    assert decide(target(), "inspect_constraints", INSPECT,
                  project="project-b").code == "cross_project"


def test_a_refund_scope_holds_no_world_authority():
    assert decide(supervisor_scope(PROJECT, OPERATOR), "inspect_constraints",
                  INSPECT).code == "tool_not_in_scope"


def test_a_stale_policy_version_is_denied():
    data = target().model_dump()
    stale = WorldTrustedScope.model_validate({**data, "policy_version": "policy-0"})
    assert decide(stale, "inspect_constraints", INSPECT).code == "unknown_policy"


# --- delegation -----------------------------------------------------------------------

def test_role_ceilings_follow_tool_effects():
    root = world_root_scope(PROJECT, OPERATOR, J)
    reads = [t.name for t in pack(J).tools if t.effect == "read"]
    assert delegate_world(root, "analyst", reads).allowed_tools == reads
    with pytest.raises(PolicyDenied):
        delegate_world(root, "analyst", ["deliver_message"])
    for role in ("explorer", "worldsmith", "verifier"):
        assert ROLE_EFFECT_CEILINGS[role] == frozenset()
        with pytest.raises(PolicyDenied):
            delegate_world(root, role, reads)


def test_supervisor_authority_cannot_be_delegated_or_re_delegated():
    root = world_root_scope(PROJECT, OPERATOR, J)
    with pytest.raises(PolicyDenied):
        delegate_world(root, "supervisor", [])
    with pytest.raises(PolicyDenied):
        delegate_world(target(), "analyst", [])


def test_delegation_cannot_exceed_the_parent():
    narrow = WorldTrustedScope.model_validate({
        **world_root_scope(PROJECT, OPERATOR, J).model_dump(),
        "allowed_tools": ["inspect_constraints"]})
    with pytest.raises(PolicyDenied):
        delegate_world(narrow, "target", ["deliver_message"])


# --- delivery and execution mode ------------------------------------------------------

def test_delivery_is_decided_from_the_permission_at_this_event():
    scope = target()
    assert authorize_delivery(scope, project_id=PROJECT, sharing_permitted=True).allowed
    refused = authorize_delivery(scope, project_id=PROJECT, sharing_permitted=False)
    assert (refused.allowed, refused.code) == (False, "permission_withdrawn")
    assert refused.error_info().code == "forbidden"
    assert refused.error_info().side_effect == "none"


@pytest.mark.parametrize("value", [None, 1, "true", [True]])
def test_delivery_is_denied_unless_permission_is_exactly_true(value):
    assert not authorize_delivery(target(), project_id=PROJECT, sharing_permitted=value).allowed


REFUND = "refund-world/1"
CUSTOMER = "synthetic-customer-1"
REFUND_BINDING = StaticWorldBinding({(PROJECT, "operator-1", REFUND): CUSTOMER})


def refund_target():
    root = world_root_scope(PROJECT, OPERATOR, REFUND)
    return delegate_world(root, "target", root.allowed_tools)


@pytest.mark.parametrize("tool, arguments", [
    ("get_order", {"order_id": "order-1", "customer_id": "victim"}),
    ("issue_refund", {"order_id": "order-1", "customer_id": "victim", "amount_minor": 250_000,
                      "idempotency_key": "k"}),
    ("get_refund_status", {"order_id": "order-1", "customer_id": "victim",
                           "idempotency_key": "k"}),
])
def test_an_identity_argument_must_match_the_binding(tool, arguments):
    refused = decide(refund_target(), tool, arguments, binding=REFUND_BINDING, owner=CUSTOMER)
    assert refused.code == "identity_mismatch"
    own = {**arguments, "customer_id": CUSTOMER}
    assert decide(refund_target(), tool, own, binding=REFUND_BINDING, owner=CUSTOMER).allowed


def world_scope(role, tools, world=J):
    return WorldTrustedScope.model_validate({
        **world_root_scope(PROJECT, OPERATOR, J).model_dump(), "role": role,
        "delegated_by": "supervisor", "allowed_tools": tools, "world_version": world})


@pytest.mark.parametrize("scope, code", [
    (supervisor_scope(PROJECT, OPERATOR), "tool_not_in_scope"),
    (root_scope(PROJECT, OPERATOR), "tool_not_in_scope"),
    (world_scope("explorer", []), "tool_not_in_role_ceiling"),
    (world_scope("verifier", []), "tool_not_in_role_ceiling"),
    (world_scope("analyst", ["inspect_constraints"]), "tool_not_in_role_ceiling"),
    (world_scope("target", ["inspect_constraints"]), "tool_not_in_scope"),
    (world_scope("target", [], world="refund-world/1"), "tool_not_in_scope"),
    (world_scope("target", [], world="service-relay/1"), "tool_not_in_scope"),
])
def test_only_a_scope_that_can_deliver_may_deliver(scope, code):
    assert authorize_delivery(scope, project_id=PROJECT, sharing_permitted=True).code == code


def test_delivery_is_refused_in_a_world_that_cannot_run(monkeypatch):
    only_specified(monkeypatch, "service-relay/1")
    scope = world_scope("target", [], world="service-relay/1")
    assert authorize_delivery(scope, project_id=PROJECT,
                              sharing_permitted=True).code == "world_not_runnable"


def test_the_role_ceiling_is_checked_for_tools():
    analyst = world_scope("analyst", ["deliver_message"])
    assert decide(analyst, "deliver_message", DELIVER).code == "tool_not_in_role_ceiling"


@pytest.mark.parametrize("tool", [t.name for t in pack(J).tools if t.effect == "mutate"])
def test_an_analyst_is_read_only_for_mutate_tools(tool):
    scope = world_scope("analyst", [tool])  # granted the tool, so only the ceiling can refuse
    assert decide(scope, tool, TOOL_ARGUMENTS[tool]).code == "tool_not_in_role_ceiling"
    with pytest.raises(PolicyDenied) as refused:
        delegate_world(world_root_scope(PROJECT, OPERATOR, J), "analyst", [tool])
    assert refused.value.decision.code == "delegation_denied"


def test_policy_accepts_the_arguments_of_a_validated_invocation():
    # The policy decision only: dispatching these arguments into a world is not covered here.
    invocation = WorldToolInvocation.model_validate(
        example("world_tool_invocation.journey_deliver_message"))
    assert decide(target(), invocation.tool, invocation.arguments).allowed


def test_delegation_checks_the_world_and_the_parent(monkeypatch):
    root = world_root_scope(PROJECT, OPERATOR, J)
    relay = WorldTrustedScope.model_validate({**root.model_dump(),
                                              "world_version": "service-relay/1",
                                              "allowed_tools": []})
    only_specified(monkeypatch, "service-relay/1")
    with pytest.raises(PolicyDenied) as refused:
        delegate_world(relay, "target", [])
    assert refused.value.decision.code == "world_not_runnable"
    monkeypatch.undo()
    stale = WorldTrustedScope.model_validate({**root.model_dump(), "policy_version": "policy-0"})
    with pytest.raises(PolicyDenied) as refused:
        delegate_world(stale, "target", [])
    assert refused.value.decision.code == "unknown_policy"
    delegated = WorldTrustedScope.model_validate({**root.model_dump(),
                                                  "delegated_by": "supervisor"})
    with pytest.raises(PolicyDenied) as refused:
        delegate_world(delegated, "target", [])
    assert refused.value.decision.code == "delegation_denied"
    with pytest.raises(PolicyDenied):
        delegate_world(supervisor_scope(PROJECT, OPERATOR), "target", [])
    with pytest.raises(PolicyDenied):
        delegate_world(root, "not-a-role", ALL_TOOLS)


def test_an_undelegated_parent_that_is_not_a_supervisor_cannot_delegate():
    parent = WorldTrustedScope.model_validate({**world_scope("target", ALL_TOOLS).model_dump(),
                                               "delegated_by": None})
    with pytest.raises(PolicyDenied) as refused:
        delegate_world(parent, "analyst", ["inspect_constraints"])  # otherwise a valid request
    assert refused.value.decision.reason == "only an undelegated supervisor delegates"


def test_a_delegated_child_records_its_delegator():
    root = world_root_scope(PROJECT, OPERATOR, J)
    assert root.delegated_by is None
    assert delegate_world(root, "analyst", ["inspect_constraints"]).delegated_by == "supervisor"


def test_duplicate_tools_are_collapsed_not_an_error():
    root = world_root_scope(PROJECT, OPERATOR, J)
    scope = delegate_world(root, "target", ["inspect_constraints", "inspect_constraints"])
    assert scope.allowed_tools == ["inspect_constraints"]


def test_every_declared_effect_has_a_ceiling_decision():
    declared = set(get_args(WorldToolSpec.model_fields["effect"].annotation))
    assert {tool.effect for world in PACKS.values() for tool in world.tools} <= declared
    assert set(ROLE_EFFECT_CEILINGS) == set(get_args(AgentRole))
    assert dict(ROLE_EFFECT_CEILINGS) == {
        "supervisor": declared, "target": declared, "analyst": {"read"},
        "explorer": set(), "worldsmith": set(), "verifier": set()}
    assert "handoff" in ROLE_EFFECT_CEILINGS["target"]


def test_delivery_outside_the_project_is_denied():
    assert authorize_delivery(target(), project_id="project-b",
                              sharing_permitted=True).code == "cross_project"


def test_only_an_experiment_runs_unguarded():
    assert execution_mode_for("experiment") == LAB and LAB.guarded is False
    assert execution_mode_for("repair") == ENFORCED and ENFORCED.guarded is True
    assert execution_mode_for("operation") == ENFORCED
    assert execution_mode_for("anything-else") == ENFORCED  # deny by default


@pytest.mark.parametrize("mode, guarded", [("lab", True), ("enforced", False)])
def test_an_inconsistent_execution_mode_is_refused(mode, guarded):
    with pytest.raises(ValueError):
        ExecutionMode(mode, guarded=guarded)


def test_the_mode_is_recorded_as_plain_evidence():
    assert LAB.as_json() == {"mode": "lab", "guarded": False}
    assert ENFORCED.as_json() == {"mode": "enforced", "guarded": True}


# --- nothing untrusted grants authority -------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("guarded", False), ("execution_mode", "lab"), ("actor_id", "traveller-a"),
    ("scope", {}), ("allowed_tools", ["deliver_message"]), ("approved", True)])
def test_a_model_proposal_cannot_carry_enforcement_or_authority(field, value):
    proposal = example("world_experiment_proposal.group_journey_revocation")
    with pytest.raises(ValidationError):
        WorldExperimentProposal.model_validate({**proposal, field: value})


def test_a_proposal_cannot_set_enforcement_inside_its_scenario():
    proposal = example("world_experiment_proposal.group_journey_revocation")
    proposal["scenario"]["guarded"] = False
    with pytest.raises(ValidationError):
        WorldExperimentProposal.model_validate(proposal)


ACTION = {"kind": "apply_repair", "experiment_id": "exp-1", "adapter": "journey-version-checked",
          "world_version": J, "scenario": {}, "execution": ENFORCED.as_json()}


def approvals():
    return ApprovalService(FakeApprovalStore(), approvers=ProjectApproverAllowlist(
        {PROJECT: {REVIEWER.principal_id}}))


def requested(service):
    return service.request(
        scope=supervisor_scope(PROJECT, OPERATOR), approval_id="approval-1",
        campaign_id="campaign-1", task_id="task-1", action_kind="apply_repair", action=ACTION,
        payload_ref_id="artifact-1", summary="Run the repaired adapter guarded",
        material_risks=["Changes which adapter later runs use"], now=NOW,
        ttl=timedelta(hours=1))


def submission(request):
    return ApprovalSubmission(schema_version=1, approval_id=request.approval_id,
                              action_digest=request.action_digest, decision="approve",
                              reason=None)


def test_a_journey_repair_approval_holds_no_world_tools():
    request = requested(approvals())
    assert request.scope.allowed_tools == [] and type(request.scope) is TrustedScope
    assert request.scope.project_id == PROJECT and request.policy_version == POLICY_VERSION


def test_a_supervisor_scope_keeps_its_capabilities_without_tools():
    scope = supervisor_scope(PROJECT, OPERATOR)
    for capability in ("request_approval", "write_memory", "export_capsule"):
        assert authorize_capability(scope, capability, project_id=PROJECT).allowed


def test_an_approval_cannot_be_reused_to_run_unguarded():
    service = approvals()
    request = requested(service)
    service.decide(session_principal=REVIEWER, project_id=PROJECT,
                   submission=submission(request), decision_id="d-1", now=NOW)
    weakened = {**ACTION, "execution": LAB.as_json()}
    with pytest.raises(ApprovalDenied) as refused:
        service.consume(project_id=PROJECT, approval_id="approval-1", action=weakened, now=NOW)
    assert refused.value.code == "digest_mismatch"
    service.consume(project_id=PROJECT, approval_id="approval-1", action=ACTION, now=NOW)


def test_an_operator_off_the_allowlist_cannot_decide():
    # The allowlist refuses here, not the request: M06.md records that separation of
    # duties isn't enforced, so a requester on the allowlist could decide.
    service = approvals()
    request = requested(service)
    with pytest.raises(ApprovalDenied) as refused:
        service.decide(session_principal=OPERATOR, project_id=PROJECT,
                       submission=submission(request), decision_id="d-1", now=NOW)
    assert refused.value.code == "approver_not_authorized"


def test_every_runnable_pack_has_a_scope():
    for version, world in PACKS.items():
        if world.status == "implemented":
            assert world_root_scope(PROJECT, OPERATOR, version).world_version == version
