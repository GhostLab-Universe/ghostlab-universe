"""Find a smaller reproducing fault subset, with an explicit finite search scope.

Delta-debugging-inspired enumeration; never edits a fixture, adapter, fault timing,
permission boundary, tool budget or source capsule. Only checked-in deterministic
adapters are supported. A recorded-action replay is not proof that a capsule came
from that adapter, so its complete fresh run must match before search begins.
"""

import hashlib
import itertools
import json
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ghostlab import capsule as refund_capsule
from ghostlab.core import Fixture, Scenario, run as refund_run
from ghostlab.worlds import journey_capsule
from ghostlab.worlds.journey import JourneyFixture, JourneyScenario
from ghostlab.worlds.journey_run import run as journey_run
from ghostlab.worlds.journey_verify import sharing_at

REPORT_VERSION = "ghostlab-minimization/1"
REFUND, JOURNEY = "refund-world/1", "group-journey/1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class SearchLimit(Exception):
    pass


class Budget:
    def __init__(self, max_runs, deadline_ms, clock):
        if type(max_runs) is not int or not 1 <= max_runs <= 64:
            raise ValueError("max_runs must be an integer from 1 to 64")
        if type(deadline_ms) is not int or not 1 <= deadline_ms <= 60_000:
            raise ValueError("deadline_ms must be an integer from 1 to 60000")
        self.max_runs, self.deadline_ms, self.clock = max_runs, deadline_ms, clock
        self.started, self.used = clock(), 0

    def call(self, fn):
        if (self.clock() - self.started) * 1000 >= self.deadline_ms:
            raise SearchLimit("deadline_exceeded")
        if self.used >= self.max_runs:
            raise SearchLimit("run_budget_exhausted")
        self.used += 1
        return fn()


@dataclass
class Problem:
    world: str
    payload: dict
    fixture: object
    replay: object

    @property
    def original_run(self):
        return self.payload["run"]

    @property
    def faults(self):
        scenario = self.payload["scenario"]
        return ([f["fault_id"] for f in scenario["faults"]] if self.world == JOURNEY
                else ["timeout_after_commit"] if scenario["timeout_after_commit"] else [])

    def scenario(self, kept):
        original = self.payload["scenario"]
        if self.world == JOURNEY:
            return {**original, "faults": [f for f in original["faults"] if f["fault_id"] in kept]}
        return {**original, "timeout_after_commit": "timeout_after_commit" in kept}

    def run(self, kept):
        data = self.scenario(kept)
        actor = self.payload["actor_id"]
        if self.world == JOURNEY:
            return journey_run(self.fixture, JourneyScenario.from_json(data),
                               self.original_run["adapter_id"], actor_id=actor)
        return refund_run(self.fixture, Scenario(**data), self.original_run["adapter"],
                          actor_id=actor)

    def export(self, path, kept, recorded):
        if self.world == JOURNEY:
            journey_capsule.export_capsule(path, self.fixture, self.scenario(kept), recorded)
        else:
            refund_capsule.export_capsule(path, self.fixture, Scenario(**self.scenario(kept)), recorded)


def load_problem(path):
    if path.stat().st_size > refund_capsule.MAX_CAPSULE_BYTES:
        raise ValueError("capsule is too large")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = envelope.get("payload") if type(envelope) is dict else None
    if type(payload) is not dict:
        raise ValueError("invalid capsule envelope")
    world = payload.get("world_version")
    version = payload.get("schema_version")
    if world == JOURNEY and type(version) is int and version == 2:
        payload = journey_capsule.load_capsule(path)
        return Problem(world, payload, JourneyFixture.from_json(payload["fixture"]),
                       journey_capsule.replay)
    if world == REFUND and type(version) is int and version == 1:
        payload = refund_capsule.load_capsule(path)
        return Problem(world, payload, Fixture(**payload["fixture"]), refund_capsule.replay)
    raise ValueError("only refund v1 and journey v2 capsules are supported")


def unpermitted_recipients(run):
    state = run["final_state"]
    return {r["recipient_id"] for r in state["delivery_log"]
            if not sharing_at(state, r["recipient_id"], r["event_id"])}


def classify(run, invariant, recipients):
    reproduces = run["checks"][invariant] is False
    if reproduces and recipients:
        reproduces = recipients <= unpermitted_recipients(run)
    if reproduces:
        return "reproduced"
    return "inconclusive" if run["status"] != "completed" else "not_reproduced"


