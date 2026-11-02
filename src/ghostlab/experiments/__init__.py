"""Reusable experiment configs, immutable resolved configs and controller execution (Track C).

Standard library only, like ``ghostlab.core`` and ``ghostlab.envs``: this
package never imports provider SDKs, Pydantic service types or UI code, and it
makes no inference calls. Execution goes through the shared environment
interface (``ghostlab.envs`` registry/adapters) and the shared refund agent
procedures, so refund behavior stays identical to ``ghostlab.core``.

Modules:

- ``configs``: strict requested-config validation and immutable resolved
  configs with pinned versions, effective values and provenance.
- ``store``: project-scoped SQLite persistence (serial local coordinator).
- ``controller``: execution plumbing with surviving budgets, recorded
  never-fired required faults and evidence-gated verdicts.
- ``diffs``: comparison metadata (config diffs) between resolved configs.
- ``ops``: project-scoped API operations with stable dict shapes.
"""

from .configs import (CONTROLLER_VERSION, EXPERIMENTS_SCHEMA_VERSION,
                      ConfigError, Effective, Pins, Policy, Provenance,
                      RequestedConfig, resolve_requested, validate_policy,
                      validate_requested)
from .controller import BudgetExhaustedError, evaluate_verdict, execute_run
from .diffs import diff_resolved
from .store import CrossProjectError, ExperimentStore

__all__ = [
    "CONTROLLER_VERSION",
    "EXPERIMENTS_SCHEMA_VERSION",
    "BudgetExhaustedError",
    "ConfigError",
    "CrossProjectError",
    "Effective",
    "ExperimentStore",
    "Pins",
    "Policy",
    "Provenance",
    "RequestedConfig",
    "diff_resolved",
    "evaluate_verdict",
    "execute_run",
    "resolve_requested",
    "validate_policy",
    "validate_requested",
]
