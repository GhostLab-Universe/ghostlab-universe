"""Reviewed local environment adapters.

An adapter is checked-in code referenced as ``"module:Class"``. Registration
resolves the reference (import plus attribute checks) without instantiating
it; only capability discovery and qualification instantiate adapters.
``describe()`` must be side-effect-free: discovery calls nothing else.
"""

import hashlib
import importlib
import inspect
import re
from copy import deepcopy

from ..core import Fixture, RefundWorld, Scenario

MODULE_PATTERN = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Every qualifying adapter implements these; registration checks without
# instantiating, so a missing method fails registration, not qualification.
REQUIRED_METHODS = (
    "describe",
    "reset",
    "invoke",
    "snapshot",
    "evidence",
    "set_fault",
    "clear_faults",
    "invoke_cancellable",
    "probe_calls",
)

REFUND_TOOLS = [
    {"name": "get_order", "parameters": ["order_id", "customer_id"]},
    {"name": "get_refund_status",
     "parameters": ["order_id", "customer_id", "idempotency_key"]},
    {"name": "issue_refund",
     "parameters": ["order_id", "customer_id", "amount_minor", "idempotency_key"]},
]

REFUND_MODES = ("recorded-actions", "deterministic-reset")
REFUND_FAULT_HOOKS = ("timeout_after_commit",)


class EnvError(Exception):
    """The environment rejected an operation (unknown tool, hook, bad state)."""


class EnvCancelled(EnvError):
    """A cancellable operation observed a set cancellation event."""


def split_adapter_ref(adapter_ref: str) -> tuple[str, str]:
    if type(adapter_ref) is not str or adapter_ref.count(":") != 1:
        raise ValueError("adapter_ref must look like 'module:Class'")
    module_name, class_name = adapter_ref.split(":")
    if not MODULE_PATTERN.match(module_name) or not CLASS_PATTERN.match(class_name):
        raise ValueError("adapter_ref must look like 'module:Class'")
    return module_name, class_name


def check_adapter_ref_allowed(adapter_ref: str, allowed_prefixes: tuple[str, ...]) -> None:
    module_name, _ = split_adapter_ref(adapter_ref)
    for prefix in allowed_prefixes:
        if module_name == prefix or module_name.startswith(
                prefix if prefix.endswith(".") else prefix + "."):
            return
    raise ValueError(f"adapter module '{module_name}' is not in the allowlist")


def resolve_adapter_ref(adapter_ref: str, allowed_prefixes: tuple[str, ...]) -> type:
    """Import and return the adapter class without instantiating it."""
    check_adapter_ref_allowed(adapter_ref, allowed_prefixes)
    module_name, class_name = split_adapter_ref(adapter_ref)
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ValueError(f"adapter module '{module_name}' cannot be imported") from error
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type):
        raise ValueError(f"adapter '{adapter_ref}' is not a class")
    missing = [name for name in REQUIRED_METHODS if not callable(getattr(cls, name, None))]
    if missing:
        raise ValueError(f"adapter '{adapter_ref}' misses: {', '.join(missing)}")
    return cls


def adapter_code_digest(cls: type) -> str:
    """Bind a registration to the reviewed source file behind the class."""
    try:
        source_file = inspect.getsourcefile(cls)
    except (OSError, TypeError) as error:
        raise ValueError(f"adapter '{cls.__name__}' has no readable source") from error
    if not source_file:
        raise ValueError(f"adapter '{cls.__name__}' has no readable source")
    with open(source_file, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class RefundEnvAdapter:
    """The reviewed synthetic INR refund world as a qualifying environment.

    Thin wrapper over ``ghostlab.core``: identical ledger behavior, plus the
    reset/fault/cancellation/evidence surface the qualification runner needs.
    """

    env_id = "refund-env"
    version = "refund-env/1"

    def __init__(self) -> None:
        self._world: RefundWorld | None = None
        self._fault_armed = False

    def describe(self) -> dict:
        return {
            "env_id": self.env_id,
            "version": self.version,
            "tools": deepcopy(REFUND_TOOLS),
            "reproduction_modes": list(REFUND_MODES),
            "fault_hooks": list(REFUND_FAULT_HOOKS),
            "supports_cancellation": True,
            "isolation": "in-process-checked-in-local",
        }

    def reset(self, fixture: dict) -> None:
        if type(fixture) is not dict:
            raise ValueError("fixture must be an object")
        parsed = Fixture(**fixture)
        scenario = Scenario(timeout_after_commit=self._fault_armed)
        self._world = RefundWorld(parsed, scenario)

    def _ready(self) -> RefundWorld:
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

    def clear_faults(self) -> None:
        self._fault_armed = False

    def probe_calls(self, fixture: dict) -> list:
        """Canonical valid sequence the generic runner may execute."""
        if type(fixture) is not dict:
            raise ValueError("fixture must be an object")
        identity = {"order_id": fixture["order_id"], "customer_id": fixture["customer_id"]}
        return [
            {"tool": "get_order", "arguments": dict(identity)},
            {"tool": "issue_refund", "arguments": {
                **identity, "amount_minor": fixture["paid_minor"],
                "idempotency_key": "qual-probe-1"}},
        ]
