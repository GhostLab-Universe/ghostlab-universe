"""Equipment-booking environment driver (external example).

Booking domain logic behind the shared driver surface: naive/corrected agent
procedures via the controller's ``invoke`` dispatcher with the SAME tool
steps, keys and retry shape as ``agents.run_naive``/``run_corrected``; the
booking evidence-shape check; the ``timeout_after_commit`` fault reading
(``BookingTimeout`` outcome); and the ``checks.verify_equipment`` mapping.

Relative imports ONLY: this module never imports ``ghostlab`` core (core
never imports examples either). Registration is therefore injected: the
caller imports this module, registers it with
``ghostlab.experiments.drivers.register_driver(ENV_ID, module)``, and
registers the three booking checks via ``register_checks(register_check)``.
"""

from .checks import verify_equipment
from .world import BookingTimeout, EquipmentFixture

ENV_ID = "booking-env"
CHECK_VERSION = "1"

CHECK_IDS = ("authorized_bookings", "no_duplicate_booking",
             "unique_idempotency_keys")

_DESCRIPTIONS = {
    "authorized_bookings": "every reservation belongs to the fixture"
                           " equipment and user",
    "no_duplicate_booking": "at most one reservation for the fixture slot",
    "unique_idempotency_keys": "reservation idempotency keys are unique",
}


def drive(agent, invoke, fixture) -> None:
    """Booking agent procedure; same steps/keys as ``agents.py``.

    ``vulnerable`` retries a timed-out booking with a FRESH key
    (``booking-retry-2``); ``repaired`` re-checks status with the STABLE key
    before retrying.
    """
    if agent not in ("vulnerable", "repaired"):
        raise ValueError(f"unsupported agent {agent!r} for {ENV_ID}")
    identity = {"equipment_id": fixture["equipment_id"],
                "user_id": fixture["user_id"]}
    invoke("get_equipment", dict(identity))
    request = {**identity, "slot": fixture["slot"],
               "idempotency_key": "booking-intent-1"}
    try:
        invoke("book_equipment", dict(request))
    except BookingTimeout:
        if agent == "vulnerable":
            invoke("book_equipment", {
                **request, "idempotency_key": "booking-retry-2"})
        else:
            status = invoke("get_booking_status", {
                **identity, "idempotency_key": request["idempotency_key"]})
            if status["status"] != "committed":
                invoke("book_equipment", dict(request))


def evidence_complete(evidence, dispatches: int) -> bool:
    """Check booking evidence is ordered, owned and consistent."""
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
            "equipment", "reservations", "total_bookings"}:
        return False
    reservations = snapshot["reservations"]
    if type(reservations) is not list:
        return False
    return snapshot["total_bookings"] == len(reservations)


def fault_fired(hook: str, trace) -> bool:
    """Whether recorded evidence shows a fault hook fired.

    ``timeout_after_commit`` commits then reports a timeout, so it fired if
    and only if the trace carries a ``BookingTimeout`` outcome. Unknown hooks
    never count as fired.
    """
    if type(trace) is not list:
        return False
    if hook == "timeout_after_commit":
        for event in trace:
            if not isinstance(event, dict):
                continue
            outcome = event.get("outcome")
            if isinstance(outcome, dict) \
                    and outcome.get("error") == "BookingTimeout":
                return True
        return False
    return False


def verify_checks(fixture, snapshot) -> dict:
    """Compute booking rule booleans via ``checks.verify_equipment``."""
    return verify_equipment(EquipmentFixture(**fixture), snapshot)


def declared_checks() -> list:
    """Check ids this environment supports (for applicability)."""
    return list(CHECK_IDS)


def agents() -> list:
    """Agent names this driver implements."""
    return ["vulnerable", "repaired"]


_BOOKING_FIXTURE_KEYS = {"equipment_id", "user_id", "slot"}


def validate_fixture(fixture) -> dict:
    """Validate a booking fixture (raises ValueError when invalid)."""
    if type(fixture) is not dict or set(fixture) != _BOOKING_FIXTURE_KEYS:
        raise ValueError("fixture must carry exactly equipment_id,"
                         " user_id and slot")
    parsed = EquipmentFixture(**fixture)
    return {"equipment_id": parsed.equipment_id,
            "user_id": parsed.user_id, "slot": parsed.slot}


def required_tools(agent) -> list:
    """Tool names the named agent procedure needs (for resolution)."""
    if agent == "vulnerable":
        return ["get_equipment", "book_equipment"]
    return ["get_equipment", "book_equipment", "get_booking_status"]


def _make_interpret(check_id):
    def interpret(*, snapshot, trace, params, fixture):
        del trace, params
        try:
            if isinstance(fixture, EquipmentFixture):
                parsed = fixture
            else:
                parsed = EquipmentFixture(**fixture)
        except (TypeError, ValueError, KeyError, AttributeError):
            return {"verdict": "unknown",
                    "detail": f"{check_id}: fixture unavailable",
                    "evidence_refs": []}
        try:
            passed = verify_equipment(parsed, snapshot)[check_id]
        except (KeyError, TypeError, AttributeError, ValueError):
            return {"verdict": "unknown",
                    "detail": f"{check_id}: snapshot evidence incomplete",
                    "evidence_refs": []}
        if passed is True:
            return {"verdict": "pass",
                    "detail": f"{check_id} holds",
                    "evidence_refs": ["snapshot:reservations"]}
        return {"verdict": "violation",
                "detail": f"{check_id} violated",
                "evidence_refs": ["snapshot:reservations"]}
    return interpret


def _interpret_unique_keys(*, snapshot, trace, params, fixture):
    """Shared idempotency-uniqueness check; dispatches on snapshot shape.

    Same concept and same logic as the refund driver's registration:
    whichever driver loads first wins, with identical outcomes. Booking
    snapshots carry ``reservations``, refund snapshots carry ``ledger``.
    """
    del trace, params, fixture
    rows = None
    ref = "snapshot"
    if isinstance(snapshot, dict):
        if isinstance(snapshot.get("reservations"), list):
            rows = snapshot["reservations"]
            ref = "snapshot:reservations"
        elif isinstance(snapshot.get("ledger"), list):
            rows = snapshot["ledger"]
            ref = "snapshot:ledger"
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


BOOKING_CHECK_SPECS = tuple(
    {"check_id": check_id, "version": CHECK_VERSION,
     "description": _DESCRIPTIONS[check_id], "params_spec": {},
     "required_evidence": ["snapshot"], "applies_to": ENV_ID,
     "temporal": "stateless",
     "interpret": _interpret_unique_keys
     if check_id == "unique_idempotency_keys"
     else _make_interpret(check_id)}
    for check_id in CHECK_IDS)


def register_checks(register_check) -> list:
    """Register the three booking checks via an injected registrar.

    The registrar is injected (normally
    ``ghostlab.experiments.checks.register_check``) so this module keeps
    relative-only imports while core still never imports examples.
    ``unique_idempotency_keys`` is shared with the refund driver: first
    registration wins (both interprets are identical), so an
    already-registered id is skipped here instead of failing. Any other
    registration error still raises.
    """
    registered = []
    for spec in BOOKING_CHECK_SPECS:
        try:
            register_check(**spec)
        except ValueError as error:
            if "already registered" not in str(error):
                raise
            continue
        registered.append(spec["check_id"])
    return registered
