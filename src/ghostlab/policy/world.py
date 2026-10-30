"""World-aware authorization (``policy-1``, additive to the refund tool policy).

Tools, arguments and effects come from the reviewed world pack, so a tool is
authorized against its manifest, not against a refund-only type. Authority still
comes only from a controller-issued scope and the controller's binding. A tool
argument never sets identity: where a pack's tool names one, it must equal the
bound identity. No proposal, memory record, capsule or request body reaches any
function here as authority.

Two things stay separate on purpose:

- **Boundaries** apply to every run of every world: project, scope, role, tool
  validation, ownership and the runnable-world gate.
- **Business rules under test** may be relaxed only for an experiment in a
  synthetic world, so the verifier can find the failure. ``execution_mode_for``
  is the only source of that choice. Anything but an experiment is enforced.

``authorize_delivery`` decides from a fact the caller reads inside the tool call,
after pending revocations apply and before delivery changes state. A check made
before the call could not close the check-to-delivery race.
"""

from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from ghostlab.contracts import Principal, TrustedScope
from ghostlab.contracts.primitives import AgentRole
from ghostlab.contracts.world import WorldTrustedScope
from ghostlab.contracts.world_packs import check_arguments, is_executable, pack

from .authorization import POLICY_VERSION, Decision, PolicyDenied, _scope_check, allow, deny

ALL_EFFECTS = frozenset({"read", "mutate", "deliver", "handoff"})
IDENTITY_ARGUMENTS = ("customer_id",)  # pack arguments that name the acting identity

# Most a role may do in a world, by tool effect, however a scope was issued.
ROLE_EFFECT_CEILINGS: Mapping[str, frozenset[str]] = MappingProxyType({
    "supervisor": ALL_EFFECTS,
    "target": ALL_EFFECTS,  # the system under test acts on the synthetic world
    "analyst": frozenset({"read"}),
    "explorer": frozenset(),
    "worldsmith": frozenset(),
    "verifier": frozenset(),
})

ExecutionPurpose = Literal["experiment", "repair", "operation"]


@dataclass(frozen=True)
class ExecutionMode:
    """How one run is enforced. The controller records it with the run's evidence."""

    mode: Literal["lab", "enforced"]
    guarded: bool

    def as_json(self) -> dict:
        return asdict(self)


LAB = ExecutionMode("lab", guarded=False)
ENFORCED = ExecutionMode("enforced", guarded=True)


def execution_mode_for(purpose: ExecutionPurpose) -> ExecutionMode:
    """Only a controller-labelled experiment runs unguarded; everything else is enforced."""
    return LAB if purpose == "experiment" else ENFORCED


class WorldActorBinding(Protocol):
    """Controller-owned map from a session principal to its identity in one world."""

    def actor_for(self, project_id: str, principal: Principal,
                  world_version: str) -> str | None: ...


class StaticWorldBinding:
    def __init__(self, bindings: Mapping[tuple[str, str, str], str]):
        self._bindings = MappingProxyType(dict(bindings))

    def actor_for(self, project_id: str, principal: Principal,
                  world_version: str) -> str | None:
        return self._bindings.get((project_id, principal.principal_id, world_version))


def authorize_world(world_version: str) -> Decision:
    """The registry gate. A registered design that isn't implemented does not run."""
    if not is_executable(world_version):
        return deny("world_not_runnable", f"{world_version!r} is not a runnable world")
    return allow()


def world_root_scope(project_id: str, principal: Principal,
                     world_version: str) -> WorldTrustedScope:
    """Supervisor scope over one runnable world's tools (M07 supplies the principal)."""
    if not (gate := authorize_world(world_version)).allowed:
        raise PolicyDenied(gate)
    return WorldTrustedScope.model_validate({
        "project_id": project_id, "principal": principal.model_dump(), "role": "supervisor",
        "delegated_by": None, "world_version": world_version,
        "allowed_tools": sorted(tool.name for tool in pack(world_version).tools),
        "policy_version": POLICY_VERSION, "issued_by": "controller"})


def supervisor_scope(project_id: str, principal: Principal) -> TrustedScope:
    """World-neutral supervisor scope for approvals, memory and explorer delegation.

    It holds no tools. An approval is bound to its action by digest, and the action
    names the world, so a world's tools never need to appear on a refund-typed scope.
    """
    return TrustedScope.model_validate({
        "project_id": project_id, "principal": principal.model_dump(), "role": "supervisor",
        "delegated_by": None, "allowed_tools": [], "policy_version": POLICY_VERSION,
        "issued_by": "controller"})


