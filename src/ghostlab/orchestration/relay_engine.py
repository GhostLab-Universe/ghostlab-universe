"""Run the trusted relay world under M06 authorization, in an explicit execution mode.

M02's world and adapters are used through their public classes and are not edited.

Every run, in every mode, passes each tool call through ``authorize_world_tool``
before dispatch: project, scope, role, tool validation and ownership. A denial
raises before the world's ``call``, so it leaves no trace event and no state
change, and the adapter can't carry on past it.

``ExecutionMode`` only decides whether the business rules under test are enforced.
In ``lab`` mode the guard is off so the verifier can find the failure. In
``enforced`` mode M02's guard decides inside the tool call, in ``_precheck``: after
this event's takeover, competing lease or lease expiry applies, and before anything
changes state. That is the only place that can close a check-then-act race. A guard
denial is a recorded world outcome, so a capsule of the run replays exactly.
"""

from collections.abc import Callable

from ghostlab.contracts import TrustedScope
from ghostlab.execution.relay_guard import GuardedRelayWorld
from ghostlab.policy import (Decision, ExecutionMode, PolicyDenied, WorldActorBinding,
                             authorize_world_tool)
from ghostlab.policy.authorization import deny
from ghostlab.worlds.relay import (WORLD_VERSION, BudgetExceeded, RelayFixture, RelayScenario,
                                   RelayWorld)
from ghostlab.worlds.relay_run import ADAPTER_VERSION, ADAPTERS
from ghostlab.worlds.relay_verify import verify

from .engine import RunOutcome


class RelayToolDenied(Exception):
    """Policy refused a call. Not a world ToolError, so adapters can't catch it."""

    def __init__(self, decision: Decision):
        super().__init__(f"{decision.code}: {decision.reason}")
        self.decision = decision


class RelayRunStopped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _AuthorizedCalls:
    """Applies in every mode: a stop check, then M06, then the world's own host checks."""

    def __init__(self, fixture, scenario, *, scope: TrustedScope, project_id: str,
                 binding: WorldActorBinding, should_stop: Callable[[], str | None]):
        actor = binding.actor_for(project_id, scope.principal, WORLD_VERSION)
        if actor is None:
            raise PolicyDenied(deny("unbound_principal", "no world identity is bound"))
        super().__init__(fixture, scenario, actor_id=actor)
        self.scope, self.project_id, self.binding = scope, project_id, binding
        self.should_stop = should_stop

    def call(self, tool, arguments):
        if (reason := self.should_stop()) is not None:
            raise RelayRunStopped(reason)
        # M06's owner is the identity allowed to change the world: the shop's automation,
        # not the business owner who can take a conversation over.
        decision = authorize_world_tool(self.scope, tool, arguments, project_id=self.project_id,
                                        binding=self.binding, owner_id=self.fixture.operator)
        if not decision.allowed:
            raise RelayToolDenied(decision)
        return super().call(tool, arguments)


class LabRelayWorld(_AuthorizedCalls, RelayWorld):
    """Synthetic and in-process: only the business rules under test are off."""


class EnforcedRelayWorld(_AuthorizedCalls, GuardedRelayWorld):
    """M02's guard decides inside the call and records its denials in the trace."""


def execute_relay_run(fixture: RelayFixture, scenario: RelayScenario, adapter: str,
                      mode: ExecutionMode, *, scope: TrustedScope, project_id: str,
                      binding: WorldActorBinding,
                      should_stop: Callable[[], str | None] = lambda: None) -> RunOutcome:
    """One relay run. The mode picks the world class; a capsule records it from the
    experiment's execution record."""
    if adapter not in ADAPTERS:
        raise ValueError("Unknown adapter")
    cls = EnforcedRelayWorld if mode.guarded else LabRelayWorld
    world = cls(fixture, scenario, scope=scope, project_id=project_id, binding=binding,
                should_stop=should_stop)
    status = "completed"
    try:
        ADAPTERS[adapter](world)
    except BudgetExceeded:
        status = "budget_exhausted"
    except RelayToolDenied as denied:
        return RunOutcome(None, denied.decision, world.calls)
    except RelayRunStopped as stopped:
        return RunOutcome(None, None, world.calls, stopped.reason)
    state = world.snapshot()
    return RunOutcome({"schema_version": 1, "world_version": WORLD_VERSION, "adapter_id": adapter,
                       "adapter_version": ADAPTER_VERSION, "actor_id": world.actor_id,
                       "status": status, "trace": world.trace, "final_state": state,
                       "checks": verify(fixture, state)}, None, world.calls)
