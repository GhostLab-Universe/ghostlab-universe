"""Failure Capsule v2 for ``service-relay/1``: data-only export and recorded-action replay.

The envelope and the checks on what replay can't reproduce are the same as the
journey world's, so the shared rules come from ``journey_capsule``. The payload
records the fixture (including whether the guarded boundary was on), the principal,
the scenario and the run. Replay re-executes recorded tool requests in a fresh
world; it never imports adapter code and never calls a model.
"""

import json
from pathlib import Path

from ghostlab.execution.relay_guard import make_world

from .journey_capsule import (MAX_CAPSULE_BYTES, PAYLOAD_FIELDS, RUN_FIELDS, SCHEMA_VERSION,
                              _check_event, _check_metadata, _text, capsule_digest)
from .relay import WORLD_VERSION, RelayFixture, RelayScenario, ToolError
from .relay_run import ADAPTER_VERSION, ADAPTERS
from .relay_verify import INVARIANTS, metrics, verify


def export_capsule(path: Path, fixture: RelayFixture, scenario: dict, recorded_run: dict,
                   *, source: str = "trusted-synthetic-relay-adapter"):
    RelayScenario.from_json(scenario)  # only a scenario this world can replay
    payload = {"schema_version": SCHEMA_VERSION, "world_version": WORLD_VERSION,
               "fixture": fixture.to_json(), "scenario": scenario,
               "actor_id": recorded_run["actor_id"], "source": source, "seed": 0,
               "recorded_model_outputs": [], "run": recorded_run}
    text = json.dumps({"payload": payload, "sha256": capsule_digest(payload)}, indent=2)
    if len(text.encode()) > MAX_CAPSULE_BYTES:
        raise ValueError("Capsule too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def load_capsule(path: Path) -> dict:
    if path.stat().st_size > MAX_CAPSULE_BYTES:
        raise ValueError("Capsule too large")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if type(envelope) is not dict or set(envelope) != {"payload", "sha256"}:
        raise ValueError("Invalid capsule envelope")
    payload = envelope["payload"]
    if type(payload) is not dict or set(payload) != PAYLOAD_FIELDS:
        raise ValueError("Invalid capsule payload")
    if capsule_digest(payload) != envelope["sha256"]:
        raise ValueError("Capsule checksum mismatch")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported capsule schema")
    if payload["world_version"] != WORLD_VERSION:
        raise ValueError("Unsupported world version")
    return payload


def _check_run(recorded, actor_id, scenario):
    if type(recorded) is not dict or set(recorded) != RUN_FIELDS:
        raise ValueError("Invalid recorded run")
    if (type(recorded["schema_version"]) is not int or recorded["schema_version"] != 1
            or recorded["world_version"] != WORLD_VERSION
            or type(recorded["adapter_id"]) is not str or recorded["adapter_id"] not in ADAPTERS
            or recorded["adapter_version"] != ADAPTER_VERSION):
        raise ValueError("Recorded run is not from this world's adapters")
    if recorded["actor_id"] != actor_id:
        raise ValueError("Recorded principal does not match capsule")
    checks = recorded["checks"]
    if (type(checks) is not dict or set(checks) != set(INVARIANTS)
            or any(type(v) is not bool for v in checks.values())):
        raise ValueError("checks must be exactly the pack's invariants, as booleans")
    if type(recorded["final_state"]) is not dict:
        raise ValueError("Invalid final state")
    trace = recorded["trace"]
    if type(trace) is not list or len(trace) > scenario.max_tool_calls:
        raise ValueError("Invalid trace length")
    for index, event in enumerate(trace):
        _check_event(index, event)
    if type(recorded["status"]) is not str or recorded["status"] not in {"completed",
                                                                          "budget_exhausted"}:
        raise ValueError("Invalid run status")
    # Stricter than the contract: the budget can only end a run that used all of it.
    if recorded["status"] == "budget_exhausted" and len(trace) != scenario.max_tool_calls:
        raise ValueError("budget_exhausted needs a trace that used the whole budget")
    return trace


def replay(path: Path) -> dict:
    """Re-execute recorded tool requests; never import adapter code or call a model.

    A matching checksum is accidental-change detection, not proof of authorship.
    A replay mismatch means the capsule's claimed evidence was not reproduced.
    """
    payload = load_capsule(path)
    _check_metadata(payload)
    actor_id = payload["actor_id"]
    if not _text(actor_id, 100):  # None would otherwise mean "the fixture's operator"
        raise ValueError("Invalid capsule principal")
    fixture = RelayFixture.from_json(payload["fixture"])
    scenario = RelayScenario.from_json(payload["scenario"])
    recorded = payload["run"]
    trace = _check_run(recorded, actor_id, scenario)
    world = make_world(fixture, scenario, actor_id=actor_id)
    for event in trace:
        try:
            world.call(event["request"]["tool"], event["request"]["arguments"])
        except ToolError:
            pass  # Recreated errors are compared to the original trace below.
    state = world.snapshot()
    checks = verify(fixture, state)
    matches = {"trace": world.trace == trace, "final_state": state == recorded["final_state"],
               "checks": checks == recorded["checks"]}
    return {"reproduced": all(matches.values()), "matches": matches, "checks": checks,
            "metrics": metrics(fixture, state), "guarded": fixture.guarded, "model_calls": 0}
