"""Record real core outputs for the M08 test fixtures. Makes no model or network call.

The repair assessment only exists on M05's branch until it merges, so this runs
against an export of that branch:

    git archive <m05 commit> src pyproject.toml uv.lock README.md skills prompts \
        | tar -x -C <scratch>/m05src
    uv run --project <scratch>/m05src --frozen --all-extras python \
        web/tests/fixtures/record_core_outputs.py --examples examples/contracts/core \
        --out web/src/fixtures/recorded.generated.ts --source-commit <m05 commit>

Worlds, adapters, verifier, capsule replay and M05's world drivers are the checked-in
code. The output is shaped as M07's RepairView and ReplayResult, because M07's routes
don't return real data yet. Only that assembly happens here, following
Controller._execute_repair and Controller._repair_action at the recorded commit.
"""

import argparse
import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from ghostlab import capsule as refund_capsule
from ghostlab.contracts.world import WorldRunResult
from ghostlab.contracts.world_refund import finding_to_world, run_to_world
from ghostlab.core import Fixture as RefundFixture
from ghostlab.orchestration.engine import derive_findings
from ghostlab.orchestration.worlds import DEFAULT_WORLDS, canonical
from ghostlab.policy import action_digest, execution_mode_for
from ghostlab.worlds import journey_capsule
from ghostlab.worlds.journey import (JourneyFixture, JourneyOption, JourneyScenario,
                                     scenario_json)
from ghostlab.worlds.journey_run import run

JOURNEY = "group-journey/1"
REFUND = "refund-world/1"
NAIVE, CHECKED = "journey-naive", "journey-version-checked"
RECORDED_AT = "2026-10-30T00:00:00Z"
ENFORCED = execution_mode_for("repair").as_json()


def dumped(model):
    return model.model_dump(mode="json")


def revocation(after_event=None, recipient="participant-c"):
    return {"fault_id": "sharing_revoked_while_queued", "after_event": after_event,
            "parameters": [{"name": "recipient_id", "value": recipient}]}


def faulted_scenario(cap, after_event=None, *, faults=None):
    data = json.loads(json.dumps(scenario_json(max_tool_calls=cap)))
    data["faults"] = [revocation(after_event)] if faults is None else faults
    return data


def verdict(assessment):
    """The same rule as Controller._repair_verdict at the recorded commit."""
    if assessment["run_status"] != "completed":
        return "budget_exhausted", "repair_incomplete"
    if assessment["verified"]:
        return "completed", "repair_verified"
    return "failed", "repair_did_not_hold" if not assessment["safety_held"] else "repair_incomplete"


def repair_view(run_id, assessment, result):
    return {"authorized": True, "verified": assessment["verified"],
            "run": {"kind": "run", "id": run_id, "sha256": None},
            "run_status": assessment["run_status"], "safety_held": assessment["safety_held"],
            "task_complete": assessment["task_complete"],
            "task_outcome": assessment.get("task_outcome"),
            "adapter": result.get("adapter"), "execution": result.get("execution"),
            "guard_denials": result.get("guard_denials"), "held_by": result.get("held_by"),
            "lost_faults": assessment["lost_faults"],
            "refunded_minor": result.get("refunded_minor")}


def journey_repair(run_id, scenario, fixture=None, fork_adapter=NAIVE):
    driver = DEFAULT_WORLDS[JOURNEY]
    fixture = fixture or JourneyFixture()
    fork = run(fixture, JourneyScenario.from_json(scenario), fork_adapter)
    recorded = run(replace(fixture, guarded=True), JourneyScenario.from_json(scenario), CHECKED)
    WorldRunResult.model_validate(recorded)
    found = driver.findings(fixture, run_id, recorded, RECORDED_AT, ENFORCED)
    assessment = driver.assess_repair(fixture, recorded, found, ENFORCED)
    proposal = {"scenario": scenario}  # as Controller._execute_repair compares them
    lost = sorted(driver.faults_fired(proposal, fork) - driver.faults_fired(proposal, recorded))
    assessment = {**assessment, "lost_faults": lost, "verified": assessment["verified"] and not lost}
    action = {"adapter": CHECKED, "execution": ENFORCED}
    result = driver.repair_result(recorded, action, assessment["verified"])
    status, outcome = verdict(assessment)
    return {"campaign_status": status, "outcome": outcome, "run": recorded, "findings": found,
            "repair": repair_view(run_id, assessment, result)}


