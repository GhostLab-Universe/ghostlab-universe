"""Deny-by-default authorization, identity binding, delegation and no-mutation denial."""

import json

import pytest

from ghostlab.contracts import Principal, ProposedToolCall, TrustedScope
from ghostlab.contracts.tools import GetOrderCall, IssueRefundCall
from ghostlab.core import Fixture, RefundWorld, Scenario
from ghostlab.policy import (POLICY_VERSION, ROLE_CAPABILITIES, ROLE_TOOL_CEILINGS, PolicyDenied,
                             StaticPrincipalBinding, authorize_capability,
                             authorize_proposed_call, authorize_tool, delegate, enforce,
                             root_scope)

PROJECT = "project-local"
FIXTURE = Fixture()
OWNER = Principal.model_validate({"principal_id": "local-dev-user", "kind": "local_dev"})
OTHER = Principal.model_validate({"principal_id": "someone-else", "kind": "human"})
BINDING = StaticPrincipalBinding({(PROJECT, OWNER.principal_id): FIXTURE.customer_id,
                                  (PROJECT, OTHER.principal_id): "synthetic-customer-2"})
REFUND = {"order_id": FIXTURE.order_id, "customer_id": FIXTURE.customer_id,
          "amount_minor": 250_000, "idempotency_key": "intent-1"}


def target_scope(principal=OWNER):
    return delegate(root_scope(PROJECT, principal), "target",
                    {"get_order", "issue_refund", "get_refund_status"})


def refund_call(**overrides):
    return IssueRefundCall.model_validate({"tool": "issue_refund",
                                           "arguments": {**REFUND, **overrides}})


def proposed(name, arguments):
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ProposedToolCall.model_validate({"provider_call_id": "call-1", "tool_name": name,
                                            "arguments_json": text})


def crafted_scope(**overrides):
    """A scope built directly (bypassing delegate) to prove the ceiling still applies."""
    base = {"project_id": PROJECT, "principal": OWNER.model_dump(), "role": "explorer",
            "delegated_by": "supervisor", "allowed_tools": ["issue_refund"],
            "policy_version": POLICY_VERSION, "issued_by": "controller"}
    return TrustedScope.model_validate({**base, **overrides})


def test_bound_target_may_refund():
    decision = authorize_tool(target_scope(), refund_call(), project_id=PROJECT, binding=BINDING)
    assert decision.allowed and decision.policy_version == POLICY_VERSION


@pytest.mark.parametrize("scope,project,code", [
    (target_scope(), "project-other", "cross_project"),
    (crafted_scope(role="target", policy_version="policy-0",
                   allowed_tools=["issue_refund"]), PROJECT, "unknown_policy"),
    (delegate(root_scope(PROJECT, OWNER), "target", {"get_order"}), PROJECT, "tool_not_in_scope"),
    (crafted_scope(), PROJECT, "tool_not_in_role_ceiling"),
    (crafted_scope(role="verifier"), PROJECT, "tool_not_in_role_ceiling"),
], ids=["cross-project", "unknown-policy", "not-granted", "explorer-ceiling", "verifier-ceiling"])
def test_denials(scope, project, code):
    decision = authorize_tool(scope, refund_call(), project_id=project, binding=BINDING)
    assert not decision.allowed and decision.code == code


def test_customer_argument_cannot_spoof_identity():
    spoof = refund_call(customer_id="synthetic-customer-2")
    assert authorize_tool(target_scope(), spoof, project_id=PROJECT,
                          binding=BINDING).code == "identity_mismatch"
    # Another real principal is bound to customer-2, but the scope is still OWNER's.
    assert authorize_tool(target_scope(OTHER), refund_call(), project_id=PROJECT,
                          binding=BINDING).code == "identity_mismatch"


def test_unbound_principal_denied():
    stranger = Principal.model_validate({"principal_id": "unbound", "kind": "human"})
    decision = authorize_tool(target_scope(stranger), refund_call(), project_id=PROJECT,
                              binding=BINDING)
    assert decision.code == "unbound_principal"


