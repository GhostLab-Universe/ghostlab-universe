"""Experiment operations over operator-gated worlds (``ghostlab-api/1``).

A stateless plane: the caller injects the project-scoped store, the
operator registry (registrations and qualification), the reviewed-local
target registry and the deployment policy. Controllers own budgets and
pins; models and manifests never raise them.
"""

from ghostlab.experiments.configs import validate_policy
from ghostlab.experiments.ops import (
    compare_resolved, create_requested, resolve_config, run_repetition)
from ghostlab.experiments.store import ExperimentStore
from ghostlab.extensions.operator_registry import OperatorRegistry
from ghostlab.extensions.registry import AdapterRegistry

DEFAULT_POLICY_VERSION = "ghostlab-local-dev/1"


def default_experiment_policy(registry: AdapterRegistry) -> dict:
    """Local-dev policy: reviewed-local targets and faults are allowed.

    Ceilings stay modest; hosted deployments must narrow this via
    ``ApiConfig.exp_policy``. Describe-only discovery, no execution.
    """
    targets = {"script"}
    hooks = set()
    for world in registry.worlds():
        targets.update(registry.targets(world))
        env = registry.environment_factory(world)()
        try:
            hooks.update(fault.fault_id for fault in env.describe().faults)
        finally:
            env.close()
    return validate_policy({
        "policy_version": DEFAULT_POLICY_VERSION,
        "ceiling_tool_calls": 50, "ceiling_total_tool_calls": 500,
        "ceiling_repetitions": 5, "ceiling_wall_clock_ms": 120000,
        "allowed_targets": sorted(targets), "allowed_hooks": sorted(hooks)})


class ExperimentPlane:
    """Stateless experiment operations over injected dependencies."""

    def __init__(self, *, store: ExperimentStore, operators: OperatorRegistry,
                 targets: AdapterRegistry, policy: dict):
        self.store = store
        self.operators = operators
        self.targets = targets
        self.policy = validate_policy(policy)

    def create_config(self, *, project_id: str, config: dict,
                      requested_by: str) -> dict:
        return create_requested(
            self.store, project_id=project_id, body=config,
            requested_by=requested_by, policy=self.policy)

    def list_configs(self, project_id: str) -> list:
        return self.store.list_requested(project_id)

    def get_config(self, project_id: str, config_id: str) -> dict:
        return self.store.get_requested(project_id, config_id)

    def resolve(self, *, project_id: str, requested_id: str,
                manifest_digest=None, resolved_by: str) -> dict:
        return resolve_config(
            self.store, project_id=project_id, requested_id=requested_id,
            policy=self.policy, operators=self.operators,
            targets=self.targets, manifest_digest=manifest_digest,
            resolved_by=resolved_by)

    def list_resolved(self, project_id: str) -> list:
        return self.store.list_resolved(project_id)

    def get_resolved(self, project_id: str, resolved_id: str) -> dict:
        return self.store.get_resolved(project_id, resolved_id)

    def run(self, *, project_id: str, resolved_id: str, repetition: int,
            attempt: int, run_by: str) -> dict:
        return run_repetition(
            self.store, project_id=project_id, resolved_id=resolved_id,
            repetition=repetition, attempt=attempt, run_by=run_by,
            operators=self.operators, targets=self.targets)

    def list_runs(self, project_id: str, resolved_id=None) -> list:
        return self.store.list_runs(project_id, resolved_id)

    def get_run(self, project_id: str, run_id: str) -> dict:
        return self.store.get_run(project_id, run_id)

    def diff(self, *, project_id: str, left_id: str, right_id: str) -> dict:
        return compare_resolved(
            self.store, project_id=project_id, left_id=left_id,
            right_id=right_id)

    def get_policy(self) -> dict:
        return dict(self.policy)