def refund_repair(run_id, recorded, fork):
    driver = DEFAULT_WORLDS[REFUND]
    found = derive_findings(run_id, recorded, recorded_at=RECORDED_AT)
    assessment = driver.assess_repair(None, recorded, found, None)
    lost = sorted(driver.faults_fired({}, fork) - driver.faults_fired({}, recorded))
    assessment = {**assessment, "lost_faults": lost, "verified": assessment["verified"] and not lost}
    result = driver.repair_result(recorded, {}, assessment["verified"])
    status, outcome = verdict(assessment)
    return {"campaign_status": status, "outcome": outcome, "run": dumped(run_to_world(recorded)),
            "findings": [dumped(finding_to_world(finding)) for finding in found],
            "repair": repair_view(run_id, assessment, result)}


def truncated_experiment(scenario, adapter=NAIVE):
    """A baseline and fork that hit their tool cap, as Controller._execute records them."""
    driver = DEFAULT_WORLDS[JOURNEY]
    fixture = JourneyFixture()
    cap = scenario["max_tool_calls"]
    baseline = run(fixture, JourneyScenario.from_json(scenario_json(max_tool_calls=cap)), adapter)
    fork = run(fixture, JourneyScenario.from_json(scenario), adapter)
    lab = execution_mode_for("experiment").as_json()
    statuses = {baseline["status"], fork["status"]}
    triggered = driver.faults_triggered({"scenario": scenario}, fork)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "capsule.json"
        journey_capsule.export_capsule(path, fixture, scenario, fork)
        capsule = json.loads(path.read_text("utf-8"))
        replayed = journey_replay(path)
    return {"baseline": baseline, "fork": fork, "scenario": scenario,
            "capsule": capsule, "replay": replayed,
            "diff": driver.diff(fixture, baseline, fork),
            "findings": driver.findings(fixture, "journey-run-1", fork, RECORDED_AT, lab),
            "coverage": {"baseline": baseline["status"], "fork": fork["status"],
                         "faults_triggered": triggered,
                         "complete": triggered and statuses == {"completed"}}}


def bound_action(driver, fixture, proposal):
    """The action an approval binds, built as Controller._repair_action does."""
    action = driver.repair_action("experiment-1", proposal)
    action["fixture_sha256"] = hashlib.sha256(canonical(driver.snapshot(fixture))).hexdigest()
    return {"action": action, "action_digest": action_digest(action)}


def refund_replay(path):
    result = refund_capsule.replay(path)
    status = json.loads(path.read_text("utf-8"))["payload"]["run"]["status"]
    return {"world_version": REFUND, "inference": "fixture", "run_status": status,
            "reproduced": result["reproduced"],
            "matches": result["matches"], "checks": result["checks"],
            "metrics": {"refunded_minor": result["total_refunded_minor"]},
            "guarded": None, "model_calls": result["model_calls"]}


def journey_replay(path):
    result = journey_capsule.replay(path)
    status = json.loads(path.read_text("utf-8"))["payload"]["run"]["status"]
    return {"world_version": JOURNEY, "inference": "fixture", "run_status": status,
            "reproduced": result["reproduced"],
            "matches": result["matches"], "checks": result["checks"],
            "metrics": result["metrics"], "guarded": result["guarded"],
            "model_calls": result["model_calls"]}


