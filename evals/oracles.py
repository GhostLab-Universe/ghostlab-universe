"""Independent oracles: requirements restated from the product, computed from stored runs.

These share no code with the M02 verifiers, the M06 guard or the M05 findings. They read
a run's JSON and answer the question a reviewer would ask, so a bug in the code under
test can't also hide in its own check. ``expected_verdicts`` recomputes every invariant
of both worlds, and scoring compares each one with the run's checks and the controller's
findings. ``mandatory_participants_feasible`` is restated here from the rule in M02.md.
"""

GUARD_DENIALS = {"StaleIntentDenied", "PermissionWithdrawnDenied"}
FAULT_ERRORS = {"PaymentTimeout"}
# What each tool may change, restated by hand from the worlds' tools, not imported from them.
# A tool that isn't listed changes nothing, and neither does a call that was refused. A fault
# can hide a commit, so it isn't refused.
REFUND_EFFECTS = {"issue_refund": {"ledger", "total_refunded_minor"}}
JOURNEY_EFFECTS = {"hold_availability": {"holds"},
                   "release_availability": {"holds", "reservations"},
                   "confirm_reservation": {"holds", "reservations", "commitment_log"},
                   "update_calendar_event": {"calendar", "commitment_log"},
                   "queue_message": {"messages"}, "deliver_message": {"messages", "delivery_log"}}
APPEND_ONLY = ("ledger", "commitment_log", "delivery_log")  # one row per call, never rewritten
REFUND_START = {"ledger": [], "total_refunded_minor": 0}
JOURNEY_START = {"holds": {}, "reservations": {}, "messages": {}, "commitment_log": [],
                 "delivery_log": []}


def tool_counts(run: dict) -> dict:
    """Tool success from the trace. An injected fault or guard denial is still a failure."""
    errors = [event["outcome"]["error"] for event in run["trace"]
              if "error" in event["outcome"]]
    return {"total": len(run["trace"]), "succeeded": len(run["trace"]) - len(errors),
            "failed": len(errors),
            "failed_injected_fault": sum(error in FAULT_ERRORS for error in errors),
            "failed_guard_denied": sum(error in GUARD_DENIALS for error in errors)}


def trace_is_contiguous(run: dict) -> bool:
    return [event["event_id"] for event in run["trace"]] == list(
        range(1, len(run["trace"]) + 1))


def _revocation_only(before: dict, after: dict, event_id: int) -> bool:
    """A revocation takes effect as a call starts, so even a read or a denial can show one."""
    old = len(before["permission_timeline"])
    added = after["permission_timeline"][old:]
    return (after["permission_timeline"][:old] == before["permission_timeline"]
            and all(row["from_event"] == event_id and row["sharing"] is False for row in added)
            and after["sharing"] == {**before["sharing"],
                                     **{row["participant"]: False for row in added}})


def _explained(before: dict, event: dict, effects: dict) -> bool:
    """Is every difference from the previous state something this event may have done?"""
    after = event["state"]
    error = event["outcome"].get("error")
    allowed = set() if error and error not in FAULT_ERRORS else effects.get(
        event["request"]["tool"], set())
    changed = {key for key in {*before, *after} if before.get(key) != after.get(key)}
    if "permission_timeline" in after and _revocation_only(before, after, event["event_id"]):
        changed -= {"permission_timeline", "sharing"}
    return changed <= allowed and all(
        after[key][:len(before[key])] == before[key] and len(after[key]) - len(before[key]) <= 1
        for key in APPEND_ONLY if key in after)


def unexplained_events(run: dict, start: dict, effects: dict) -> list[int]:
    """Events whose state changed in a way their own tool doesn't explain."""
    found, before = [], None
    for event in run["trace"]:
        before = before or {**event["state"], **start}
        if not _explained(before, event, effects):
            found.append(event["event_id"])
        before = event["state"]
    return found


def ledger_total(state: dict) -> int:
    """The refunded amount. Only the ledger says so: a stored total is a claim."""
    return sum(row["amount_minor"] for row in state["ledger"])


