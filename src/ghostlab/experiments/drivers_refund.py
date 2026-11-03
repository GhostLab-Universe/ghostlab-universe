"""Refund environment driver: refund logic behind the driver surface.

This module owns the refund agent procedures (identical tool sequences and
idempotency keys to ``ghostlab.core``), the refund evidence shape, the
``timeout_after_commit`` fault reading and the ``core.verify`` mapping. The
controller dispatches here for refund environments; the legacy
``drive_agent``/``fault_fired``/``evidence_complete`` names in
``controller`` stay as thin delegates over these functions.

Importing this module registers the driver (for ``refund-env``) and the four
refund checks in the code-only check registry.
"""

import sys

from ..core import Fixture, PaymentTimeout
from ..core import verify as core_verify
from . import drivers as _drivers
from .checks import CheckError, get_check, register_check
from .configs import ConfigError

ENV_ID = "refund-env"
CHECK_VERSION = "1"

CHECK_IDS = ("authorized_refunds", "total_refund_within_payment",
             "no_duplicate_full_refund", "unique_idempotency_keys")

_DESCRIPTIONS = {
    "authorized_refunds": "every ledger row belongs to the fixture"
                          " order and customer",
    "total_refund_within_payment": "total refunded does not exceed"
                                   " the paid amount",
    "no_duplicate_full_refund": "at most one full-amount refund row",
    "unique_idempotency_keys": "ledger idempotency keys are unique",
}


def drive(agent, invoke, fixture) -> None:
    """Refund agent procedure; identical sequence to ``core.run_agent``.

    ``invoke`` is any ``(tool, arguments) -> result`` dispatcher, so the same
    procedure drives a world or an environment adapter without divergence.
    """
    if agent not in ("vulnerable", "repaired"):
        raise ConfigError(f"unsupported agent {agent!r}")
    identity = {"order_id": fixture["order_id"],
                "customer_id": fixture["customer_id"]}
    order = invoke("get_order", dict(identity))
    request = {**identity, "amount_minor": order["paid_minor"],
               "idempotency_key": "refund-intent-1"}
    try:
        invoke("issue_refund", request)
    except PaymentTimeout:
        if agent == "vulnerable":
            invoke("issue_refund", {**request,
                                    "idempotency_key": "refund-retry-2"})
        else:
            status = invoke("get_refund_status", {
                **identity, "idempotency_key": request["idempotency_key"]})
            if status["status"] != "committed":
                invoke("issue_refund", request)


def evidence_complete(evidence, dispatches: int) -> bool:
    """Check refund evidence is ordered, owned and consistent."""
    if type(evidence) is not dict or set(evidence) != {"snapshot", "trace"}:
        return False
    trace = evidence["trace"]
    snapshot = evidence["snapshot"]
    if type(trace) is not list or len(trace) != dispatches:
        return False
    if [e.get("event_id") for e in trace
            if isinstance(e, dict)] != list(range(1, len(trace) + 1)):
        return False
    if type(snapshot) is not dict or set(snapshot) != {
            "order", "ledger", "total_refunded_minor"}:
        return False
    ledger = snapshot["ledger"]
    if type(ledger) is not list:
        return False
    try:
        total = sum(row["amount_minor"] for row in ledger)
    except (KeyError, TypeError):
        return False
    return snapshot["total_refunded_minor"] == total


def fault_fired(hook: str, trace) -> bool:
    """Whether recorded evidence shows a fault hook fired.

    ``timeout_after_commit`` commits then reports a timeout, so it fired if
    and only if the trace carries a ``PaymentTimeout`` outcome. Unknown hooks
    never count as fired: they cannot reach execution (resolution rejects
    undiscovered hooks), so treating them as fired would hide a gap.
    """
    if type(trace) is not list:
        return False
    if hook == "timeout_after_commit":
        for event in trace:
            if not isinstance(event, dict):
                continue
            outcome = event.get("outcome")
            if isinstance(outcome, dict) \
                    and outcome.get("error") == "PaymentTimeout":
                return True
        return False
    return False


