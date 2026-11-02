"""Build Failure Capsule v3 envelopes from extension-run evidence.

Pydantic boundary over the stdlib replay core. Pack worlds only: the v3
contract names ``PackVersion``, so non-pack external worlds cannot be
enveloped until world versions become extensible (an M00 follow-up).
Nothing here invents a world version.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ghostlab import capsule as refund_capsule
from ghostlab.adapters.refund_env import SOURCE as REFUND_SOURCE
from ghostlab.contracts.capsule_v3 import CapsuleV3, capsule_digest, fixture_digest
from ghostlab.contracts.world_refund import run_to_world, scenario_to_world

REFUND_WORLD = "refund-world/1"
REFUND_ENVIRONMENT_ID = "refund-environment"
REFUND_ENVIRONMENT_VERSION = "refund-environment/1"
REFUND_FIXTURE_NAME = "synthetic-inr-2500"


def export_v3(*, world_version: str, fixture: Mapping, scenario: Mapping, actor_id: str,
              source: str, seed: int, recorded_model_outputs: Any, run: Mapping,
              provenance: Mapping | None) -> CapsuleV3:
    """Validate one v3 envelope over pack-world evidence and seal it.

    The contract owns every semantic check (same-world consistency, trace
    budget, fixture digest, fault schedule); this constructor only assembles
    the payload and seals the checksum.
    """
    if type(world_version) is not str or not world_version:
        raise ValueError("Invalid world version")
    if not isinstance(fixture, Mapping):
        raise ValueError("Invalid fixture")
    if not isinstance(scenario, Mapping):
        raise ValueError("Invalid scenario")
    if type(actor_id) is not str or not actor_id:
        raise ValueError("Invalid actor id")
    if type(source) is not str or not source:
        raise ValueError("Invalid source")
    if type(seed) is not int or not 0 <= seed <= 2**53 - 1:
        raise ValueError("Invalid seed")
    if not isinstance(recorded_model_outputs, (list, tuple)):
        raise ValueError("Invalid recorded model outputs")
    if not isinstance(run, Mapping):
        raise ValueError("Invalid run")
    if provenance is not None and not isinstance(provenance, Mapping):
        raise ValueError("Invalid provenance")
    payload = {"schema_version": 3, "world_version": world_version,
               "fixture": dict(fixture), "scenario": dict(scenario), "actor_id": actor_id,
               "source": source, "seed": seed,
               "recorded_model_outputs": list(recorded_model_outputs),
               "run": dict(run),
               "provenance": None if provenance is None else dict(provenance)}
    return CapsuleV3.model_validate({"payload": payload, "sha256": capsule_digest(payload)})


def export_refund_run_v3(*, fixture: Mapping, timeout_after_commit: bool,
                         max_tool_calls: int, actor_id: str, adapter: str,
                         adapter_version: str, status: str, trace: list,
                         final_state: Mapping, checks: Mapping, faults_fired: Any,
                         rule_versions: Any = None, config: Any = None,
                         external_dependencies: Any = (),
                         truncated: bool = False, replay_kind: str = "recorded",
                         retest_adapter_id: str | None = None,
                         retest_adapter_version: str | None = None,
                         recorded_model_outputs: Any = (),
                         seed: int = 0, source: str = REFUND_SOURCE) -> CapsuleV3:
    """Envelope one refund-world/1 extension run as a v3 capsule.

    ``faults_fired`` has no default: it reports faults that demonstrably
    took effect (the environment's ``faults_fired`` detection), never the
    bare schedule. ``rule_versions`` and ``config`` are world-supplied and
    default to empty; the contract checks every shape and cross-field rule.
    """
    if type(timeout_after_commit) is not bool:
        raise ValueError("timeout_after_commit must be boolean")
    if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 1000:
        raise ValueError("Invalid max tool calls")
    if not isinstance(fixture, Mapping):
        raise ValueError("Invalid fixture")
    if type(actor_id) is not str or not actor_id:
        raise ValueError("Invalid actor id")
    for value, label in ((adapter, "adapter"), (adapter_version, "adapter version"),
                         (status, "status"), (replay_kind, "replay kind")):
        if type(value) is not str or not value:
            raise ValueError(f"Invalid {label}")
    if type(trace) is not list or len(trace) > 1000:
        raise ValueError("Invalid trace")
    if not isinstance(final_state, Mapping):
        raise ValueError("Invalid final state")
    if (type(checks) is not dict
            or any(type(name) is not str or type(hit) is not bool
                   for name, hit in checks.items())):
        raise ValueError("Invalid checks")
    if (not isinstance(faults_fired, (list, tuple))
            or any(type(name) is not str for name in faults_fired)):
        raise ValueError("Invalid faults fired")
    scenario = scenario_to_world({"timeout_after_commit": timeout_after_commit,
                                  "max_tool_calls": max_tool_calls})
    world_run = run_to_world({"adapter": adapter, "adapter_version": adapter_version,
                              "actor_id": actor_id, "status": status, "trace": trace,
                              "final_state": dict(final_state), "checks": dict(checks)})
    fixture = dict(fixture)
    provenance = {
        "replay_kind": replay_kind, "environment_id": REFUND_ENVIRONMENT_ID,
        "environment_version": REFUND_ENVIRONMENT_VERSION,
        "fixture_name": REFUND_FIXTURE_NAME, "fixture_sha256": fixture_digest(fixture),
        "rule_versions": dict(rule_versions or {}), "faults_fired": list(faults_fired),
        "config": dict(config or {}),
        "external_dependencies": [dict(dep) for dep in external_dependencies],
        "truncated": truncated, "retest_adapter_id": retest_adapter_id,
        "retest_adapter_version": retest_adapter_version,
    }
    return export_v3(
        world_version=REFUND_WORLD, fixture=fixture,
        scenario=scenario.model_dump(mode="json"), actor_id=actor_id, source=source,
        seed=seed, recorded_model_outputs=recorded_model_outputs,
        run=world_run.model_dump(mode="json"), provenance=provenance)


def write_capsule_file(path: str | Path, capsule: Any,
                       max_bytes: int = refund_capsule.MAX_CAPSULE_BYTES) -> Path:
    """Write one checksum-verified v3 envelope to a size-capped file."""
    capsule = CapsuleV3.model_validate(capsule)
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Invalid capsule size cap")
    path = Path(path)
    text = json.dumps(capsule.model_dump(mode="json"), indent=2) + "\n"
    if len(text.encode()) > max_bytes:
        raise ValueError("Capsule too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
