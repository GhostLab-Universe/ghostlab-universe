"""Track-A environment adapter over the external equipment-booking world.

This is the integration seam between the environment pipeline (registration,
manifest import, discovery, qualification) and the external example: it lets
the booking world qualify and execute through the shared adapter interface
with zero edits to ``ghostlab`` core code.

The operator registers ``adapter_ref =
"examples.equipment_booking.env_adapter:BookingEnvAdapter"`` with an explicit
allowlist entry; registration binds the reviewed source file's digest, and
qualification executes only this checked-in code on disposable fixtures.
"""

from copy import deepcopy

from ghostlab.envs.adapters import EnvCancelled, EnvError

from .world import EquipmentFixture, EquipmentScenario, EquipmentWorld

ENV_ID = "booking-env"
ENV_VERSION = "booking-env/1"

BOOKING_TOOLS = [
    {"name": "get_equipment", "parameters": ["equipment_id", "user_id"]},
    {"name": "get_booking_status",
     "parameters": ["equipment_id", "user_id", "idempotency_key"]},
    {"name": "book_equipment",
     "parameters": ["equipment_id", "user_id", "slot", "idempotency_key"]},
]

BOOKING_MODES = ["recorded-actions", "deterministic-reset"]
BOOKING_FAULT_HOOKS = ["timeout_after_commit"]

_FIXTURE_KEYS = {"equipment_id", "user_id", "slot"}


class BookingEnvAdapter:
    """The external booking world behind the shared adapter protocol."""

    env_id = ENV_ID
    version = ENV_VERSION

    def __init__(self) -> None:
        self._world: EquipmentWorld | None = None
        self._fault_armed = False

    def describe(self) -> dict:
        return {
            "env_id": self.env_id,
            "version": self.version,
            "tools": deepcopy(BOOKING_TOOLS),
            "reproduction_modes": list(BOOKING_MODES),
            "fault_hooks": list(BOOKING_FAULT_HOOKS),
            "supports_cancellation": True,
            "isolation": "in-process-checked-in-local",
        }

    def reset(self, fixture: dict) -> None:
        if type(fixture) is not dict or set(fixture) != _FIXTURE_KEYS:
            raise ValueError("fixture must carry exactly the booking fields")
        parsed = EquipmentFixture(**fixture)
        scenario = EquipmentScenario(timeout_after_commit=self._fault_armed)
        self._world = EquipmentWorld(parsed, scenario)

    def _ready(self) -> EquipmentWorld:
        if self._world is None:
            raise EnvError("environment has not been reset")
        return self._world

    def invoke(self, tool: str, arguments: dict) -> dict:
        return self._ready().call(tool, arguments)

    def invoke_cancellable(self, tool: str, arguments: dict, cancel=None) -> dict:
        if cancel is not None and cancel.is_set():
            raise EnvCancelled("cancelled before dispatch")
        return self.invoke(tool, arguments)

    def snapshot(self) -> dict:
        return self._ready().snapshot()

    def evidence(self) -> dict:
        world = self._ready()
        return {"snapshot": world.snapshot(), "trace": deepcopy(world.trace)}

    def set_fault(self, name: str, params: dict) -> None:
        if name != "timeout_after_commit":
            raise EnvError(f"unknown fault hook '{name}'")
        if type(params) is not dict or params != {}:
            raise ValueError("timeout_after_commit takes no parameters")
        self._fault_armed = True
        if self._world is not None:
            # Late arming (mirrors RefundEnvAdapter): a reset world reads
            # its scenario per dispatch. Single-shot per reset.
            self._world.scenario = EquipmentScenario(
                timeout_after_commit=True,
                max_tool_calls=self._world.scenario.max_tool_calls)

    def clear_faults(self) -> None:
        self._fault_armed = False
        if self._world is not None:
            self._world.scenario = EquipmentScenario(
                timeout_after_commit=False,
                max_tool_calls=self._world.scenario.max_tool_calls)

    def probe_calls(self, fixture: dict) -> list:
        """Canonical valid sequence the generic runner may execute."""
        if type(fixture) is not dict or not _FIXTURE_KEYS.issubset(fixture):
            raise ValueError("fixture must carry exactly the booking fields")
        identity = {"equipment_id": fixture["equipment_id"],
                    "user_id": fixture["user_id"]}
        return [
            {"tool": "get_equipment", "arguments": dict(identity)},
            {"tool": "book_equipment", "arguments": {
                **identity, "slot": fixture["slot"],
                "idempotency_key": "qual-probe-1"}},
        ]