def verify_checks(fixture, snapshot) -> dict:
    """Compute refund rule booleans via ``core.verify`` (same outcomes)."""
    return core_verify(Fixture(**fixture), snapshot)


def declared_checks() -> list:
    """Check ids this environment supports (for applicability)."""
    return list(CHECK_IDS)


def agents() -> list:
    """Agent names this driver implements."""
    return ["vulnerable", "repaired"]


def required_tools(agent) -> list:
    """Tool names the named agent procedure needs (for resolution)."""
    if agent == "vulnerable":
        return ["get_order", "issue_refund"]
    return ["get_order", "issue_refund", "get_refund_status"]


def _make_interpret(check_id):
    def interpret(*, snapshot, trace, params, fixture):
        del trace, params
        try:
            if isinstance(fixture, Fixture):
                parsed = fixture
            else:
                parsed = Fixture(**fixture)
        except (TypeError, ValueError, KeyError, AttributeError):
            return {"verdict": "unknown",
                    "detail": f"{check_id}: fixture unavailable",
                    "evidence_refs": []}
        try:
            passed = core_verify(parsed, snapshot)[check_id]
        except (KeyError, TypeError, AttributeError, ValueError):
            return {"verdict": "unknown",
                    "detail": f"{check_id}: snapshot evidence incomplete",
                    "evidence_refs": []}
        if passed is True:
            return {"verdict": "pass",
                    "detail": f"{check_id} holds",
                    "evidence_refs": ["snapshot:ledger"]}
        return {"verdict": "violation",
                "detail": f"{check_id} violated",
                "evidence_refs": ["snapshot:ledger"]}
    return interpret


def _interpret_unique_keys(*, snapshot, trace, params, fixture):
    """Shared idempotency-uniqueness check; dispatches on snapshot shape.

    ``unique_idempotency_keys`` is the same concept in every env, so both
    drivers share this one registration (same logic in the booking driver;
    whichever driver loads first wins, with identical outcomes). Refund
    snapshots carry ``ledger``, booking snapshots carry ``reservations``.
    """
    del trace, params, fixture
    rows = None
    ref = "snapshot"
    if isinstance(snapshot, dict):
        if isinstance(snapshot.get("ledger"), list):
            rows = snapshot["ledger"]
            ref = "snapshot:ledger"
        elif isinstance(snapshot.get("reservations"), list):
            rows = snapshot["reservations"]
            ref = "snapshot:reservations"
    if rows is None:
        return {"verdict": "unknown",
                "detail": "unique_idempotency_keys:"
                          " snapshot evidence incomplete",
                "evidence_refs": []}
    try:
        keys = [row["idempotency_key"] for row in rows]
    except (KeyError, TypeError):
        return {"verdict": "unknown",
                "detail": "unique_idempotency_keys:"
                          " snapshot evidence incomplete",
                "evidence_refs": []}
    if len(set(keys)) == len(keys):
        return {"verdict": "pass",
                "detail": "unique_idempotency_keys holds",
                "evidence_refs": [ref]}
    return {"verdict": "violation",
            "detail": "unique_idempotency_keys violated",
            "evidence_refs": [ref]}


for _check_id in CHECK_IDS:
    _interpret = _interpret_unique_keys \
        if _check_id == "unique_idempotency_keys" \
        else _make_interpret(_check_id)
    try:
        get_check(_check_id)
    except CheckError:
        register_check(
            check_id=_check_id, version=CHECK_VERSION,
            description=_DESCRIPTIONS[_check_id], params_spec={},
            required_evidence=["snapshot"], applies_to=ENV_ID,
            temporal="stateless", interpret=_interpret)
del _check_id, _interpret

_drivers.register_driver(ENV_ID, sys.modules[__name__])