@pytest.mark.parametrize("name,arguments,code", [
    ("shell", {"cmd": "rm -rf /"}, "unknown_tool"),
    ("issue_refund ", REFUND, "unknown_tool"),
    ("issue_refund", "{not json", "invalid_arguments"),
    ("issue_refund", '{"amount_minor": NaN}', "invalid_arguments"),
    ("issue_refund", {**REFUND, "amount_minor": True}, "invalid_arguments"),
    ("issue_refund", {**REFUND, "amount_minor": 2500.0}, "invalid_arguments"),
    ("issue_refund", {**REFUND, "actor_id": "admin"}, "invalid_arguments"),
    ("issue_refund", {**REFUND, "customer_id": "synthetic-customer-2"}, "identity_mismatch"),
    ("issue_refund", ["not", "an", "object"], "invalid_arguments"),
])
def test_model_output_cannot_widen_or_spoof(name, arguments, code):
    decision, call = authorize_proposed_call(target_scope(), proposed(name, arguments),
                                             project_id=PROJECT, binding=BINDING)
    assert not decision.allowed and decision.code == code and call is None


def test_valid_model_proposal_becomes_typed_call():
    decision, call = authorize_proposed_call(target_scope(), proposed("issue_refund", REFUND),
                                             project_id=PROJECT, binding=BINDING)
    assert decision.allowed and call.arguments.amount_minor == 250_000


@pytest.mark.parametrize("parent,role,tools", [
    (lambda: root_scope(PROJECT, OWNER), "explorer", {"get_order"}),
    (lambda: root_scope(PROJECT, OWNER), "analyst", {"issue_refund"}),
    (lambda: root_scope(PROJECT, OWNER), "supervisor", set()),
    (lambda: target_scope(), "target", {"get_order"}),  # delegated scopes cannot re-delegate
    (lambda: delegate(root_scope(PROJECT, OWNER), "analyst", {"get_order"}), "verifier", set()),
], ids=["explorer-tools", "analyst-write", "supervisor-clone", "re-delegate", "worker-delegates"])
def test_delegation_escalation_denied(parent, role, tools):
    with pytest.raises(PolicyDenied) as raised:
        delegate(parent(), role, tools)
    assert raised.value.decision.code == "delegation_denied"


def test_delegation_cannot_exceed_parent_scope():
    narrow_parent = TrustedScope.model_validate({**root_scope(PROJECT, OWNER).model_dump(),
                                                 "allowed_tools": ["get_order"]})
    with pytest.raises(PolicyDenied):
        delegate(narrow_parent, "target", {"issue_refund"})


def test_workers_never_inherit_all_supervisor_authority():
    supervisor = ROLE_CAPABILITIES["supervisor"]
    for role, capabilities in ROLE_CAPABILITIES.items():
        if role != "supervisor":
            assert capabilities < supervisor
            assert ROLE_TOOL_CEILINGS[role] <= ROLE_TOOL_CEILINGS["supervisor"]
    delegated = delegate(root_scope(PROJECT, OWNER), "analyst", {"get_order"})
    for capability in ("delegate", "write_memory", "export_capsule"):
        assert authorize_capability(delegated, capability, project_id=PROJECT).code == \
            "capability_denied"
    assert authorize_capability(delegated, "read_memory", project_id=PROJECT).allowed
    assert authorize_capability(delegated, "read_memory",
                                project_id="project-other").code == "cross_project"


def test_policy_tables_are_read_only():
    with pytest.raises(TypeError):
        ROLE_TOOL_CEILINGS["explorer"] = frozenset({"issue_refund"})


@pytest.mark.parametrize("call", [
    refund_call(customer_id="synthetic-customer-2"),
    GetOrderCall.model_validate({"tool": "get_order", "arguments": {
        "order_id": FIXTURE.order_id, "customer_id": FIXTURE.customer_id}}),
], ids=["spoofed-refund", "tool-not-granted"])
def test_denial_causes_no_mutation(call):
    world = RefundWorld(FIXTURE, Scenario())
    calls = []

    def execute():
        calls.append(call.tool)
        return world.call(call.tool, call.arguments.model_dump())

    scope = delegate(root_scope(PROJECT, OWNER), "target", {"issue_refund"})
    decision = authorize_tool(scope, call, project_id=PROJECT, binding=BINDING)
    with pytest.raises(PolicyDenied):
        enforce(decision, execute)
    assert calls == [] and world.ledger == [] and world.trace == []
    error = decision.error_info()
    assert error.code == "forbidden" and error.side_effect == "none"


def test_allowed_call_executes_against_the_world():
    world = RefundWorld(FIXTURE, Scenario())
    call = refund_call()
    decision = authorize_tool(target_scope(), call, project_id=PROJECT, binding=BINDING)
    enforce(decision, lambda: world.call(call.tool, call.arguments.model_dump()))
    assert world.snapshot()["total_refunded_minor"] == 250_000
