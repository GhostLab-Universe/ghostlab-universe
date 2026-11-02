"""Qualify a registered target agent with a non-mutating probe."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class QualifyReport:
    agent_name: str
    agent_version: str
    passed: bool
    checks: dict[str, bool]
    details: dict[str, Any]


def qualify_agent(
    registry,
    agent_name: str,
    *,
    world_factory: Callable[[], Any],
    read_only_probe: tuple[str, dict[str, Any]],
    expected_tools: set[str] | None = None,
) -> QualifyReport:
    """Check manifest, allowlist and a read-only probe that must not mutate."""
    from .gateway import ControlledGateway

    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    record = registry.get(agent_name)
    checks["registered"] = record is not None
    if record is None:
        return QualifyReport(agent_name, "", False, checks, {"error": "unknown agent"})
    checks["version_present"] = bool(record.version)
    checks["config_is_object"] = isinstance(record.config, dict)
    checks["allowlist_nonempty"] = len(record.allowed_tools) > 0
    if expected_tools is not None:
        checks["allowlist_subset"] = set(record.allowed_tools) <= set(expected_tools)
        details["expected_tools"] = sorted(expected_tools)
    else:
        checks["allowlist_subset"] = True
    details["allowed_tools"] = list(record.allowed_tools)
    details["version"] = record.version

    gateway = ControlledGateway(registry)
    world = world_factory()
    before = world.snapshot()
    probe_tool, probe_args = read_only_probe
    try:
        if probe_tool not in record.allowed_tools:
            checks["probe_allowlisted"] = False
            details["probe_error"] = f"Probe tool not allowlisted: {probe_tool}"
            passed = all(checks.values())
            return QualifyReport(record.name, record.version, passed, checks, details)
        checks["probe_allowlisted"] = True
        run_id = gateway.create_run(agent_name, max_tool_calls=5)
        gateway.dispatch(run_id, world, probe_tool, copy.deepcopy(probe_args))
        checks["probe_succeeded"] = True
    except Exception as error:  # Probe failure is a qualification signal, not a crash.
        checks["probe_succeeded"] = False
        details["probe_error"] = f"{type(error).__name__}: {error}"
        passed = all(checks.values())
        return QualifyReport(record.name, record.version, passed, checks, details)
    after = world.snapshot()
    checks["probe_non_mutating"] = _non_mutating(before, after)
    if not checks["probe_non_mutating"]:
        details["probe_error"] = "Probe mutated world state"
    details["probe_records"] = len(gateway.records_for_run(run_id))
    passed = all(checks.values())
    return QualifyReport(record.name, record.version, passed, copy.deepcopy(checks), copy.deepcopy(details))


def _non_mutating(before: dict[str, Any], after: dict[str, Any]) -> bool:
    for key in ("ledger", "reservations", "total_refunded_minor", "total_bookings"):
        if before.get(key) != after.get(key):
            return False
    return True
