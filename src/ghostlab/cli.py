import argparse
import json
from pathlib import Path

from .capsule import export_capsule, replay
from .core import Fixture, Scenario, experiment


def main(argv=None):
    parser = argparse.ArgumentParser(description="GhostLab trusted synthetic refund experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Execute baseline, fault and repaired adapters")
    demo.add_argument("--capsule", type=Path, default=Path("artifacts/local/refund-failure.json"))
    replay_command = commands.add_parser("replay", help="Re-execute recorded actions without a model")
    replay_command.add_argument("capsule", type=Path)
    args = parser.parse_args(argv)
    try:
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
