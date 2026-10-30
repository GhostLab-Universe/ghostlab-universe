"""Checked-in relay adapters, runs, Reality Diff and the fault experiment.

Both adapters are deterministic, trusted local code: no model is called. A run
returns a dict shaped exactly like the ``world_run_result/1`` contract; a diff is
shaped like ``world_reality_diff/1``. Verdicts come from ``relay_verify``, never
from the adapters.
"""

from dataclasses import replace

from ghostlab.execution.relay_guard import make_world

from .relay import (AUTOMATION, WORLD_VERSION, BudgetExceeded, RelayFixture, RelayScenario,
                    ReservationAckLost, ToolError, scenario_json)
from .relay_verify import INVARIANTS, VERIFIER_VERSION, metrics, verify, verify_details

ADAPTER_VERSION = "relay-adapters/1"
MAX_RETRIES = 2  # bounds both adapters' tool use; a job fits well inside the target's limit


def _attempt(world, tool, arguments):
    """A refused call is already recorded in the trace; the adapter carries on."""
    try:
        return world.call(tool, arguments)
    except BudgetExceeded:
        raise
    except ToolError:
        return None


def _inspect(world, job_id):
    return _attempt(world, "inspect_availability", {"job_id": job_id})


def _new_key(keys):
    keys.append(f"request-{len(keys) + 1}")
    return keys[-1]


def _reserve_blindly(world, job_id, resource_id, keys):
    """Any failure gets a retry under a brand-new key, without looking at what happened."""
    for _ in range(MAX_RETRIES):
        lease = _attempt(world, "reserve_resource", {"job_id": job_id, "resource_id": resource_id,
                                                     "request_key": _new_key(keys)})
        if lease is not None:
            return lease
    return None


def relay_local_success(world):
    """Each role trusts its own view: the front desk reads the availability counters and
    remembers the promise version, inventory and dispatch reserve from those counters
    and retry blindly, and the front desk confirms without looking at either result."""
    keys = []
    for job in world.fixture.jobs:
        seen = _inspect(world, job.job_id)
        if seen is None:
            continue
        if seen["part_needed"] is not None:
            _reserve_blindly(world, job.job_id, seen["part_needed"], keys)
        slot = next((r["resource_id"] for r in seen["resources"]
                     if r["kind"] == "technician" and r["available"] >= 1), None)
        if slot is not None:
            _reserve_blindly(world, job.job_id, slot, keys)
        _attempt(world, "confirm_service", {"job_id": job.job_id,
                                            "promise_version": seen["promise_version"]})


def _free(seen, resource):
    """Capacity left by the lease list, which stays true when the counters don't."""
    holders = [lease for lease in seen["leases"]
               if lease["resource_id"] == resource["resource_id"]]
    return resource["capacity"] - len(holders)


def _plan(seen):
    """What this job would lease, read from the leases; None if it can't be done."""
    by_id = {r["resource_id"]: r for r in seen["resources"]}
    slot = next((r["resource_id"] for r in seen["resources"]
                 if r["kind"] == "technician" and _free(seen, r) >= 1), None)
    part = seen["part_needed"]
    if slot is None or (part is not None and _free(seen, by_id[part]) < 1):
        return None
    return [*([part] if part is not None else []), slot]


def _reserve_checked(world, job_id, resource_id, keys):
    """A lost acknowledgement is retried under the same key, so nothing is granted twice."""
    arguments = {"job_id": job_id, "resource_id": resource_id, "request_key": _new_key(keys)}
    for _ in range(MAX_RETRIES):
        try:
            return world.call("reserve_resource", arguments)
        except ReservationAckLost:
            continue
        except BudgetExceeded:
            raise
        except ToolError:
            return None
    return None


def _still_good(check, job_id, leases):
    """The re-read: the conversation is ours and every lease is held, within capacity."""
    if check is None or check["conversation_owner"] != AUTOMATION:
        return False
    held = {lease["lease_id"] for lease in check["leases"] if lease["job_id"] == job_id}
    by_id = {r["resource_id"]: r for r in check["resources"]}
    return all(lease["lease_id"] in held and _free(check, by_id[lease["resource_id"]]) >= 0
               for lease in leases)


