import argparse
import json
from dataclasses import replace
from pathlib import Path

from .capsule import export_capsule, replay
from .core import Fixture, Scenario, experiment
from .worlds import journey_capsule, relay_capsule
from .worlds.journey import JourneyFixture
from .worlds.journey_run import experiment as journey_experiment
from .worlds.journey_verify import task_outcome, verify_details
from .worlds.relay import RelayFixture
from .worlds.relay_run import experiment as relay_experiment
from .worlds.relay_verify import task_outcome as relay_task_outcome
from .worlds.relay_verify import verify_details as relay_verify_details


def _journey_summary(fixture, recorded):
    details = verify_details(fixture, recorded["final_state"])
    return {"adapter": recorded["adapter_id"], "events": len(recorded["trace"]),
            "violated_invariants": {name: {"evidence_event_ids": d["evidence_event_ids"],
                                           "observed": d["observed_value"], "unit": d["unit"]}
                                    for name, d in details.items() if not d["passed"]},
            "denied": [{"event_id": e["event_id"], "error": e["outcome"]["error"]}
                       for e in recorded["trace"] if "error" in e["outcome"]],
            "task_outcome": task_outcome(fixture, recorded["final_state"])}


def journey_demo(capsule: Path) -> dict:
    fixture = JourneyFixture()
    result = journey_experiment(fixture)
    guarded = JourneyFixture(guarded=True)
    revocation = result["faults"]["sharing_revoked_while_queued"]
    journey_capsule.export_capsule(capsule, fixture, revocation["scenario"],
                                   revocation["faulted"])
    output = {"fixture": "synthetic group journey (2 mandatory travellers, 1 viewer)",
              "model_calls": 0,
              "baseline": {a: _journey_summary(fixture, r) for a, r in result["baseline"].items()},
              "faults": {}}
    for fault_id, case in result["faults"].items():
        output["faults"][fault_id] = {
            "first_divergence_event": case["diff"]["first_divergence"]["event_id"],
            "naive": _journey_summary(fixture, case["faulted"]),
            "version_checked": _journey_summary(fixture, case["repaired"]),
            "guarded_naive": _journey_summary(guarded, case["guarded"])}
    race = result["race"]
    output["revocation_race"] = {
        "revoked_after_event": race["scenario"]["faults"][0]["after_event"],
        "version_checked_lab": _journey_summary(fixture, race["lab"]),
        "version_checked_guarded": _journey_summary(guarded, race["guarded"])}
    output["capsule"] = str(capsule)
    return output


def _relay_summary(fixture, recorded):
    details = relay_verify_details(fixture, recorded["final_state"])
    return {"adapter": recorded["adapter_id"], "events": len(recorded["trace"]),
            "violated_invariants": {name: {"evidence_event_ids": d["evidence_event_ids"],
                                           "observed": d["observed_value"], "unit": d["unit"]}
                                    for name, d in details.items() if not d["passed"]},
            "denied": [{"event_id": e["event_id"], "error": e["outcome"]["error"]}
                       for e in recorded["trace"] if "error" in e["outcome"]],
            "task_outcome": relay_task_outcome(fixture, recorded["final_state"])}


def relay_demo(capsule: Path) -> dict:
    fixture = RelayFixture()
    result = relay_experiment(fixture)
    guarded = RelayFixture(guarded=True)
    takeover = result["faults"]["takeover_with_queued_confirmation"]
    relay_capsule.export_capsule(capsule, fixture, takeover["scenario"], takeover["faulted"])
    output = {"fixture": "synthetic repair shop (2 technician slots, 1 part, 2 jobs)",
              "model_calls": 0,
              "baseline": {a: _relay_summary(fixture, r) for a, r in result["baseline"].items()},
              "faults": {}, "races": {}}
    for fault_id, case in result["faults"].items():
        output["faults"][fault_id] = {
            "first_divergence_event": case["diff"]["first_divergence"]["event_id"],
            "local_success": _relay_summary(fixture, case["faulted"]),
            "lease_checked": _relay_summary(fixture, case["repaired"]),
            "guarded_local_success": _relay_summary(guarded, case["guarded"])}
    for name, race in result["races"].items():
        lab = RelayFixture.from_json(race["fixture"])
        output["races"][name] = {
            "lease_checked_lab": _relay_summary(lab, race["lab"]),
            "lease_checked_guarded": _relay_summary(replace(lab, guarded=True), race["guarded"])}
    output["capsule"] = str(capsule)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="GhostLab trusted synthetic world experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Execute baseline, fault and repaired adapters")
    demo.add_argument("--capsule", type=Path, default=Path("artifacts/local/refund-failure.json"))
    replay_command = commands.add_parser("replay", help="Re-execute recorded actions without a model")
    replay_command.add_argument("capsule", type=Path)
    journey = commands.add_parser("journey-demo",
                                  help="Run the group-journey faults, adapters and guard")
    journey.add_argument("--capsule", type=Path,
                         default=Path("artifacts/local/journey-failure.json"))
    journey_replay = commands.add_parser("journey-replay",
                                         help="Re-execute a v2 journey capsule without a model")
    journey_replay.add_argument("capsule", type=Path)
    relay = commands.add_parser("relay-demo",
                                help="Run the service-relay faults, adapters, guard and races")
    relay.add_argument("--capsule", type=Path, default=Path("artifacts/local/relay-failure.json"))
    relay_replay = commands.add_parser("relay-replay",
                                       help="Re-execute a v2 relay capsule without a model")
    relay_replay.add_argument("capsule", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "journey-demo":
            print(json.dumps(journey_demo(args.capsule), indent=2))
            return 0
        if args.command == "journey-replay":
            result = journey_capsule.replay(args.capsule)
            print(json.dumps(result, indent=2))
            return 0 if result["reproduced"] else 1
        if args.command == "relay-demo":
            print(json.dumps(relay_demo(args.capsule), indent=2))
            return 0
        if args.command == "relay-replay":
            result = relay_capsule.replay(args.capsule)
            print(json.dumps(result, indent=2))
            return 0 if result["reproduced"] else 1
        if args.command == "demo":
            fixture = Fixture()
            result = experiment(fixture)
            export_capsule(args.capsule, fixture, Scenario(timeout_after_commit=True), result["faulted"])
            output = {
                "fixture": "synthetic INR 2500 order", "currency": "INR", "model_calls": 0,
                "baseline": {"refunded_minor": result["diff"]["baseline_refunded_minor"],
                             "checks": result["baseline"]["checks"]},
                "faulted": {"refunded_minor": result["diff"]["faulted_refunded_minor"],
                            "violated_invariants": result["diff"]["violated_invariants"]},
                "first_divergence_event": result["diff"]["first_divergence"]["event_id"],
                "repaired": {"refunded_minor": result["repaired"]["final_state"]["total_refunded_minor"],
                             "checks": result["repaired"]["checks"]},
                "capsule": str(args.capsule),
            }
            print(json.dumps(output, indent=2))
            return 0
        result = replay(args.capsule)
        print(json.dumps(result, indent=2))
        return 0 if result["reproduced"] else 1
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, f"Invalid experiment or capsule: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