def record(examples: Path, source_commit: str) -> dict:
    fixture = JourneyFixture()
    refund, journey = DEFAULT_WORLDS[REFUND], DEFAULT_WORLDS[JOURNEY]
    experiment = json.loads((examples / "experiment_result.refund_demo.json").read_text("utf-8"))
    published_fork = json.loads(
        (examples / "world_run_result.journey_fixture.json").read_text("utf-8"))
    refund_proposal = {"adapter": "vulnerable",
                       "scenario": {"max_tool_calls": 20, "timeout_after_commit": True}}
    journey_proposal = {"adapter_id": NAIVE, "scenario": faulted_scenario(20)}
    lab = execution_mode_for("experiment").as_json()
    truncated_violation = faulted_scenario(
        11, faults=[revocation(after_event=2, recipient=fixture.organizer)])
    missed_fault = [{"fault_id": "delayed_itinerary_update", "after_event": None,
                     "parameters": [{"name": "delay_events", "value": 3}]},
                    revocation(after_event=11, recipient="traveller-a")]
    late_options = JourneyFixture(options=(JourneyOption("return-1830", 1110, 1190, 2),))
    stale_memory = [{"fault_id": "stale_preference_memory", "after_event": None,
                     "parameters": [{"name": "preference_version", "value": 1}]}]
    refund_findings = derive_findings("run-faulted-1", experiment["faulted"],
                                      recorded_at=RECORDED_AT)
    return {
        "source_commit": source_commit,
        "refund_baseline_run": dumped(run_to_world(experiment["baseline"])),
        "refund_fork_findings": [dumped(finding_to_world(f)) for f in refund_findings],
        "refund_repair": refund_repair("run-repaired-1", experiment["repaired"],
                                       experiment["faulted"]),
        "refund_repair_action": bound_action(refund, RefundFixture(), refund_proposal),
        "refund_replay": refund_replay(examples / "capsule_v1.refund_failure.json"),
        "journey_baseline_run": run(fixture, JourneyScenario.from_json(scenario_json()), NAIVE),
        "journey_fork_findings": journey.findings(fixture, "journey-run-1", published_fork,
                                                  RECORDED_AT, lab),
        "journey_repair_held_by_adapter":
            journey_repair("journey-repair-adapter", faulted_scenario(20)),
        "journey_repair_held_by_boundary":
            journey_repair("journey-repair-boundary", faulted_scenario(20, after_event=17),
                           fork_adapter=CHECKED),
        "journey_repair_truncated":
            journey_repair("journey-repair-truncated", faulted_scenario(12)),
        "journey_repair_idle":
            journey_repair("journey-repair-idle", faulted_scenario(20, faults=stale_memory),
                           late_options),
        "journey_repair_lost_fault":
            journey_repair("journey-repair-lost", faulted_scenario(20, faults=missed_fault)),
        "journey_repair_action": bound_action(journey, fixture, journey_proposal),
        "journey_unfired": truncated_experiment(faulted_scenario(20, after_event=15)),
        "journey_race": truncated_experiment(faulted_scenario(20, after_event=17), CHECKED),
        "journey_race_action": bound_action(journey, fixture, {
            "adapter_id": CHECKED, "scenario": faulted_scenario(20, after_event=17)}),
        "journey_truncated_clean": truncated_experiment(faulted_scenario(3)),
        "journey_truncated_violation": truncated_experiment(truncated_violation),
        "journey_truncated_action": bound_action(journey, fixture, {
            "adapter_id": NAIVE, "scenario": truncated_violation}),
        "journey_replay": journey_replay(examples / "capsule_v2.journey_fixture.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    data = record(args.examples, args.source_commit)
    body = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True)
    args.out.write_text(
        "// Generated by web/tests/fixtures/record_core_outputs.py. Do not edit.\n"
        "// Real output of the checked-in worlds, adapters, verifier and capsule replay,\n"
        f"// with M05's world drivers at {args.source_commit}.\n"
        "// No model or network call was made. These are test fixtures, not service results.\n"
        'import type { RecordedCoreOutputs } from "./recordedTypes.ts";\n\n'
        f"export const recorded: RecordedCoreOutputs = {body};\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
