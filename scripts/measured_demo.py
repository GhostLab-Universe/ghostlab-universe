"""Measured BYOE demonstration (P6): external env + external agents, end to end.

Connects the vendored reservation service (developed outside this tree)
through its reviewed adapter, runs two dependency-free external agents
(see examples/p6_demo_agents.py), and proves the full loop: qualify,
detect a genuine rule violation, reproduce it in a clean setup, minimize
the faults, and retest the fixed agent. Writes report.json and report.md.

No model calls, no network. reservation-world/1 is not a frozen pack
version, so evidence is recorded as JSON, not a v3 capsule; the report
states that boundary instead of working around it. Held-out evaluation is
owned by another session: without a published runner it stays PENDING and
dev results are never substituted.

Exit 0 when every expectation holds, 1 otherwise.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from p6_demo_agents import BlindBooker, CarefulBooker  # noqa: E402

from ghostlab.adapters.reservation_env import (ENVIRONMENT_MANIFEST,  # noqa: E402
                                               RESERVATION_WORLD_VERSION,
                                               SERVICE_PROVENANCE,
                                               ReservationEnvironment)
from ghostlab.extensions.environment import Action  # noqa: E402
from ghostlab.extensions.qualify import ProbeScript, qualify_environment  # noqa: E402
from ghostlab.extensions.registry import AdapterRegistry  # noqa: E402
from ghostlab.extensions.runner import run_attempt  # noqa: E402
from ghostlab.extensions.target import TargetTask  # noqa: E402
from ghostlab.replay import (allow_tools, assess_fidelity, check_verdicts,  # noqa: E402
                             failure_witness, replay_recorded, witness_sha256)
from ghostlab.replay.minimize_ext import RunEvidence, minimize_faults  # noqa: E402
from ghostlab.targets.external import ExternalTargetAdapter  # noqa: E402
from ghostlab.verify.heldout import HELDOUT_RUNNER_VERSION  # noqa: E402

WORLD = RESERVATION_WORLD_VERSION
FIXTURE = "near_full"
ACTOR = "demo-booker-task"
FAULTS = ("timeout_after_commit", "stale_snapshot", "skip_capacity_check")
FULL_SCENARIO = {"timeout_after_commit": True, "stale_snapshot": True,
                 "skip_capacity_check": True, "max_tool_calls": 20}
CLEAN_SCENARIO = {"timeout_after_commit": False, "stale_snapshot": False,
                  "skip_capacity_check": False, "max_tool_calls": 20}
GATEWAY_TOOLS = ["check_availability", "reserve", "cancel", "get_status"]


def registry():
    registry = AdapterRegistry()
    registry.register_environment(WORLD, ReservationEnvironment, ENVIRONMENT_MANIFEST)
    return registry


def run_agent(env_factory, agent, scenario):
    env = env_factory()
    env.reset(env.named_fixture(FIXTURE), dict(scenario), actor_id=ACTOR)
    attempt = run_attempt(env, ExternalTargetAdapter(agent),
                          TargetTask(task_id="book-two-seats", params={}),
                          authorize=allow_tools(*GATEWAY_TOOLS), cap=20)
    evaluator = env_factory()
    try:
        checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
    finally:
        evaluator.close()
    info = attempt
    return {"trace": list(info.trace), "state": dict(info.state),
            "status": info.status, "tool_calls": info.tool_calls,
            "verdicts": check_verdicts(checks),
            "failed": sorted(name for name, hit in check_verdicts(checks).items()
                             if hit is False),
            "witness": failure_witness(checks)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="demo-out/measured-demo",
                        help="report directory (created)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    reg = registry()
    factory = reg.environment_factory(WORLD)
    failures = []

    def expect(label, condition, detail=""):
        print(("PASS " if condition else "FAIL ") + label
              + (f" ({detail})" if detail else ""))
        if not condition:
            failures.append(label)

    # 1. Qualify the external environment.
    qual = qualify_environment(
        world_version=WORLD, env_factory=factory,
        manifest=reg.environment_manifest(WORLD),
        probe=ProbeScript(
            fixture=FIXTURE, scenario=dict(CLEAN_SCENARIO),
            script=[Action(tool="check_availability", arguments={}),
                    Action(tool="reserve", arguments={"holder": "demo", "seats": 1}),
                    Action(tool="cancel", arguments={"reservation_id": "R-0003"})],
            task_id="demo-qualify"),
        actor_id=ACTOR).to_dict()
    expect("qualification passes", qual["passed"],
           f"{sum(1 for c in qual['checks'] if c['passed'])}/{len(qual['checks'])} checks")

    # 2. Fidelity: determinism, replay and reference behavior.
    script = [Action(tool="check_availability", arguments={}),
              Action(tool="reserve", arguments={"holder": "demo", "seats": 1})]
    probe_env = factory()
    probe_env.reset(probe_env.named_fixture(FIXTURE), dict(CLEAN_SCENARIO),
                    actor_id=ACTOR)
    try:
        snapshot = probe_env.named_fixture(FIXTURE)
    finally:
        probe_env.close()
    fidelity = assess_fidelity(
        env_factory=factory, fixture=snapshot, scenario=dict(CLEAN_SCENARIO),
        actor_id=ACTOR, script=script, repeats=3,
        declared_tested=["reset", "isolation", "schema agreement", "replay"],
        declared_approximations=["faults are injected flags, not real latency"],
        declared_omissions=["multi-holder contention", "restart recovery"]).to_dict()
    expect("fidelity passes", fidelity["passed"], fidelity["world_version"])

    # 3. Defective agent under the fault schedule: expect a genuine violation.
    defective = run_agent(factory, BlindBooker(), FULL_SCENARIO)
    valid = defective["failed"] == ["capacity_respected"]
    expect("defective agent violates capacity_respected", valid,
           f"failed={defective['failed']}")

    # 4. False findings: both agents on the clean schedule must pass.
    clean_defective = run_agent(factory, BlindBooker(), CLEAN_SCENARIO)
    clean_fixed = run_agent(factory, CarefulBooker(), CLEAN_SCENARIO)
    false_findings = (1 if clean_defective["failed"] else 0) + (
        1 if clean_fixed["failed"] else 0)
    expect("no false findings on clean schedule", false_findings == 0)

    # 5. Reproduce the violation in a clean setup (fresh instance, same evidence).
    replayed = replay_recorded(
        registry=reg, world_version=WORLD, fixture=dict(snapshot),
        scenario=dict(FULL_SCENARIO), actor_id=ACTOR,
        recorded_trace=defective["trace"], recorded_state=defective["state"],
        recorded_checks=defective["verdicts"]).to_dict()
    expect("recorded-action replay reproduces", replayed["reproduced"])

    # 6. Minimize the fault schedule, keeping the same witness.
    def run_subset(subset):
        scenario = dict(CLEAN_SCENARIO)
        scenario.update({name: name in subset for name in FAULTS})
        attempt = run_agent(factory, BlindBooker(), scenario)
        evaluator = factory()
        try:
            live = evaluator.evaluate(trace=attempt["trace"], state=attempt["state"])
        finally:
            evaluator.close()
        return RunEvidence(status=attempt["status"], checks=live,
                           tool_calls=attempt["tool_calls"])

    minimized = minimize_faults(run=run_subset, faults=list(FAULTS),
                                invariant="capacity_respected").to_dict()
    witness_kept = (minimized["failure_witness_sha256"]
                    == witness_sha256(defective["witness"]))
    reduced = (minimized["selected_faults"] is not None
               and len(minimized["selected_faults"]) < len(FAULTS)
               and "skip_capacity_check" in minimized["selected_faults"])
    expect("minimizer reduces faults with the same witness", witness_kept and reduced,
           f"{list(FAULTS)} -> {minimized['selected_faults']}")

    # 7. Retest the fixed agent under the same fault schedule.
    fixed = run_agent(factory, CarefulBooker(), FULL_SCENARIO)
    expect("fixed agent passes the same schedule", fixed["failed"] == [],
           f"failed={fixed['failed']}")

    # 8. Repeated-run variation: the violation is stable.
    repeats = [run_agent(factory, BlindBooker(), FULL_SCENARIO)["failed"]
               for _ in range(3)]
    stable = all(failed == defective["failed"] for failed in repeats)
    expect("violation stable across 3 repeats", stable)

    report = {
        "demo": "ghostlab-measured-demo/1",
        "date": datetime.date.today().isoformat(),
        "world_version": WORLD,
        "environment_provenance": SERVICE_PROVENANCE,
        "fixture": FIXTURE,
        "defective_agent": {"agent_id": BlindBooker.agent_id,
                            "agent_version": BlindBooker.agent_version},
        "fixed_agent": {"agent_id": CarefulBooker.agent_id,
                        "agent_version": CarefulBooker.agent_version},
        "integration_effort": [
            "environment: vendored external service + reviewed adapter (P2, no core edits)",
            "agents: two dependency-free classes, dicts in/out, no GhostLab imports",
            "registration: environment factory on an explicit operator registry",
        ],
        "qualification": {"passed": qual["passed"],
                          "missing_capabilities": qual["missing_capabilities"]},
        "fidelity": {"passed": fidelity["passed"],
                     "determinism_identical": fidelity["determinism"]["identical"],
                     "replay_reproduced": fidelity["replay"]["reproduced"]},
        "valid_failures": 1 if valid else 0,
        "failed_invariants": defective["failed"],
        "false_findings": false_findings,
        "replay_reproduced": replayed["reproduced"],
        "minimized_faults": minimized["selected_faults"],
        "witness_preserved": witness_kept,
        "fixed_retest_passed": fixed["failed"] == [],
        "repeats_stable_3x": stable,
        "inference_calls": 0,
        "envelope": ("evidence JSON (no v3 capsule: reservation-world/1 is not a "
                     "frozen pack version; world-version extensibility is an M00 "
                     "follow-up)"),
        "held_out": {"status": "pending",
                     "reason": ("no runner published by the held-out owner; interface "
                                + HELDOUT_RUNNER_VERSION),
                     "dev_results_substituted": False},
        "expectations_failed": failures,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n",
                                     encoding="utf-8")
    lines = ["# Measured demo: bring your environment, bring your agent",
             "", f"Date: {report['date']}", f"World: {WORLD} ({SERVICE_PROVENANCE})",
             f"Fixture: {FIXTURE}",
             f"Agents: {BlindBooker.agent_id} {BlindBooker.agent_version} (defective) vs "
             f"{CarefulBooker.agent_version} (fixed)",
             "", "## Results", ""]
    for key in ("valid_failures", "false_findings", "replay_reproduced",
                "minimized_faults", "witness_preserved", "fixed_retest_passed",
                "repeats_stable_3x", "inference_calls"):
        lines.append(f"- {key}: {report[key]!r}")
    lines += ["", "## Boundaries", "",
              f"- envelope: {report['envelope']}",
              f"- held-out: {report['held_out']['status']} "
              f"({report['held_out']['reason']})"]
    if failures:
        lines += ["", "## FAILED EXPECTATIONS", ""] + [f"- {name}" for name in failures]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out / 'report.json'} and {out / 'report.md'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