def _back_out(world, check, leases):
    """Give back what this job holds, unless the owner holds it now: their call, not ours."""
    if check is None or check["conversation_owner"] != AUTOMATION:
        return
    held = {lease["lease_id"] for lease in check["leases"]}
    for lease in leases:
        if lease["lease_id"] in held:
            _attempt(world, "release_resource", {"lease_id": lease["lease_id"]})


def _book_checked(world, job_id, keys):
    seen = _inspect(world, job_id)
    if seen is None or seen["conversation_owner"] != AUTOMATION:
        return
    plan = _plan(seen)
    if plan is None:
        return  # the promise can't be kept, so it isn't made
    leases = []
    for resource_id in plan:
        lease = _reserve_checked(world, job_id, resource_id, keys)
        if lease is None:
            _back_out(world, _inspect(world, job_id), leases)
            return
        leases.append(lease)
    check = _inspect(world, job_id)
    if not _still_good(check, job_id, leases):
        _back_out(world, check, leases)
        return
    confirmed = _attempt(world, "confirm_service", {"job_id": job_id,
                                                    "promise_version": check["promise_version"]})
    if confirmed is None:
        _back_out(world, _inspect(world, job_id), leases)


def relay_lease_checked(world):
    """Plans from the lease list instead of the counters, re-reads leases, the promise
    version and conversation ownership before confirming, and backs out otherwise.

    Check-then-act still leaves one event between the re-read and the action: a
    takeover, competing lease or lease expiry that lands exactly on that action gets
    through in the lab world. Only the guarded boundary closes that window.
    """
    keys = []
    for job in world.fixture.jobs:
        _book_checked(world, job.job_id, keys)


ADAPTERS = {"relay-local-success": relay_local_success, "relay-lease-checked": relay_lease_checked}


def run(fixture: RelayFixture, scenario: RelayScenario, adapter_id: str, *,
        actor_id: str | None = None) -> dict:
    if adapter_id not in ADAPTERS:
        raise ValueError("Unknown adapter")
    world = make_world(fixture, scenario, actor_id=actor_id)
    status = "completed"
    try:
        ADAPTERS[adapter_id](world)
    except BudgetExceeded:
        status = "budget_exhausted"
    state = world.snapshot()
    return {"schema_version": 1, "world_version": WORLD_VERSION, "adapter_id": adapter_id,
            "adapter_version": ADAPTER_VERSION, "actor_id": world.actor_id, "status": status,
            "trace": world.trace, "final_state": state, "checks": verify(fixture, state)}


def reality_diff(fixture: RelayFixture, baseline: dict, faulted: dict) -> dict:
    first = None
    for index in range(max(len(baseline["trace"]), len(faulted["trace"]))):
        left = baseline["trace"][index] if index < len(baseline["trace"]) else None
        right = faulted["trace"][index] if index < len(faulted["trace"]) else None
        if left != right:
            first = {"event_id": index + 1, "baseline": left, "faulted": right}
            break
    return {"schema_version": 1, "world_version": WORLD_VERSION, "first_divergence": first,
            "baseline_metrics": metrics(fixture, baseline["final_state"]),
            "faulted_metrics": metrics(fixture, faulted["final_state"]),
            "violated_invariants": [n for n in INVARIANTS if not faulted["checks"][n]]}


def findings(fixture: RelayFixture, recorded: dict, run_id: str, recorded_at: str) -> list:
    """``world_invariant_finding/1``-shaped verdicts; the caller supplies the timestamp."""
    return [{"schema_version": 1, "finding_id": f"{run_id}-{name}", "run_id": run_id,
             "world_version": WORLD_VERSION, "invariant": name,
             "verifier_version": VERIFIER_VERSION,
             "verdict": "passed" if detail["passed"] else "failed",
             "evidence_event_ids": detail["evidence_event_ids"],
             "observed_value": detail["observed_value"], "limit_value": detail["limit_value"],
             "unit": detail["unit"], "recorded_at": recorded_at}
            for name, detail in verify_details(fixture, recorded["final_state"]).items()]