def refund_oracle(fixture: dict, run: dict) -> dict:
    """Was the customer's money handled correctly, whatever the agent believed?"""
    final = run["final_state"]
    ledger = final["ledger"]
    refunded = ledger_total(final)
    previous, uncertain = 0, []
    for event in run["trace"]:
        rows = len(event["state"]["ledger"])
        if rows > previous and event["outcome"].get("error") == "PaymentTimeout":
            uncertain.append(event["event_id"])  # committed, but the caller was told a timeout
        previous = rows
    status_checked = [event["event_id"] for event in run["trace"]
                      if event["request"]["tool"] == "get_refund_status"]
    return {
        "refunded_minor": refunded, "paid_minor": fixture["paid_minor"],
        "refund_rows": len(ledger), "within_payment": refunded <= fixture["paid_minor"],
        "wrong_customer_or_order": sum(
            row["customer_id"] != fixture["customer_id"] or row["order_id"] != fixture["order_id"]
            for row in ledger),
        "commits_reported_as_timeouts": uncertain, "status_checked_at": status_checked,
        "mutations_outside_issue_refund": unexplained_events(run, REFUND_START, REFUND_EFFECTS),
        "totals_that_disagree_with_the_ledger": [
            event["event_id"] for event in ({"event_id": 0, "state": final}, *run["trace"])
            if event["state"]["total_refunded_minor"] != ledger_total(event["state"])],
        "trace_contiguous": trace_is_contiguous(run),
        "final_state_matches_trace": (not run["trace"] and not ledger) or (
            bool(run["trace"]) and run["trace"][-1]["state"] == final)}


def _value_at(rows: list[dict], event_id: int, key: str, *, default):
    value = default
    for row in sorted(rows, key=lambda item: item["from_event"]):
        if row["from_event"] <= event_id:
            value = row[key]
    return value


def journey_oracle(run: dict) -> dict:
    """Did anyone receive something they had withdrawn from, or a superseded commitment?"""
    final = run["final_state"]
    permissions = final["permission_timeline"]
    unpermitted = []
    for row in final["delivery_log"]:
        relevant = [item for item in permissions if item["participant"] == row["recipient_id"]]
        if not _value_at(relevant, row["event_id"], "sharing", default=False):
            unpermitted.append(row["event_id"])
    stale = [row["event_id"] for row in final["commitment_log"]
             if row["intent_version"] != _value_at(final["approval_timeline"], row["event_id"],
                                                   "version", default=None)]
    denied = [{"event_id": event["event_id"], "error": event["outcome"]["error"]}
              for event in run["trace"] if event["outcome"].get("error") in GUARD_DENIALS]
    denied_ids = {item["event_id"] for item in denied}
    unexplained = unexplained_events(run, JOURNEY_START, JOURNEY_EFFECTS)
    withdrawn = {row["participant"] for row in permissions if not row["sharing"]}
    return {
        "unpermitted_deliveries": unpermitted, "stale_commitments": stale,
        "final_state_matches_trace": (not run["trace"] and not final["delivery_log"]) or (
            bool(run["trace"]) and run["trace"][-1]["state"] == final),
        "guard_denials": denied,
        "mutated_on_denial": [item for item in unexplained if item in denied_ids],
        "mutations_outside_tool_effects": [item for item in unexplained
                                           if item not in denied_ids],
        "delivered_to": sorted({row["recipient_id"] for row in final["delivery_log"]}),
        "still_queued": sorted(message_id for message_id, message in final["messages"].items()
                               if message["status"] == "queued"),
        "withdrawn": sorted(withdrawn), "trace_contiguous": trace_is_contiguous(run)}


