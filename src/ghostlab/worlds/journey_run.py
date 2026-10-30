"""Checked-in journey adapters, runs, Reality Diff and the fault experiment.

Both adapters are deterministic, trusted local code: no model is called. A run
returns a dict shaped exactly like the ``world_run_result/1`` contract; a diff
is shaped like ``world_reality_diff/1``. Verdicts come from
``journey_verify``, never from the adapters.
"""

from dataclasses import replace

from ghostlab.execution.journey_guard import make_world

from .journey import (WORLD_VERSION, BudgetExceeded, JourneyFixture, JourneyScenario,
                      ToolError, scenario_json)
from .journey_verify import INVARIANTS, VERIFIER_VERSION, metrics, verify, verify_details

ADAPTER_VERSION = "journey-adapters/1"
RETURN_ENTRY = "return-journey"


def _attempt(world, tool, arguments):
    """A refused call is already recorded in the trace; the adapter carries on."""
    try:
        return world.call(tool, arguments)
    except BudgetExceeded:
        raise
    except ToolError:
        return None


def _inspect(world):
    return _attempt(world, "inspect_constraints", {"journey_id": world.fixture.journey_id})


def _mandatory(seen):
    return [p["participant_id"] for p in seen["participants"] if p["mandatory"]]


def _sharing(seen, participant):
    return any(p["participant_id"] == participant and p["sharing"]
               for p in seen["participants"])


def _book(world, seen, version, return_by):
    """Hold, confirm and schedule the latest option that arrives by ``return_by``."""
    travellers = _mandatory(seen)
    fitting = [o for o in seen["options"]
               if o["arrive"] <= return_by and o["seats_left"] >= len(travellers)]
    option = max(fitting, key=lambda o: o["arrive"], default=None)
    if option is None:
        return False
    hold = _attempt(world, "hold_availability", {"journey_id": world.fixture.journey_id,
                                                 "option_id": option["option_id"],
                                                 "intent_version": version})
    if hold is None:
        return False
    return _confirm_and_schedule(world, hold["hold_id"], version, travellers)


def _confirm_and_schedule(world, hold_id, version, travellers):
    if _attempt(world, "confirm_reservation",
                {"hold_id": hold_id, "intent_version": version}) is None:
        return False
    for traveller in travellers:
        _attempt(world, "update_calendar_event", {"participant_id": traveller,
                                                  "event_id": RETURN_ENTRY,
                                                  "intent_version": version})
    return True


def journey_naive(world):
    """Takes one traveller's agreement as everyone's, trusts memory, lets the latest
    arrival win, and tells every participant without re-checking permission."""
    seen = _inspect(world)
    if seen is None:
        return
    intent = {"intent_version": seen["intent_version"], "return_by": seen["return_by"]}
    for key in ("remembered_intent", "incoming_update"):
        candidate = seen.get(key)
        if candidate and candidate["agreed_by"]:  # one traveller agreed: good enough
            intent = candidate
    if not _book(world, seen, intent["intent_version"], intent["return_by"]):
        return
    refreshed = _inspect(world)
    update = refreshed and refreshed.get("incoming_update")
    if update and update["agreed_by"] and update["intent_version"] != intent["intent_version"]:
        intent = update  # newest by arrival, so it must be newest by intent
        _book(world, refreshed, intent["intent_version"], intent["return_by"])
    template = f"itinerary-v{intent['intent_version']}"
    queued = [_attempt(world, "queue_message", {"recipient_id": p["participant_id"],
                                                "template_id": template})
              for p in seen["participants"]]
    for message in queued:
        if message is not None:
            _attempt(world, "deliver_message", {"message_id": message["message_id"]})


def journey_version_checked(world):
    """Acts only on the version both mandatory travellers approved, re-reads it before
    confirming, and re-reads permission after queueing and before each delivery.

    Check-then-deliver still leaves one event between the check and the delivery:
    a revocation that lands exactly on the delivery event gets through in the lab
    world. Only the guarded boundary closes that window.
    """
    seen = _inspect(world)
    if seen is None:
        return
    version = seen["intent_version"]  # memory and late updates are observations only
    travellers = _mandatory(seen)
    fitting = [o for o in seen["options"]
               if o["arrive"] <= seen["return_by"] and o["seats_left"] >= len(travellers)]
    option = max(fitting, key=lambda o: o["arrive"], default=None)
    if option is None:
        return
    hold = _attempt(world, "hold_availability", {"journey_id": world.fixture.journey_id,
                                                 "option_id": option["option_id"],
                                                 "intent_version": version})
    if hold is None:
        return
    check = _inspect(world)
    if check is None or check["intent_version"] != version:
        _attempt(world, "release_availability", {"hold_id": hold["hold_id"]})
        return
    if not _confirm_and_schedule(world, hold["hold_id"], version, travellers):
        return
    for participant in (p["participant_id"] for p in seen["participants"]):
        before = _inspect(world)
        if before is None or not _sharing(before, participant):
            continue
        message = _attempt(world, "queue_message", {"recipient_id": participant,
                                                    "template_id": f"itinerary-v{version}"})
        if message is None:
            continue
        after = _inspect(world)
        if after is None or not _sharing(after, participant):
            continue  # withdrawn while queued: leave it undelivered
        _attempt(world, "deliver_message", {"message_id": message["message_id"]})


