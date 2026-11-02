"""Measured fidelity reports for extension environments.

:func:`assess_fidelity` measures what it can (repeat determinism,
recorded-action replay, snapshot round-trips, reference-trace diffs) and
labels everything else as caller-declared. Declared approximations are
echoed, never verified; missing evidence is reported, never a pass.

This complements :mod:`ghostlab.extensions.qualify` (a conformance suite
with pass/fail checks): a qualification report may be attached and is
echoed verbatim under ``qualification``.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ghostlab.extensions.environment import (EXTENSION_INTERFACE_VERSION, Action,
                                             ActionBudgetExceeded, UnsupportedOperation)
from ghostlab.extensions.registry import check_environment
from ghostlab.extensions.runner import first_divergence, replay_trace

FIDELITY_SCHEMA = "ghostlab-fidelity/1"
FIDELITY_VERSION = "ghostlab-fidelity/1"


@dataclass(frozen=True)
class FidelityReport:
    """Measured behavior plus declared approximations, kept separate."""

    schema: str
    world_version: str
    environment_id: str
    environment_version: str
    fidelity_version: str
    interface_version: str
    passed: bool
    determinism: dict
    reference: dict
    replay: dict
    snapshot: dict
    capabilities: dict
    missing_capabilities: tuple
    declared_tested: tuple
    declared_approximations: tuple
    declared_omissions: tuple
    qualification: dict | None
    notes: tuple

    def __post_init__(self):
        if self.schema != FIDELITY_SCHEMA:
            raise ValueError("Invalid fidelity schema")
        for value, label in ((self.world_version, "world version"),
                             (self.environment_id, "environment id"),
                             (self.environment_version, "environment version"),
                             (self.fidelity_version, "fidelity version"),
                             (self.interface_version, "interface version")):
            if type(value) is not str or not value:
                raise ValueError(f"Invalid {label}")
        if type(self.passed) is not bool:
            raise ValueError("Invalid fidelity verdict")
        if (type(self.determinism) is not dict
                or set(self.determinism) != {"repeats", "identical", "state_equal",
                                             "trace_divergence", "budget_stops"}):
            raise ValueError("Invalid fidelity determinism")
        if (type(self.reference) is not dict
                or set(self.reference) != {"compared_trace", "compared_state",
                                           "matched", "trace_divergence",
                                           "state_equal"}):
            raise ValueError("Invalid fidelity reference")
        if (type(self.replay) is not dict
                or set(self.replay) != {"supported", "reproduced"}):
            raise ValueError("Invalid fidelity replay")
        if (type(self.snapshot) is not dict
                or set(self.snapshot) != {"declared", "probed", "supported",
                                          "restored_match"}):
            raise ValueError("Invalid fidelity snapshot")
        if (type(self.capabilities) is not dict
                or any(type(key) is not str or type(val) is not bool
                       for key, val in self.capabilities.items())):
            raise ValueError("Invalid fidelity capabilities")
        for names, label in ((self.missing_capabilities, "missing capabilities"),
                             (self.declared_tested, "declared tested"),
                             (self.declared_approximations, "declared approximations"),
                             (self.declared_omissions, "declared omissions"),
                             (self.notes, "notes")):
            if (not isinstance(names, (list, tuple))
                    or any(type(entry) is not str for entry in names)):
                raise ValueError(f"Invalid {label}")
        if self.qualification is not None and type(self.qualification) is not dict:
            raise ValueError("Invalid fidelity qualification")
        object.__setattr__(self, "missing_capabilities", tuple(self.missing_capabilities))
        object.__setattr__(self, "declared_tested", tuple(self.declared_tested))
        object.__setattr__(self, "declared_approximations",
                           tuple(self.declared_approximations))
        object.__setattr__(self, "declared_omissions", tuple(self.declared_omissions))
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> dict:
        return {"schema": self.schema, "world_version": self.world_version,
                "environment_id": self.environment_id,
                "environment_version": self.environment_version,
                "fidelity_version": self.fidelity_version,
                "interface_version": self.interface_version, "passed": self.passed,
                "determinism": self.determinism, "reference": self.reference,
                "replay": self.replay, "snapshot": self.snapshot,
                "capabilities": self.capabilities,
                "missing_capabilities": list(self.missing_capabilities),
                "declared_tested": list(self.declared_tested),
                "declared_approximations": list(self.declared_approximations),
                "declared_omissions": list(self.declared_omissions),
                "qualification": self.qualification, "notes": list(self.notes)}

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        det = self.determinism
        return (f"{self.world_version} fidelity {verdict} "
                f"(determinism {det['repeats']}x identical={det['identical']}; "
                f"replay reproduced={self.replay['reproduced']}; "
                f"missing: {', '.join(self.missing_capabilities) or 'none'})")


def _run_script(env_factory: Callable, fixture: dict, scenario: dict, actor_id: str,
                script: tuple) -> tuple:
    """One fresh instance through the probe script: trace, state, budget stop."""
    env = env_factory()
    check_environment(env)
    try:
        env.reset(dict(fixture), dict(scenario), actor_id=actor_id)
        stopped = False
        for action in script:
            try:
                env.execute(action)
            except ActionBudgetExceeded:
                stopped = True
                break
        observed = env.observe()
        return list(observed.trace), dict(observed.state), stopped
    finally:
        env.close()


def assess_fidelity(*, env_factory: Callable, fixture: Mapping, scenario: Mapping,
                    actor_id: str, script: Any, repeats: int = 3,
                    expected_trace: Any = None, expected_state: Any = None,
                    declared_tested: Any = (), declared_approximations: Any = (),
                    declared_omissions: Any = (),
                    qualification: Any = None) -> FidelityReport:
    """Measure repeatability, replay and reference agreement of one world.

    ``script`` is 1 to 100 already-validated actions. ``expected_trace``
    and ``expected_state`` come from the developer's reference
    implementation; when absent the reference is simply not compared.
    Declared lists are echoed verbatim: the report never presents a
    declaration as a measurement.
    """
    if not callable(env_factory):
        raise TypeError("assess_fidelity env_factory must be callable")
    if not isinstance(fixture, Mapping) or not isinstance(scenario, Mapping):
        raise ValueError("Invalid fixture or scenario")
    if type(actor_id) is not str or not actor_id:
        raise ValueError("Invalid actor id")
    if (not isinstance(script, (list, tuple)) or not 1 <= len(script) <= 100
            or any(not isinstance(action, Action) for action in script)):
        raise ValueError("script holds 1 to 100 Action entries")
    script = tuple(script)
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("repeats must be an integer from 1 to 10")
    if expected_trace is not None and (
            type(expected_trace) is not list or len(expected_trace) > 1000):
        raise ValueError("Invalid expected trace")
    if expected_state is not None and not isinstance(expected_state, Mapping):
        raise ValueError("Invalid expected state")
    for declared, label in ((declared_tested, "declared tested"),
                            (declared_approximations, "declared approximations"),
                            (declared_omissions, "declared omissions")):
        if (not isinstance(declared, (list, tuple)) or len(declared) > 32
                or any(type(entry) is not str or not entry for entry in declared)):
            raise ValueError(f"Invalid {label}")
    if qualification is not None and type(qualification) is not dict:
        raise ValueError("Invalid qualification attachment")
    probe = env_factory()
    check_environment(probe)
    try:
        description = probe.describe()
    finally:
        probe.close()
    world, env_id = description.world_version, description.environment_id
    env_version, capabilities = description.environment_version, dict(description.capabilities)
    notes: list = []
    runs = [_run_script(env_factory, dict(fixture), dict(scenario), actor_id, script)
            for _ in range(repeats)]
    first_trace, first_state = runs[0][0], runs[0][1]
    budget_stops = sum(1 for run in runs if run[2])
    if budget_stops:
        notes.append(f"probe script hit the world budget on "
                     f"{budget_stops}/{repeats} repeats")
    identical, state_equal, divergence = True, True, None
    for trace, state, _ in runs[1:]:
        if trace != first_trace:
            identical = False
            if divergence is None:
                divergence = first_divergence(first_trace, trace)
        if state != first_state:
            identical, state_equal = False, False
    determinism = {"repeats": repeats, "identical": identical and state_equal,
                   "state_equal": state_equal, "trace_divergence": divergence,
                   "budget_stops": budget_stops}
    compared_trace = expected_trace is not None
    compared_state = expected_state is not None
    trace_match = (first_trace == expected_trace) if compared_trace else True
    state_match = (first_state == dict(expected_state)) if compared_state else True
    reference = {
        "compared_trace": compared_trace, "compared_state": compared_state,
        "matched": trace_match and state_match,
        "trace_divergence": (None if trace_match or not compared_trace
                             else first_divergence(expected_trace, first_trace)),
        "state_equal": state_match if compared_state else True,
    }
    if not compared_trace and not compared_state:
        notes.append("no reference trace or state supplied; reference not compared")
    replay_env = env_factory()
    check_environment(replay_env)
    try:
        replay_env.reset(dict(fixture), dict(scenario), actor_id=actor_id)
        replayed = replay_trace(replay_env, first_trace)
        reproduced = (list(replayed.trace) == first_trace
                      and dict(replayed.state) == first_state)
    finally:
        replay_env.close()
    replay = {"supported": True, "reproduced": reproduced}
    declared_snapshots = capabilities.get("snapshot_restore", False)
    snapshot = {"declared": declared_snapshots, "probed": False, "supported": False,
                "restored_match": None}
    if declared_snapshots:
        snap_env = env_factory()
        check_environment(snap_env)
        try:
            snap_env.reset(dict(fixture), dict(scenario), actor_id=actor_id)
            before = snap_env.observe()
            try:
                snap = snap_env.snapshot()
            except UnsupportedOperation:
                notes.append("snapshots declared but the adapter refused them")
            else:
                snapshot["probed"] = True
                for action in script:
                    try:
                        snap_env.execute(action)
                    except ActionBudgetExceeded:
                        break
                restored = snap_env.restore(snap)
                match = (list(restored.trace) == list(before.trace)
                         and dict(restored.state) == dict(before.state))
                snapshot["supported"] = True
                snapshot["restored_match"] = match
                if not match:
                    notes.append("snapshot restore did not return to the snapshot point")
        finally:
            snap_env.close()
    else:
        notes.append("no mid-run snapshots; replay re-executes recorded actions")
    missing = tuple(sorted(name for name, have in capabilities.items() if have is False))
    snapshot_ok = (not declared_snapshots
                   or (snapshot["supported"] and snapshot["restored_match"] is True))
    passed = bool(determinism["identical"] and reproduced and reference["matched"]
                  and snapshot_ok)
    return FidelityReport(
        schema=FIDELITY_SCHEMA, world_version=world, environment_id=env_id,
        environment_version=env_version, fidelity_version=FIDELITY_VERSION,
        interface_version=EXTENSION_INTERFACE_VERSION, passed=passed,
        determinism=determinism, reference=reference, replay=replay, snapshot=snapshot,
        capabilities=capabilities, missing_capabilities=missing,
        declared_tested=tuple(declared_tested),
        declared_approximations=tuple(declared_approximations),
        declared_omissions=tuple(declared_omissions),
        qualification=None if qualification is None else dict(qualification),
        notes=tuple(notes))
