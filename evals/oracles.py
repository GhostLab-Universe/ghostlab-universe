"""Independent oracles: requirements restated from the product, computed from stored runs.

These share no code with the M02 verifiers, the M06 guard or the M05 findings. They read
a run's JSON and answer the question a reviewer would ask, so a bug in the code under
test can't also hide in its own check. Agreement with the system's checks is measured
separately.
"""

GUARD_DENIALS = {"StaleIntentDenied", "PermissionWithdrawnDenied"}
FAULT_ERRORS = {"PaymentTimeout"}


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


def refund_oracle(fixture: dict, run: dict) -> dict:
    """Was the customer's money handled correctly, whatever the agent believed?"""
    ledger = run["final_state"]["ledger"]
    refunded = sum(row["amount_minor"] for row in ledger)
    previous, uncertain, unexplained = 0, [], []
    for event in run["trace"]:
        rows = len(event["state"]["ledger"])
        tool = event["request"]["tool"]
        if rows > previous:
            if tool != "issue_refund":
                unexplained.append(event["event_id"])
            if event["outcome"].get("error") == "PaymentTimeout":
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
        "mutations_outside_issue_refund": unexplained,
        "trace_contiguous": trace_is_contiguous(run),
        "final_state_matches_trace": (not run["trace"] and not ledger) or (
            bool(run["trace"]) and run["trace"][-1]["state"]["ledger"] == ledger)}


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
    denied, mutated_on_denial = [], []
    for event in run["trace"]:
        error = event["outcome"].get("error")
        if error in GUARD_DENIALS:
            denied.append({"event_id": event["event_id"], "error": error})
            before = run["trace"][event["event_id"] - 2]["state"] if event["event_id"] > 1 else None
            if before is not None and (
                    len(before["delivery_log"]) != len(event["state"]["delivery_log"])
                    or len(before["commitment_log"]) != len(event["state"]["commitment_log"])):
                mutated_on_denial.append(event["event_id"])
    withdrawn = {row["participant"] for row in permissions if not row["sharing"]}
    return {
        "unpermitted_deliveries": unpermitted, "stale_commitments": stale,
        "guard_denials": denied, "mutated_on_denial": mutated_on_denial,
        "delivered_to": sorted({row["recipient_id"] for row in final["delivery_log"]}),
        "still_queued": sorted(message_id for message_id, message in final["messages"].items()
                               if message["status"] == "queued"),
        "withdrawn": sorted(withdrawn), "trace_contiguous": trace_is_contiguous(run)}


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


def agrees_with_controller(world: str, oracle: dict, findings: list[dict]) -> bool:
    """The controller's findings for one run must say what the oracle measured."""
    failed = {finding["invariant"] for finding in findings if finding["verdict"] == "failed"}
    if world == "refund-world/1":
        return ("total_refund_within_payment" in failed) == (not oracle["within_payment"])
    return (("delivered_messages_permitted" in failed) == bool(oracle["unpermitted_deliveries"])
            and ("commitments_use_approved_intent" in failed) == bool(oracle["stale_commitments"]))
