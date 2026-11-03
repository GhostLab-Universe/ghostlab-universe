"""Environment driver registry: domain logic behind one fixed surface.

A driver is a module object exposing exactly::

    drive(agent, invoke, fixture) -> None
    evidence_complete(evidence, dispatches) -> bool
    fault_fired(hook, trace) -> bool
    verify_checks(fixture, snapshot) -> {rule: bool}
    declared_checks() -> [check_id, ...]
    agents() -> ["vulnerable", "repaired", ...]
    required_tools(agent) -> [tool, ...]

``drive`` runs the agent procedure through the controller's ``invoke``
dispatcher; ``evidence_complete`` validates the adapter's evidence shape;
``fault_fired`` reads fault activation from the trace; ``verify_checks``
computes rule booleans from authoritative state; ``declared_checks`` lists
the check ids the environment supports (resolution rejects anything else);
``agents`` lists the agent names the driver implements;
``required_tools`` lists the tool names the named agent procedure needs
(resolution rejects environments that lack them).

Optional hook (used when present, legacy default otherwise):

    validate_fixture(fixture) -> dict

Drivers for non-refund environments MUST implement ``validate_fixture``;
when absent, the refund fixture shape applies (correct for ``refund-env``;
a non-refund driver that forgets it fails loudly because its fixtures do
not match refund keys). Driverless environments cannot execute at all.

Drivers are registered by trusted checked-in code only, keyed by env id, and
are never redefined. Core registers the refund driver; example drivers (such
as the equipment-booking driver) register themselves from outside core.
"""

import re

ENV_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REQUIRED = ("drive", "evidence_complete", "fault_fired", "verify_checks",
             "declared_checks", "agents", "required_tools")

_registry: dict = {}


def register_driver(env_id: str, driver) -> None:
    """Register one driver module for an env id (never redefined)."""
    if type(env_id) is not str or not ENV_ID_PATTERN.match(env_id):
        raise ValueError(f"invalid env_id {env_id!r}")
    missing = [name for name in _REQUIRED
               if not callable(getattr(driver, name, None))]
    if missing:
        raise ValueError(f"driver for env {env_id!r} misses:"
                         f" {', '.join(missing)}")
    if env_id in _registry:
        raise ValueError(f"driver for env {env_id!r} is already"
                         " registered; drivers are never redefined")
    _registry[env_id] = driver


def get_driver(env_id: str):
    """Return the driver module for an env id, or None if unregistered."""
    return _registry.get(env_id)
