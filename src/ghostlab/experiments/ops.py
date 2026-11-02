"""Project-scoped API operations for experiment configs and runs.

These are the stable operation shapes a service layer (FastAPI routes) or a
sibling track calls. Every operation takes the caller's ``project_id`` first —
it stands for the session-derived project, never client-selected scope — and
all inputs/outputs are plain JSON-serializable dicts. No web framework, no
model calls.

Shapes (``...`` marks the full validated body, see ``configs``):

- create_requested_config(store, project_id, obj, policy_obj, requested_by)
  -> {"config_id", "project_id", "name", "config": {...}, "digest",
      "requested_by", "created_at"}. Idempotent per project: recreating
  identical settings returns the existing record.
- get_requested_config(store, project_id, config_id) -> same record.
- list_requested_configs(store, project_id) -> {"project_id", "configs": [...]}.
- resolve_config(store, registry, project_id, requested_id, policy_obj,
  requested_by, resolved_by="controller", manifest_digest=None)
  -> {"resolved_id", "project_id", "requested_id", "resolved": {...},
      "digest", "created_at"}. Describe-only env access: no inference, no
  target-env mutation. Idempotent per project.
- get_resolved_config / list_resolved_configs -> same record shape.
- start_run(store, registry, project_id, resolved_id, repetition=0,
  attempt=0, cancel=None)
  -> {"run_id", "project_id", "resolved_id", "repetition", "attempt",
      "run": {...}, "digest", "created_at"}. Executes the environment.
- get_run / list_runs(store, project_id, resolved_id=None) -> same.
- compare_resolved(store, project_id, left_id, right_id)
  -> {"left_id", "right_id", "left_digest", "right_digest", "equal",
      "diff_count", "truncated", "diffs": [{"path", "left", "right"}]}.
- budget_used(store, project_id, resolved_id, resource="tool_calls")
  -> {"project_id", "resolved_id", "resource", "used"}.

Errors: ``ConfigError`` (a ``ValueError``) for invalid/unsupported/impossible
configs and unknown ids; ``CrossProjectError`` (a ``PermissionError``) when a
record belongs to a different project; ``BudgetExhaustedError`` when the
aggregate envelope is spent (the environment was not touched). Validation and
resolution fail before any mutation or execution.
"""

from ..envs.registry import utcnow
from .configs import (ConfigError, Policy, RequestedConfig, resolve_requested,
                      validate_policy)
from .controller import BudgetExhaustedError, execute_run
from .diffs import diff_resolved
from .store import CrossProjectError

__all__ = [
    "BudgetExhaustedError",
    "ConfigError",
    "CrossProjectError",
    "budget_used",
    "compare_resolved",
    "create_requested_config",
    "get_requested_config",
    "get_resolved_config",
    "get_run",
    "list_requested_configs",
    "list_resolved_configs",
    "list_runs",
    "resolve_config",
    "start_run",
]


def create_requested_config(store, project_id: str, obj, policy_obj,
                            requested_by: str) -> dict:
    """Validate reusable settings and persist them for one project."""
    requested = RequestedConfig.from_obj(obj, policy_obj)
    body = requested.to_dict()
    return store.save_requested(project_id=project_id, body=body,
                                digest=requested.digest(),
                                requested_by=requested_by,
                                created_at=utcnow())


def get_requested_config(store, project_id: str, config_id: str) -> dict:
    return store.get_requested(project_id, config_id)


def list_requested_configs(store, project_id: str) -> dict:
    return {"project_id": project_id,
            "configs": store.list_requested(project_id)}


def resolve_config(store, registry, project_id: str, requested_id: str,
                   policy_obj, requested_by: str,
                   resolved_by: str = "controller",
                   manifest_digest: str | None = None) -> dict:
    """Bind stored settings to pinned versions (no inference, no mutation)."""
    stored = store.get_requested(project_id, requested_id)
    policy = Policy.from_obj(validate_policy(policy_obj))
    requested = RequestedConfig.from_obj(stored["config"],
                                         policy.to_dict())
    if requested.digest() != stored["digest"]:
        raise ConfigError("requested config digest mismatch")
    resolved = resolve_requested(
        requested, project_id=project_id, requested_id=requested_id,
        requested_digest=stored["digest"], requested_by=requested_by,
        policy=policy, registry=registry, manifest_digest=manifest_digest,
        resolved_by=resolved_by)
    resolved.verify()
    body = resolved.body()
    return store.save_resolved(project_id=project_id,
                               requested_id=requested_id, body=body,
                               digest=body["digest"],
                               created_at=body["resolved_at"])


def get_resolved_config(store, project_id: str, resolved_id: str) -> dict:
    return store.get_resolved(project_id, resolved_id)


def list_resolved_configs(store, project_id: str) -> dict:
    return {"project_id": project_id,
            "configs": store.list_resolved(project_id)}


def start_run(store, registry, project_id: str, resolved_id: str,
              repetition: int = 0, attempt: int = 0,
              cancel=None) -> dict:
    """Execute one repetition attempt through the shared env interface."""
    return execute_run(store, registry, project_id=project_id,
                       resolved_id=resolved_id, repetition=repetition,
                       attempt=attempt, cancel=cancel)


def get_run(store, project_id: str, run_id: str) -> dict:
    return store.get_run(project_id, run_id)


def list_runs(store, project_id: str,
              resolved_id: str | None = None) -> dict:
    return {"project_id": project_id,
            "runs": store.list_runs(project_id, resolved_id)}


def compare_resolved(store, project_id: str, left_id: str,
                     right_id: str) -> dict:
    """Return comparison metadata (config diffs) for two resolved configs."""
    left = store.get_resolved(project_id, left_id)
    right = store.get_resolved(project_id, right_id)
    return diff_resolved(left["resolved"], right["resolved"])


def budget_used(store, project_id: str, resolved_id: str,
                resource: str = "tool_calls") -> dict:
    store.get_resolved(project_id, resolved_id)
    return {"project_id": project_id, "resolved_id": resolved_id,
            "resource": resource,
            "used": store.budget_used(project_id, resolved_id, resource)}
