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
from ghostlab.worlds.journey_verify import (sharing_at, approved_version_at, _infeasibilities)

REPORT_VERSION = "ghostlab-minimization/2"
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

    def call(self, fn, *, executions=1):
        if (self.clock() - self.started) * 1000 >= self.deadline_ms:
            raise SearchLimit("deadline_exceeded")
        if self.used + executions > self.max_runs:
            raise SearchLimit("run_budget_exhausted")
        self.used += executions
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


def _without_event_times(value):
    if isinstance(value, dict):
        return {k: _without_event_times(v) for k, v in value.items()
                if k not in {"event_id", "from_event", "confirmed_at", "cancelled_at",
                             "held_at", "queued_at", "delivered_at"}}
    if isinstance(value, list):
        return [_without_event_times(v) for v in value]
    return value


def failure_witness(problem, run, invariant):
    """Conservative domain evidence, not just a shared invariant/recipient label.

    Logical event positions may move. A disclosed message's identity/template cannot;
    feasibility retains typed problem keys and booking/calendar evidence. Keeping extra
    context can prevent a reduction; silently switching the demonstrated failure cannot.
    """
    state = run["final_state"]
    if problem.world == REFUND:
        return {"ledger": _without_event_times(state["ledger"])}
    if invariant == "delivered_messages_permitted":
        witnesses = []
        for row in state["delivery_log"]:
            if not sharing_at(state, row["recipient_id"], row["event_id"]):
                message = state["messages"][row["message_id"]]
                witnesses.append({"recipient_id": row["recipient_id"],
                                  "message": _without_event_times(message),
                                  "permission_at_delivery": False})
        return sorted(witnesses, key=canonical)
    if invariant == "commitments_use_approved_intent":
        witnesses = []
        for row in state["commitment_log"]:
            approved = approved_version_at(state, row["event_id"])
            if row["intent_version"] != approved:
                booking = next((r for r in state["reservations"].values()
                                if r["reservation_id"] == row.get("ref")), None)
                witnesses.append({"commitment": _without_event_times(row),
                                  "approved_version": approved,
                                  "reservation": _without_event_times(booking)})
        return sorted(witnesses, key=canonical)
    if invariant == "mandatory_participants_feasible":
        # Version-pinned verifier problem keys distinguish late/double/overlap/etc.
        # Full domain context is intentionally conservative, excluding sharing state.
        problems = _infeasibilities(problem.fixture, state)
        return {"problem_keys": sorted([list(key) for key in problems], key=canonical),
                "reservations": _without_event_times(state["reservations"]),
                "calendar": _without_event_times(state["calendar"])}
    raise ValueError("unsupported failure witness")


def classify(run, invariant, recipients, *, problem=None, expected_witness=None):
    reproduces = run["checks"][invariant] is False
    if reproduces and recipients:
        reproduces = recipients <= unpermitted_recipients(run)
    if reproduces and problem is not None:
        reproduces = canonical(failure_witness(problem, run, invariant)) == canonical(expected_witness)
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
    witness = failure_witness(problem, problem.original_run, invariant)
    report = {
        "schema": REPORT_VERSION, "world_version": problem.world, "invariant": invariant,
        "recipient_witnesses": sorted(recipients), "failure_witness": witness,
        "failure_witness_sha256": hashlib.sha256(canonical(witness)).hexdigest(),
        "candidate_faults": None, "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "scope": "fault-subsets-only; fixture, adapter, actor, mode, timings and tool cap fixed",
        "original_faults": problem.faults, "selected_faults": None, "removed_faults": [],
        "trials": [], "max_runs": max_runs, "deadline_ms": deadline_ms,
        "cardinality_minimal": False, "capsule_reproduced": False, "model_calls": 0,
        "status": "incomplete", "minimal_capsule_sha256": None}
    # Atomically claim the new output directory. A competing creator is never reused.
    try:
        out.mkdir(parents=True)
    except FileExistsError as error:
        raise ValueError("output directory must not already exist") from error
    best_run, best_faults, unknown_sizes, smaller = None, None, [], False
    try:
        if budget.call(lambda: problem.replay(source), executions=2)["reproduced"] is not True:
            raise ValueError("original capsule evidence does not reproduce")
        original = budget.call(lambda: problem.run(problem.faults))
        if canonical(original) != canonical(problem.original_run):
            raise ValueError("recorded actions do not match the checked-in adapter's fresh run")
        best_run, best_faults = original, list(problem.faults)
        if classify(original, invariant, recipients, problem=problem, expected_witness=witness) != "reproduced":
            raise ValueError("selected failure cannot be reproduced")
        found = False
        for size in range(len(problem.faults)):
            for subset in itertools.combinations(problem.faults, size):
                run = budget.call(lambda subset=subset: problem.run(subset))
                result = classify(run, invariant, recipients, problem=problem, expected_witness=witness)
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
            # Confirm in private temporary storage; publish no unconfirmed capsule.
            with tempfile.TemporaryDirectory(prefix="ghostlab-candidate-") as temporary:
                path = Path(temporary) / "candidate.json"
                problem.export(path, best_faults, best_run)
                if problem.replay(path)["reproduced"] is not True:
                    return False
                with (out / "minimal-capsule.json").open("xb") as handle:
                    handle.write(path.read_bytes())
                return True

        confirmed = budget.call(confirm, executions=2)
        if confirmed is not True:
            raise ValueError("candidate capsule failed reproduction")
        inconclusive = any(size < len(best_faults) for size in unknown_sizes)
        report.update(cardinality_minimal=not inconclusive, capsule_reproduced=True,
                      minimal_capsule_sha256=hashlib.sha256((out / "minimal-capsule.json").read_bytes()).hexdigest(),
                      status=("smallest_found" if inconclusive else "baseline_failure" if not best_faults
                              else "reduced" if smaller else "already_minimal"))
    except SearchLimit as limit:
        report["status"] = str(limit)
    except ValueError:
        # Refusals happen before anything is published: release the claimed
        # directory while still empty so a retry reports the real cause.
        try:
            out.rmdir()
        except OSError:
            pass
        raise
    selected = best_faults if report["capsule_reproduced"] else None
    report.update(runs_used=budget.used, selected_faults=selected, candidate_faults=best_faults,
                  removed_faults=[f for f in problem.faults if selected is not None and f not in selected])
    with (out / "original-capsule.json").open("xb") as handle:
        handle.write(source_bytes)
    with (out / "report.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["capsule_reproduced"]:
        regression = {"schema": "ghostlab-diagnostic-regression/2", "capsule": "minimal-capsule.json",
                      "sha256": report["minimal_capsule_sha256"], "invariant": invariant,
                      "expected_passed": False, "failure_witness_sha256": report["failure_witness_sha256"],
                      "failure_witness": witness, "recipient_witnesses": sorted(recipients)}
        with (out / "regression-case.json").open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(regression, indent=2, sort_keys=True) + "\n")
    return report
