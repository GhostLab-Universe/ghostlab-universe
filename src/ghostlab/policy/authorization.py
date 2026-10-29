"""Deny-by-default tool and resource authorization (policy ``policy-1``).

Authority comes only from a controller-issued ``TrustedScope`` and the
controller's principal binding. Tool arguments, retrieved text and model output
are data: they can narrow nothing into existence and can never widen a scope.
This module decides; M02 executes and M05 routes.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol, TypeVar

from pydantic import TypeAdapter, ValidationError

from ghostlab.contracts import ErrorInfo, Principal, ProposedToolCall, TrustedScope
from ghostlab.contracts.primitives import AgentRole, ToolName
from ghostlab.contracts.tools import ToolCall

POLICY_VERSION = "policy-1"

ALL_TOOLS: frozenset[str] = frozenset({"get_order", "issue_refund", "get_refund_status"})

# Most tools any scope for a role may hold, however it was issued.
ROLE_TOOL_CEILINGS: Mapping[str, frozenset[str]] = MappingProxyType({
    "supervisor": ALL_TOOLS,
    "target": ALL_TOOLS,  # the system under test acts on the synthetic world
    "analyst": frozenset({"get_order", "get_refund_status"}),  # read-only
    "explorer": frozenset(),  # proposes experiments; never acts
    "worldsmith": frozenset(),  # proposes reviewed specifications; never acts
    "verifier": frozenset(),  # reads controller-owned state, not tools
})

Capability = Literal["delegate", "write_memory", "read_memory", "read_evidence",
                     "request_approval", "export_capsule"]

# Non-tool capabilities. Workers never hold the supervisor-only ones.
ROLE_CAPABILITIES: Mapping[str, frozenset[str]] = MappingProxyType({
    "supervisor": frozenset({"delegate", "write_memory", "read_memory", "read_evidence",
                             "request_approval", "export_capsule"}),
    "explorer": frozenset({"read_memory", "read_evidence"}),
    "worldsmith": frozenset({"read_memory", "read_evidence"}),
    "analyst": frozenset({"read_memory", "read_evidence", "request_approval"}),
    "target": frozenset(),
    "verifier": frozenset({"read_evidence"}),
})

SUPERVISOR_ONLY: frozenset[str] = frozenset({"delegate", "write_memory", "export_capsule"})

DenialCode = Literal[
    "unknown_policy", "cross_project", "unknown_tool", "invalid_arguments",
    "tool_not_in_scope", "tool_not_in_role_ceiling", "unbound_principal",
    "identity_mismatch", "capability_denied", "delegation_denied",
]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: Literal["allowed"] | DenialCode
    reason: str
    policy_version: str = POLICY_VERSION

    def error_info(self) -> ErrorInfo:
        if self.allowed:
            raise ValueError("An allowed decision has no error")
        code = "validation_failed" if self.code in {"unknown_tool", "invalid_arguments"} \
            else "forbidden"
        return ErrorInfo.model_validate({"code": code, "message": f"{self.code}: {self.reason}",
                                         "retryable": False, "side_effect": "none",
                                         "ref": None})


def allow(reason: str = "permitted by policy") -> Decision:
    return Decision(True, "allowed", reason)


def deny(code: DenialCode, reason: str) -> Decision:
    return Decision(False, code, reason)


class PolicyDenied(Exception):
    def __init__(self, decision: Decision):
        super().__init__(f"{decision.code}: {decision.reason}")
        self.decision = decision


class PrincipalBinding(Protocol):
    """Controller-owned map from a session principal to its synthetic-world identity.

    M02 consults this instead of trusting a ``customer_id`` tool argument.
    """

    def customer_id_for(self, project_id: str, principal: Principal) -> str | None: ...


class StaticPrincipalBinding:
    def __init__(self, bindings: Mapping[tuple[str, str], str]):
        self._bindings = MappingProxyType(dict(bindings))

    def customer_id_for(self, project_id: str, principal: Principal) -> str | None:
        return self._bindings.get((project_id, principal.principal_id))


def _scope_check(scope: TrustedScope, project_id: str) -> Decision | None:
    if scope.policy_version != POLICY_VERSION:
        return deny("unknown_policy", f"scope issued under {scope.policy_version!r}")
    if scope.project_id != project_id:
        return deny("cross_project", "scope does not cover the requested project")
    return None


def authorize_capability(scope: TrustedScope, capability: Capability, *,
                         project_id: str) -> Decision:
    if (failure := _scope_check(scope, project_id)) is not None:
        return failure
    if capability not in ROLE_CAPABILITIES.get(scope.role, frozenset()):
        return deny("capability_denied", f"role {scope.role!r} lacks {capability!r}")
    if capability in SUPERVISOR_ONLY and scope.delegated_by is not None:
        return deny("capability_denied", f"{capability!r} is never delegated")
    return allow()


def authorize_tool(scope: TrustedScope, call: ToolCall, *, project_id: str,
                   binding: PrincipalBinding) -> Decision:
    """Decide one already-typed tool call. Unknown tools cannot reach this type."""
    if (failure := _scope_check(scope, project_id)) is not None:
        return failure
    if call.tool not in scope.allowed_tools:
        return deny("tool_not_in_scope", f"{call.tool!r} not granted to this scope")
    if call.tool not in ROLE_TOOL_CEILINGS.get(scope.role, frozenset()):
        return deny("tool_not_in_role_ceiling", f"role {scope.role!r} may not call {call.tool!r}")
    bound = binding.customer_id_for(project_id, scope.principal)
    if bound is None:
        return deny("unbound_principal", "no world identity is bound to this principal")
    if call.arguments.customer_id != bound:
        return deny("identity_mismatch", "customer_id argument differs from the bound principal")
    return allow()


_TOOL_CALL = TypeAdapter(ToolCall)


def _reject_constant(value: str):
    raise ValueError(f"non-standard JSON constant {value}")


def parse_proposed_call(proposed: ProposedToolCall) -> ToolCall | Decision:
    """Turn untrusted model output into a typed call, or a denial."""
    if proposed.tool_name not in ALL_TOOLS:
        return deny("unknown_tool", "tool is not on the allowlist")
    try:
        arguments = json.loads(proposed.arguments_json, parse_constant=_reject_constant)
        return _TOOL_CALL.validate_python({"tool": proposed.tool_name, "arguments": arguments})
    except (ValueError, ValidationError):
        return deny("invalid_arguments", "arguments do not match the tool schema")


def authorize_proposed_call(scope: TrustedScope, proposed: ProposedToolCall, *, project_id: str,
                            binding: PrincipalBinding) -> tuple[Decision, ToolCall | None]:
    parsed = parse_proposed_call(proposed)
    if isinstance(parsed, Decision):
        return parsed, None
    decision = authorize_tool(scope, parsed, project_id=project_id, binding=binding)
    return decision, parsed if decision.allowed else None


R = TypeVar("R")


def enforce(decision: Decision, action: Callable[[], R]) -> R:
    """Run ``action`` only for an allowed decision; a denial performs no call."""
    if not decision.allowed:
        raise PolicyDenied(decision)
    return action()


def root_scope(project_id: str, principal: Principal) -> TrustedScope:
    """Supervisor scope for a session principal (M07 supplies the principal)."""
    return TrustedScope.model_validate({
        "project_id": project_id, "principal": principal.model_dump(), "role": "supervisor",
        "delegated_by": None, "allowed_tools": sorted(ROLE_TOOL_CEILINGS["supervisor"]),
        "policy_version": POLICY_VERSION, "issued_by": "controller",
    })


def delegate(parent: TrustedScope, child_role: AgentRole,
             tools: frozenset[ToolName] | set[ToolName]) -> TrustedScope:
    """Issue a narrower worker scope. Requests beyond the parent or the role ceiling fail."""
    if parent.policy_version != POLICY_VERSION:
        raise PolicyDenied(deny("unknown_policy", f"parent under {parent.policy_version!r}"))
    if parent.role != "supervisor" or parent.delegated_by is not None:
        raise PolicyDenied(deny("delegation_denied", "only an undelegated supervisor delegates"))
    if child_role == "supervisor":
        raise PolicyDenied(deny("delegation_denied", "supervisor authority is not delegable"))
    excess = set(tools) - (set(parent.allowed_tools) & ROLE_TOOL_CEILINGS[child_role])
    if excess:
        raise PolicyDenied(deny("delegation_denied",
                                f"{sorted(excess)} exceed parent scope or {child_role} ceiling"))
    return TrustedScope.model_validate({
        "project_id": parent.project_id, "principal": parent.principal.model_dump(),
        "role": child_role, "delegated_by": "supervisor", "allowed_tools": sorted(tools),
        "policy_version": POLICY_VERSION, "issued_by": "controller",
    })
