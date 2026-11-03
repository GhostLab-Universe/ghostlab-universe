"""Frozen extension interfaces (``ghostlab-extensions/1``): worlds, targets, registry, runner.

Standard library only. Later tracks implement against these interfaces; the
checked-in refund adapters in :mod:`ghostlab.adapters` prove them.
"""

from .environment import (EXTENSION_INTERFACE_VERSION, Action, ActionBudgetExceeded,
                          EnvironmentAdapter, EnvironmentDescription, FaultParameterSpec,
                          FaultSpec, InvariantCheck, Observation, Outcome, ToolSpec,
                          UnsupportedOperation)
from .registry import (CHECKED_IN, AdapterManifest, AdapterRegistry, RegistrationError,
                       UnknownTargetError, UnknownWorldError, check_environment, check_target)
from .runner import AttemptResult, first_divergence, replay_trace, run_attempt
from .target import (BUDGET_EXHAUSTED, DENIED, ERROR, FINISHED, TargetAdapter, TargetInfo,
                     TargetTask)

__all__ = [
    "CHECKED_IN", "BUDGET_EXHAUSTED", "DENIED", "ERROR",
    "EXTENSION_INTERFACE_VERSION", "FINISHED", "Action", "ActionBudgetExceeded",
    "AdapterManifest", "AdapterRegistry", "AttemptResult", "EnvironmentAdapter",
    "EnvironmentDescription", "FaultParameterSpec", "FaultSpec", "InvariantCheck",
    "Observation", "Outcome", "RegistrationError", "TargetAdapter", "TargetInfo",
    "TargetTask", "ToolSpec", "UnknownTargetError", "UnknownWorldError",
    "check_environment", "check_target", "first_divergence", "replay_trace", "run_attempt",
]