def fault_scenarios(fixture: RelayFixture) -> dict:
    """One ``world_scenario/1`` per fault, each at the fault's default trigger."""
    return {
        "stale_availability_competition": scenario_json(
            [("stale_availability_competition", None,
              {"competing_jobs": len(fixture.competitors)})]),
        "lost_reservation_ack": scenario_json([("lost_reservation_ack", None, {})]),
        "takeover_with_queued_confirmation": scenario_json(
            [("takeover_with_queued_confirmation", None, {})]),
    }


def _clean_checked_trace(fixture: RelayFixture) -> list:
    """A fault-free lab run of the repaired adapter: the source of every race trigger."""
    lab = replace(fixture, guarded=False)
    return run(lab, RelayScenario.from_json(scenario_json()), "relay-lease-checked")["trace"]


def _first_job_event(trace, fixture, tool) -> int:
    job_id = fixture.jobs[0].job_id
    return next(e["event_id"] for e in trace if e["request"]["tool"] == tool
                and e["request"]["arguments"]["job_id"] == job_id)


def takeover_race_scenario(fixture: RelayFixture) -> dict:
    """Takeover timed to land on the lease-checked adapter's first confirmation.

    The trigger is derived from a fault-free run: the ownership check right before
    that confirmation. Takeover takes effect from the next event, the confirmation.
    """
    confirm = _first_job_event(_clean_checked_trace(fixture), fixture, "confirm_service")
    return scenario_json([("takeover_with_queued_confirmation", confirm - 1, {})])


def capacity_race_scenario(fixture: RelayFixture) -> dict:
    """Competitors arrive on the event the lease-checked adapter makes its first lease,
    after it read the leases and found the resource free."""
    reserve = _first_job_event(_clean_checked_trace(fixture), fixture, "reserve_resource")
    return scenario_json([("stale_availability_competition", reserve - 1,
                           {"competing_jobs": len(fixture.competitors)})])


def expiry_race_fixture(fixture: RelayFixture) -> RelayFixture:
    """A lease lifetime that ends on the event of the first confirmation.

    Derived from a fault-free run: the time between the first job's first lease and
    its confirmation, so the re-read still sees the lease and the confirmation doesn't.
    """
    trace = _clean_checked_trace(fixture)
    reserve = _first_job_event(trace, fixture, "reserve_resource")
    confirm = _first_job_event(trace, fixture, "confirm_service")
    return replace(fixture, lease_ttl_events=confirm - reserve)


def _race(baseline, lab_fixture, data):
    race = RelayScenario.from_json(data)
    lab = run(lab_fixture, race, "relay-lease-checked")
    guarded = run(replace(lab_fixture, guarded=True), race, "relay-lease-checked")
    return {"scenario": data, "fixture": lab_fixture.to_json(), "lab": lab, "guarded": guarded,
            "diff": reality_diff(lab_fixture, baseline, lab)}


def experiment(fixture: RelayFixture | None = None) -> dict:
    """Baseline, each fault (local-success, lease-checked, guarded local-success) and the
    three races."""
    fixture = replace(fixture or RelayFixture(), guarded=False)
    guarded = replace(fixture, guarded=True)
    clean = RelayScenario.from_json(scenario_json())
    baseline = {adapter: run(fixture, clean, adapter) for adapter in ADAPTERS}
    faults = {}
    for fault_id, data in fault_scenarios(fixture).items():
        scenario = RelayScenario.from_json(data)
        faulted = run(fixture, scenario, "relay-local-success")
        faults[fault_id] = {
            "scenario": data, "faulted": faulted,
            "repaired": run(fixture, scenario, "relay-lease-checked"),
            "guarded": run(guarded, scenario, "relay-local-success"),
            "diff": reality_diff(fixture, baseline["relay-local-success"], faulted)}
    checked = baseline["relay-lease-checked"]
    expiry = expiry_race_fixture(fixture)
    return {"baseline": baseline, "faults": faults, "races": {
        "owner_takeover": _race(checked, fixture, takeover_race_scenario(fixture)),
        "capacity": _race(checked, fixture, capacity_race_scenario(fixture)),
        "lease_expiry": _race(checked, expiry, scenario_json())}}
