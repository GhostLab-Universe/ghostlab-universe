"""Run the trusted journey world under M06 authorization, in an explicit execution mode.

M02's world and adapters are used through their public classes and are not edited.

Every run, in every mode, passes each tool call through ``authorize_world_tool``
before dispatch: project, scope, role, tool validation and ownership. A denial
raises before the world's ``call``, so it leaves no trace event and no state
change, and the adapter can't carry on past it.

``ExecutionMode`` only decides whether the business rules under test are enforced.
In ``lab`` mode the guard is off so the verifier can find the failure. In
``enforced`` mode delivery is decided inside the tool call, in ``_precheck``:
after pending revocations apply and before delivery changes state. That is the
only place that can close the check-to-delivery race. A guard denial is a recorded
world outcome, so a capsule of the run replays exactly.
"""

from collections.abc import Callable
from dataclasses import replace

from ghostlab.contracts import TrustedScope
from ghostlab.execution.journey_guard import GuardedJourneyWorld
from ghostlab.policy import (Decision, ExecutionMode, PolicyDenied, WorldActorBinding,
                             authorize_delivery, authorize_world_tool)
from ghostlab.policy.authorization import deny
from ghostlab.worlds.journey import (WORLD_VERSION, BudgetExceeded, JourneyFixture,
                                     JourneyScenario, JourneyWorld)
from ghostlab.worlds.journey_run import ADAPTER_VERSION, ADAPTERS
from ghostlab.worlds.journey_verify import verify

from .engine import RunOutcome


class JourneyToolDenied(Exception):
    """Policy refused a call. Not a world ToolError, so adapters can't catch it."""

    def __init__(self, decision: Decision):
        super().__init__(f"{decision.code}: {decision.reason}")
        self.decision = decision


class JourneyRunStopped(Exception):
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
            raise JourneyRunStopped(reason)
        decision = authorize_world_tool(self.scope, tool, arguments, project_id=self.project_id,
                                        binding=self.binding, owner_id=self.fixture.organizer)
        if not decision.allowed:
            raise JourneyToolDenied(decision)
        return super().call(tool, arguments)


class LabJourneyWorld(_AuthorizedCalls, JourneyWorld):
    """Synthetic and in-process: only the business rules under test are off."""


class EnforcedJourneyWorld(_AuthorizedCalls, GuardedJourneyWorld):
    def _precheck(self, at, tool, arguments):
        decision = None
        if tool == "deliver_message":
            # Read here, not before the call: the revocation applied earlier in this call.
            recipient = self.state["messages"][arguments["message_id"]]["recipient_id"]
            decision = authorize_delivery(self.scope, project_id=self.project_id,
                                          sharing_permitted=self.state["sharing"][recipient])
        super()._precheck(at, tool, arguments)  # M02's guard records the denial in the trace
        if decision is not None and not decision.allowed:
            raise JourneyToolDenied(decision)  # the two layers disagree: fail closed


def execute_journey_run(fixture: JourneyFixture, scenario: JourneyScenario, adapter: str,
                        mode: ExecutionMode, *, scope: TrustedScope, project_id: str,
                        binding: WorldActorBinding,
                        should_stop: Callable[[], str | None] = lambda: None) -> RunOutcome:
    """One journey run. The fixture's ``guarded`` flag records the mode in any capsule."""
    if adapter not in ADAPTERS:
        raise ValueError("Unknown adapter")
    fixture = replace(fixture, guarded=mode.guarded)
    cls = EnforcedJourneyWorld if mode.guarded else LabJourneyWorld
    world = cls(fixture, scenario, scope=scope, project_id=project_id, binding=binding,
                should_stop=should_stop)
    status = "completed"
    try:
        ADAPTERS[adapter](world)
    except BudgetExceeded:
        status = "budget_exhausted"
    except JourneyToolDenied as denied:
        return RunOutcome(None, denied.decision, world.calls)
    except JourneyRunStopped as stopped:
        return RunOutcome(None, None, world.calls, stopped.reason)
    state = world.snapshot()
    return RunOutcome({"schema_version": 1, "world_version": WORLD_VERSION, "adapter_id": adapter,
                       "adapter_version": ADAPTER_VERSION, "actor_id": world.actor_id,
                       "status": status, "trace": world.trace, "final_state": state,
                       "checks": verify(fixture, state)}, None, world.calls)
