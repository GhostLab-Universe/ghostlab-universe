"""Lossless mapping between refund v1 contracts and generic world contracts.

Refund v1 remains the authoritative format for ``refund-world/1``: the core and
``ghostlab.capsule`` keep emitting and replaying v1. These functions let generic
consumers (M05/M07/M08/M09) treat refund evidence like any other world, and
round-trip back to v1 exactly (tested), including the capsule checksum.
v1 <-> v3 composes through v2 (see ``ghostlab.contracts.capsule_v3``).
"""

import hashlib
import json

from .capsule_v3 import CapsuleChecksumError
from .evidence import CapsuleV1, ExperimentResult, InvariantFinding, RealityDiff, RunResult
from .evidence import ScenarioV1
from .provider import ExperimentProposal
from .world import (CapsuleV2, WorldExperimentProposal, WorldInvariantFinding,
                    WorldRealityDiff, WorldRunResult, WorldScenario)

REFUND = "refund-world/1"
TIMEOUT = "timeout_after_commit"


def capsule_digest(payload: dict) -> str:
    """The same canonical digest ``ghostlab.capsule`` uses for v1 capsules."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _dump(model) -> dict:
    return model.model_dump(mode="json")


def scenario_to_world(scenario: ScenarioV1 | dict) -> WorldScenario:
    scenario = ScenarioV1.model_validate(scenario)
    faults = ([{"fault_id": TIMEOUT, "after_event": None, "parameters": []}]
              if scenario.timeout_after_commit else [])
    return WorldScenario.model_validate({"schema_version": 1, "world_version": REFUND,
                                         "faults": faults,
                                         "max_tool_calls": scenario.max_tool_calls})


def scenario_from_world(scenario: WorldScenario | dict) -> ScenarioV1:
    scenario = WorldScenario.model_validate(scenario)
    if scenario.world_version != REFUND:
        raise ValueError("only refund-world/1 scenarios map to ScenarioV1")
    if any(f.after_event is not None for f in scenario.faults):
        raise ValueError("refund v1 has no custom fault timing")
    return ScenarioV1.model_validate({
        "timeout_after_commit": any(f.fault_id == TIMEOUT for f in scenario.faults),
        "max_tool_calls": scenario.max_tool_calls})


def proposal_to_world(proposal: ExperimentProposal | dict) -> WorldExperimentProposal:
    proposal = ExperimentProposal.model_validate(proposal)
    return WorldExperimentProposal.model_validate({
        "schema_version": 1, "world_version": REFUND, "hypothesis": proposal.hypothesis,
        "adapter_id": proposal.adapter, "scenario": _dump(scenario_to_world(proposal.scenario)),
        "expected_signal": proposal.expected_signal})


def proposal_from_world(proposal: WorldExperimentProposal | dict) -> ExperimentProposal:
    proposal = WorldExperimentProposal.model_validate(proposal)
    return ExperimentProposal.model_validate({
        "schema_version": 1, "hypothesis": proposal.hypothesis, "adapter": proposal.adapter_id,
        "scenario": _dump(scenario_from_world(proposal.scenario)),
        "expected_signal": proposal.expected_signal})


def run_to_world(run: RunResult | dict) -> WorldRunResult:
    data = _dump(RunResult.model_validate(run))
    return WorldRunResult.model_validate({
        "schema_version": 1, "world_version": REFUND, "adapter_id": data["adapter"],
        "adapter_version": data["adapter_version"], "actor_id": data["actor_id"],
        "status": data["status"], "trace": data["trace"], "final_state": data["final_state"],
        "checks": data["checks"]})


def run_from_world(run: WorldRunResult | dict) -> RunResult:
    data = _dump(WorldRunResult.model_validate(run))
    if data["world_version"] != REFUND:
        raise ValueError("only refund-world/1 runs map to RunResult")
    return RunResult.model_validate({
        "adapter": data["adapter_id"], "adapter_version": data["adapter_version"],
        "actor_id": data["actor_id"], "status": data["status"], "trace": data["trace"],
        "final_state": data["final_state"], "checks": data["checks"]})


def diff_to_world(diff: RealityDiff | dict) -> WorldRealityDiff:
    data = _dump(RealityDiff.model_validate(diff))
    return WorldRealityDiff.model_validate({
        "schema_version": 1, "world_version": REFUND,
        "first_divergence": data["first_divergence"],
        "baseline_metrics": {"refunded_minor": data["baseline_refunded_minor"]},
        "faulted_metrics": {"refunded_minor": data["faulted_refunded_minor"]},
        "violated_invariants": data["violated_invariants"]})


def finding_to_world(finding: InvariantFinding | dict) -> WorldInvariantFinding:
    data = _dump(InvariantFinding.model_validate(finding))
    valued = data["observed_minor"] is not None or data["limit_minor"] is not None
    return WorldInvariantFinding.model_validate({
        "schema_version": 1, "finding_id": data["finding_id"], "run_id": data["run_id"],
        "world_version": REFUND, "invariant": data["invariant"],
        "verifier_version": data["verifier_version"], "verdict": data["verdict"],
        "evidence_event_ids": data["evidence_event_ids"],
        "observed_value": data["observed_minor"], "limit_value": data["limit_minor"],
        "unit": "minor_units" if valued else None, "recorded_at": data["recorded_at"]})



def _verified_payload(capsule) -> dict:
    data = _dump(capsule)
    if capsule_digest(data["payload"]) != data["sha256"]:
        raise CapsuleChecksumError("Capsule checksum mismatch")
    return data["payload"]


def capsule_v1_to_v2(capsule: CapsuleV1 | dict) -> CapsuleV2:
    """Convert only a capsule whose checksum verifies (accidental-change detection)."""
    payload = _verified_payload(CapsuleV1.model_validate(capsule))
    v2 = {"schema_version": 2, "world_version": REFUND, "fixture": payload["fixture"],
          "scenario": _dump(scenario_to_world(payload["scenario"])),
          "actor_id": payload["actor_id"], "source": payload["source"], "seed": payload["seed"],
          "recorded_model_outputs": payload["recorded_model_outputs"],
          "run": _dump(run_to_world(payload["run"]))}
    return CapsuleV2.model_validate({"payload": v2, "sha256": capsule_digest(v2)})


def capsule_v2_to_v1(capsule: CapsuleV2 | dict) -> CapsuleV1:
    payload = _verified_payload(CapsuleV2.model_validate(capsule))
    if payload["world_version"] != REFUND:
        raise ValueError("only refund-world/1 capsules map to v1")
    v1 = {"schema_version": 1, "world_version": REFUND, "fixture": payload["fixture"],
          "scenario": _dump(scenario_from_world(payload["scenario"])),
          "actor_id": payload["actor_id"], "source": payload["source"], "seed": payload["seed"],
          "recorded_model_outputs": payload["recorded_model_outputs"],
          "run": _dump(run_from_world(payload["run"]))}
    return CapsuleV1.model_validate({"payload": v1, "sha256": capsule_digest(v1)})


def experiment_to_world(result: ExperimentResult | dict) -> dict:
    result = ExperimentResult.model_validate(result)
    return {"baseline": _dump(run_to_world(result.baseline)),
            "faulted": _dump(run_to_world(result.faulted)),
            "repaired": _dump(run_to_world(result.repaired)),
            "diff": _dump(diff_to_world(result.diff))}