def delegate_world(parent: WorldTrustedScope, child_role: AgentRole,
                   tools: Collection[str]) -> WorldTrustedScope:
    """Issue a narrower worker scope in the parent's world."""
    if not isinstance(parent, WorldTrustedScope):
        raise PolicyDenied(deny("delegation_denied", "parent is not issued for a world"))
    if parent.policy_version != POLICY_VERSION:
        raise PolicyDenied(deny("unknown_policy", f"parent under {parent.policy_version!r}"))
    if parent.role != "supervisor" or parent.delegated_by is not None:
        raise PolicyDenied(deny("delegation_denied", "only an undelegated supervisor delegates"))
    if child_role == "supervisor":
        raise PolicyDenied(deny("delegation_denied", "supervisor authority is not delegable"))
    if not (gate := authorize_world(parent.world_version)).allowed:
        raise PolicyDenied(gate)
    world = pack(parent.world_version)
    effects = ROLE_EFFECT_CEILINGS.get(child_role, frozenset())
    permitted = {tool.name for tool in world.tools
                 if tool.effect in effects and tool.name in parent.allowed_tools}
    tools = set(tools)
    excess = tools - permitted
    if excess:
        raise PolicyDenied(deny("delegation_denied",
                                f"{sorted(excess)} exceed parent scope or {child_role} ceiling"))
    return WorldTrustedScope.model_validate({
        "project_id": parent.project_id, "principal": parent.principal.model_dump(),
        "role": child_role, "delegated_by": "supervisor", "world_version": parent.world_version,
        "allowed_tools": sorted(tools), "policy_version": POLICY_VERSION,
        "issued_by": "controller"})


def authorize_world_tool(scope: TrustedScope, tool: object, arguments: object, *,
                         project_id: str, binding: WorldActorBinding,
                         owner_id: str) -> Decision:
    """Decide one world tool call before dispatch. Applies in every execution mode.

    ``owner_id`` is the trusted owner from the controller's fixture: the only
    identity that may change the world. Reads need a bound identity; effects also
    need that identity to be the owner. An identity argument must match the binding.
    """
    if (failure := _scope_check(scope, project_id)) is not None:
        return failure
    if not isinstance(scope, WorldTrustedScope):
        return deny("tool_not_in_scope", "scope is not issued for a world")
    if not (gate := authorize_world(scope.world_version)).allowed:
        return gate
    spec = pack(scope.world_version).tool(tool) if type(tool) is str else None
    if spec is None:
        return deny("unknown_tool", f"not a tool of {scope.world_version}")
    if not isinstance(arguments, Mapping) or check_arguments(spec.parameters, arguments):
        return deny("invalid_arguments", "arguments do not match the pack's tool schema")
    if tool not in scope.allowed_tools:
        return deny("tool_not_in_scope", f"{tool!r} not granted to this scope")
    if spec.effect not in ROLE_EFFECT_CEILINGS.get(scope.role, frozenset()):
        return deny("tool_not_in_role_ceiling", f"role {scope.role!r} may not call {tool!r}")
    bound = binding.actor_for(project_id, scope.principal, scope.world_version)
    if bound is None:
        return deny("unbound_principal", "no world identity is bound to this principal")
    if any(arguments.get(name, bound) != bound for name in IDENTITY_ARGUMENTS):
        return deny("identity_mismatch", "an identity argument differs from the bound principal")
    if spec.effect != "read" and bound != owner_id:
        return deny("identity_mismatch", "only the bound owner may change this world")
    return allow()


def authorize_delivery(scope: TrustedScope, *, project_id: str,
                       sharing_permitted: bool) -> Decision:
    """Decide an irreversible delivery from the recipient's permission at this event.

    The scope must itself be able to deliver in a runnable world, so this can't be
    satisfied by a scope that merely belongs to the right project.
    """
    if (failure := _scope_check(scope, project_id)) is not None:
        return failure
    if not isinstance(scope, WorldTrustedScope):
        return deny("tool_not_in_scope", "scope is not issued for a world")
    if not (gate := authorize_world(scope.world_version)).allowed:
        return gate
    if "deliver" not in ROLE_EFFECT_CEILINGS.get(scope.role, frozenset()):
        return deny("tool_not_in_role_ceiling", f"role {scope.role!r} may not deliver")
    tools = {tool.name for tool in pack(scope.world_version).tools if tool.effect == "deliver"}
    if not tools & set(scope.allowed_tools):
        return deny("tool_not_in_scope", "no delivery tool is granted to this scope")
    if sharing_permitted is not True:
        return deny("permission_withdrawn", "recipient's sharing permission was withdrawn")
    return allow()