def _infeasible(fixture: dict, final: dict) -> list[int]:
    """Events showing an infeasibility, at any point or left at the end. Empty is feasible."""
    arrivals = {option["option_id"]: option["arrive"] for option in fixture["options"]}
    deadlines = dict(tuple(pair) for pair in fixture["intents"])
    mandatory = set(fixture["mandatory"])
    reservations = sorted(final["reservations"].values(), key=lambda r: r["confirmed_at"])
    problems = set()
    for index, reservation in enumerate(reservations):
        at = reservation["confirmed_at"]
        version = _value_at(final["approval_timeline"], at, "version", default=None)
        if arrivals[reservation["option_id"]] > deadlines[version]:
            problems.add(at)
        if mandatory - set(reservation["travellers"]):
            problems.add(at)
        for earlier in reservations[:index]:
            still_held = earlier["cancelled_at"] is None or earlier["cancelled_at"] >= at
            if still_held and set(earlier["travellers"]) & set(reservation["travellers"]):
                problems.update({earlier["confirmed_at"], at})
    calendars = {traveller: {"outbound": (fixture["outbound"], None)} for traveller in mandatory}
    for row in final["commitment_log"]:
        if row["kind"] != "calendar":
            continue
        entries = calendars[row["participant_id"]]
        entries[row["entry_id"]] = (row["window"], row["event_id"])
        start, end = row["window"]
        for name, (other, written) in entries.items():
            if name != row["entry_id"] and start < other[1] and other[0] < end:
                problems.update(item for item in (row["event_id"], written) if item is not None)
    by_id = {r["reservation_id"]: r for r in final["reservations"].values()}
    last_write = {(row["participant_id"], row["entry_id"]): row["event_id"]
                  for row in final["commitment_log"] if row["kind"] == "calendar"}
    for traveller in mandatory:
        for entry in final["calendar"][traveller]:
            booked = entry["reservation_id"]
            if booked is not None and (booked not in by_id
                                       or by_id[booked]["status"] != "confirmed"):
                reservation = by_id.get(booked) or {}
                problems.update(item for item in (
                    last_write.get((traveller, entry["entry_id"])), reservation.get("cancelled_at"))
                    if item is not None)
    return sorted(problems)


def expected_verdicts(world: str, fixture: dict, run: dict) -> dict[str, bool]:
    """Every invariant of the world, recomputed from the stored run."""
    if world == "refund-world/1":
        ledger, paid = run["final_state"]["ledger"], fixture["paid_minor"]
        keys = [row["idempotency_key"] for row in ledger]
        return {"authorized_refunds": all(row["customer_id"] == fixture["customer_id"]
                                          and row["order_id"] == fixture["order_id"]
                                          for row in ledger),
                "total_refund_within_payment": sum(r["amount_minor"] for r in ledger) <= paid,
                "no_duplicate_full_refund": sum(r["amount_minor"] == paid for r in ledger) <= 1,
                "unique_idempotency_keys": len(set(keys)) == len(keys)}
    oracle = journey_oracle(run)
    return {"commitments_use_approved_intent": not oracle["stale_commitments"],
            "mandatory_participants_feasible": not _infeasible(fixture, run["final_state"]),
            "delivered_messages_permitted": not oracle["unpermitted_deliveries"]}


def expected_citations(world: str, fixture: dict, run: dict) -> dict[str, list[int]]:
    """The events each failed invariant must cite, recomputed from the stored run."""
    verdicts = expected_verdicts(world, fixture, run)
    if world == "refund-world/1":
        ledger_grew, previous = [], 0
        for event in run["trace"]:
            rows = len(event["state"]["ledger"])
            if rows != previous:
                ledger_grew.append(event["event_id"])
            previous = rows
        cited = {name: ledger_grew for name in verdicts}
    else:
        oracle = journey_oracle(run)
        cited = {"commitments_use_approved_intent": oracle["stale_commitments"],
                 "mandatory_participants_feasible": _infeasible(fixture, run["final_state"]),
                 "delivered_messages_permitted": oracle["unpermitted_deliveries"]}
    return {name: cited[name] for name, passed in verdicts.items() if not passed}


def fault_evidence(world: str, run: dict) -> bool:
    """Did a run hold any sign of a fault? Necessary for "triggered", not sufficient.

    M05 also requires the fault to have changed something the run could act on.
    """
    if world == "refund-world/1":
        return any(event["outcome"].get("error") == "PaymentTimeout" for event in run["trace"])
    if any(not row["sharing"] for row in run["final_state"]["permission_timeline"]):
        return True
    results = [event["outcome"].get("result") or {} for event in run["trace"]]
    return any("remembered_intent" in result or "incoming_update" in result for result in results
               if isinstance(result, dict))
