"""The journey race through the real controller, with a scripted proposal.

    python -m evals.demo --out <dir>

``docs/demo.md`` walks through this output. Real: the controller, stores, policy, world,
verifier and capsules. Mocked: the proposal, which is a labeled fixture, and the reviewer,
which is a scripted principal. No network request is made and no model is called.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from .cases import CID, approve_and_resume, export_and_replay, start
from .evidence import campaign_evidence
from .harness import (CHECKED, FIXTURE_LABEL, JOURNEY, NAIVE, RACE_AFTER_EVENT, VIEWER, Stack,
                      Transport, journey_proposal, reply, revocation)


def _show(step: str, value: dict) -> None:
    print(f"--- {step}")
    print(json.dumps(value, indent=2, sort_keys=True))


def _experiment(stack: Stack) -> dict:
    with stack.controller() as controller:
        return next(iter(controller.state(CID)["experiments"].values()))


def _fork(stack: Stack) -> dict:
    return next(run["body"] for name, run in campaign_evidence(stack, CID)["runs"].items()
                if name.endswith("/fork"))


def run_race(root: Path) -> None:
    proposal = journey_proposal(CHECKED, faults=[revocation(RACE_AFTER_EVENT)])
    stack = Stack(root, Transport(reply(proposal)))
    paused = start(stack, JOURNEY, max_iterations=1)
    experiment, fork = _experiment(stack), _fork(stack)
    diff = experiment["diff"]
    _show("1. careful agent in the lab", {
        "adapter": CHECKED, "execution": experiment["execution"],
        "coverage": experiment["coverage"], "violated_invariants": diff["violated_invariants"],
        "baseline_unpermitted": diff["baseline_metrics"]["messages_delivered_without_permission"],
        "fork_unpermitted": diff["faulted_metrics"]["messages_delivered_without_permission"]})
    _show("2. Reality Diff", {
        "first_divergence_event": diff["first_divergence"]["event_id"],
        "permission_change": [row for row in fork["final_state"]["permission_timeline"]
                              if not row["sharing"]],
        "delivery": [row for row in fork["final_state"]["delivery_log"]
                     if row["recipient_id"] == VIEWER],
        "last_events": [{"event_id": event["event_id"], "tool": event["request"]["tool"],
                         "viewer_sharing_after": event["state"]["sharing"][VIEWER]}
                        for event in fork["trace"][-3:]]})
    _show("3a. paused for a human", {
        "status": paused["status"], "approval_id": paused["approval"]["approval_id"],
        "action_digest": paused["approval"]["action_digest"],
        "adapter": paused["approval"]["action"]["adapter"],
        "execution": paused["approval"]["action"]["execution"]})
    done = approve_and_resume(stack, paused)
    repair = done["repair"]
    _show("3b. approved by the scripted reviewer, same fault, enforced", {
        "status": done["status"], "outcome": done["outcome"], "verified": repair["verified"],
        "held_by": repair["held_by"], "guard_denials": repair["guard_denials"],
        "execution": repair["execution"], "lost_faults": repair["lost_faults"]})
    _show("4. capsule replay", {"fork": export_and_replay(stack, "fork"),
                                "repaired": export_and_replay(stack, "repaired")})


def run_untested(root: Path) -> None:
    """The revocation lands after the last message arrived, so nothing was tested."""
    stack = Stack(root, Transport(reply(journey_proposal(NAIVE, faults=[revocation(15)]))))
    done = start(stack, JOURNEY, max_iterations=1)
    _show("5. a fault that never fired", {
        "status": done["status"], "outcome": done["outcome"],
        "coverage": _experiment(stack)["coverage"], "repair": done.get("repair") or None})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="An empty working directory")
    args = parser.parse_args(argv)
    os.environ.setdefault("LANGSMITH_TRACING", "false")
    print(f"inference: mocked ({FIXTURE_LABEL})")
    run_race(args.out / "race")
    run_untested(args.out / "untested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