ADAPTERS = {"journey-naive": journey_naive, "journey-version-checked": journey_version_checked}


def run(fixture: JourneyFixture, scenario: JourneyScenario, adapter_id: str, *,
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


def reality_diff(fixture: JourneyFixture, baseline: dict, faulted: dict) -> dict:
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


def findings(fixture: JourneyFixture, recorded: dict, run_id: str, recorded_at: str) -> list:
    """``world_invariant_finding/1``-shaped verdicts; the caller supplies the timestamp."""
    return [{"schema_version": 1, "finding_id": f"{run_id}-{name}", "run_id": run_id,
             "world_version": WORLD_VERSION, "invariant": name,
             "verifier_version": VERIFIER_VERSION,
             "verdict": "passed" if detail["passed"] else "failed",
             "evidence_event_ids": detail["evidence_event_ids"],
             "observed_value": detail["observed_value"], "limit_value": detail["limit_value"],
             "unit": detail["unit"], "recorded_at": recorded_at}
            for name, detail in verify_details(fixture, recorded["final_state"]).items()]


def fault_scenarios(fixture: JourneyFixture) -> dict:
    """One ``world_scenario/1`` per fault, each at the fault's default trigger."""
    superseded = min(v for v, _ in fixture.intents if v != fixture.current_version)
    return {
        "stale_preference_memory": scenario_json(
            [("stale_preference_memory", None, {"preference_version": superseded})]),
        "delayed_itinerary_update": scenario_json(
            [("delayed_itinerary_update", None, {"delay_events": 2})]),
        "sharing_revoked_while_queued": scenario_json(
            [("sharing_revoked_while_queued", None, {"recipient_id": fixture.viewers[0]})]),
    }


def race_scenario(fixture: JourneyFixture) -> dict:
    """Revocation timed to land on the version-checked adapter's delivery to the viewer.

    The trigger is derived from a fault-free version-checked run: the permission
    check right before that delivery. Revocation takes effect from the next event,
    which is the delivery itself.
    """
    viewer = fixture.viewers[0]
    lab = replace(fixture, guarded=False)
    trace = run(lab, JourneyScenario.from_json(scenario_json()), "journey-version-checked")["trace"]
    message = next(e["outcome"]["result"]["message_id"] for e in trace
                   if e["request"]["tool"] == "queue_message"
                   and e["request"]["arguments"]["recipient_id"] == viewer)
    delivery = next(e["event_id"] for e in trace if e["request"]["tool"] == "deliver_message"
                    and e["request"]["arguments"]["message_id"] == message)
    return scenario_json([("sharing_revoked_while_queued", delivery - 1,
                           {"recipient_id": viewer})])


def experiment(fixture: JourneyFixture | None = None) -> dict:
    """Baseline, each fault (naive, version-checked, guarded naive) and the race."""
    fixture = replace(fixture or JourneyFixture(), guarded=False)
    guarded = replace(fixture, guarded=True)
    clean = JourneyScenario.from_json(scenario_json())
    baseline = {adapter: run(fixture, clean, adapter) for adapter in ADAPTERS}
    faults = {}
    for fault_id, data in fault_scenarios(fixture).items():
        scenario = JourneyScenario.from_json(data)
        faulted = run(fixture, scenario, "journey-naive")
        faults[fault_id] = {
            "scenario": data, "faulted": faulted,
            "repaired": run(fixture, scenario, "journey-version-checked"),
            "guarded": run(guarded, scenario, "journey-naive"),
            "diff": reality_diff(fixture, baseline["journey-naive"], faulted)}
    race_data = race_scenario(fixture)
    race = JourneyScenario.from_json(race_data)
    race_lab = run(fixture, race, "journey-version-checked")
    return {"baseline": baseline, "faults": faults,
            "race": {"scenario": race_data, "lab": race_lab,
                     "guarded": run(guarded, race, "journey-version-checked"),
                     "diff": reality_diff(fixture, baseline["journey-version-checked"],
                                          race_lab)}}