def minimize_capsule(source: Path, out: Path, *, invariant: str, max_runs=16,
                     deadline_ms=5000, clock=time.monotonic):
    """Snapshot a bounded input once, so concurrent edits cannot change the experiment."""
    with source.open("rb") as handle:
        data = handle.read(refund_capsule.MAX_CAPSULE_BYTES + 1)
    if len(data) > refund_capsule.MAX_CAPSULE_BYTES:
        raise ValueError("capsule is too large")
    with tempfile.TemporaryDirectory(prefix="ghostlab-minimize-") as directory:
        snapshot = Path(directory) / "input.json"
        snapshot.write_bytes(data)
        return _minimize_snapshot(snapshot, out, invariant=invariant, max_runs=max_runs,
                                  deadline_ms=deadline_ms, clock=clock)


def _minimize_snapshot(source: Path, out: Path, *, invariant: str, max_runs,
                       deadline_ms, clock):
    """Write a diagnostic report, source copy, confirmed capsule and JSON regression case.

    Minimality means smallest fault *count*, among subsets of this exact schedule.
    It says nothing about other fixtures, timings, adapters or production systems.
    Exhausted/truncated negative trials cannot establish absence of a failure.
    """
    budget = Budget(max_runs, deadline_ms, clock)
    if out.exists():
        raise ValueError("output directory must not already exist")
    source_bytes = source.read_bytes() if source.stat().st_size <= refund_capsule.MAX_CAPSULE_BYTES else None
    if source_bytes is None:
        raise ValueError("capsule is too large")
    problem = load_problem(source)
    if type(invariant) is not str or problem.original_run.get("checks", {}).get(invariant) is not False:
        raise ValueError("select an invariant that failed in the original recording")
    recipients = (unpermitted_recipients(problem.original_run)
                  if problem.world == JOURNEY and invariant == "delivered_messages_permitted" else set())
    report = {
        "schema": REPORT_VERSION, "world_version": problem.world, "invariant": invariant,
        "recipient_witnesses": sorted(recipients), "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "scope": "fault-subsets-only; fixture, adapter, actor, mode, timings and tool cap fixed",
        "original_faults": problem.faults, "selected_faults": None, "removed_faults": [],
        "trials": [], "max_runs": max_runs, "deadline_ms": deadline_ms,
        "cardinality_minimal": False, "capsule_reproduced": False, "model_calls": 0,
        "status": "incomplete", "minimal_capsule_sha256": None}
    best_run, best_faults, unknown_sizes, smaller = None, None, [], False
    try:
        if budget.call(lambda: problem.replay(source))["reproduced"] is not True:
            raise ValueError("original capsule evidence does not reproduce")
        original = budget.call(lambda: problem.run(problem.faults))
        if canonical(original) != canonical(problem.original_run):
            raise ValueError("recorded actions do not match the checked-in adapter's fresh run")
        best_run, best_faults = original, list(problem.faults)
        if classify(original, invariant, recipients) != "reproduced":
            raise ValueError("selected failure cannot be reproduced")
        found = False
        for size in range(len(problem.faults)):
            for subset in itertools.combinations(problem.faults, size):
                run = budget.call(lambda subset=subset: problem.run(subset))
                result = classify(run, invariant, recipients)
                report["trials"].append({"faults": list(subset), "result": result,
                                          "run_status": run["status"], "tool_calls": len(run["trace"])})
                if result == "inconclusive":
                    unknown_sizes.append(size)
                if result == "reproduced":
                    best_run, best_faults, found, smaller = run, list(subset), True, True
                    break
            if found:
                break
        # This includes the output replay in the run cap. It makes no model call.
        def confirm():
            out.mkdir(parents=True)
            path = out / "minimal-capsule.json"
            problem.export(path, best_faults, best_run)
            return problem.replay(path)["reproduced"]

        confirmed = budget.call(confirm)
        if confirmed is not True:
            raise ValueError("candidate capsule failed reproduction")
        inconclusive = any(size < len(best_faults) for size in unknown_sizes)
        report.update(cardinality_minimal=not inconclusive, capsule_reproduced=True,
                      minimal_capsule_sha256=hashlib.sha256((out / "minimal-capsule.json").read_bytes()).hexdigest(),
                      status=("smallest_found" if inconclusive else "baseline_failure" if not best_faults
                              else "reduced" if smaller else "already_minimal"))
    except SearchLimit as limit:
        report["status"] = str(limit)
    report.update(runs_used=budget.used, selected_faults=best_faults,
                  removed_faults=[f for f in problem.faults if best_faults is not None and f not in best_faults])
    out.mkdir(parents=True, exist_ok=True)
    (out / "original-capsule.json").write_bytes(source_bytes)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report["capsule_reproduced"]:
        regression = {"schema": "ghostlab-diagnostic-regression/1", "capsule": "minimal-capsule.json",
                      "sha256": report["minimal_capsule_sha256"], "invariant": invariant,
                      "expected_passed": False, "recipient_witnesses": sorted(recipients)}
        (out / "regression-case.json").write_text(json.dumps(regression, indent=2, sort_keys=True) + "\n",
                                                  encoding="utf-8")
    return report
