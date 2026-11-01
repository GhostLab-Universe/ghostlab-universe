"""Failure Capsule v2 for ``group-journey/1``: data-only export and recorded-action replay.

The envelope is ``{payload, sha256}`` with the same checksum algorithm as v1
(``ghostlab.capsule``) and the ``capsule_v2`` contract. The payload records the
fixture (including whether the guarded boundary was on), the principal, the
scenario and the run. Replay re-executes recorded tool requests in a fresh
world, then runs the named checked-in adapter to check them. A capsule supplies no code,
and no model is called.
"""

import hashlib
import json
import re
from pathlib import Path

from ghostlab.execution.journey_guard import make_world

from .journey import WORLD_VERSION, JourneyFixture, JourneyScenario, ToolError
from .journey_run import ADAPTER_VERSION, ADAPTERS, run
from .journey_verify import INVARIANTS, metrics, verify

SCHEMA_VERSION = 2
MAX_CAPSULE_BYTES = 2_000_000
PAYLOAD_FIELDS = {"schema_version", "world_version", "fixture", "scenario", "actor_id",
                  "source", "seed", "recorded_model_outputs", "run"}
RUN_FIELDS = {"schema_version", "world_version", "adapter_id", "adapter_version", "actor_id",
              "status", "trace", "final_state", "checks"}
ERROR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _text(value, high):
    return type(value) is str and 1 <= len(value) <= high


def capsule_digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def export_capsule(path: Path, fixture: JourneyFixture, scenario: dict, recorded_run: dict,
                   *, source: str = "trusted-synthetic-journey-adapter"):
    JourneyScenario.from_json(scenario)  # only a scenario this world can replay
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


def _check_metadata(payload):
    """Fields replay cannot reproduce, held to the ``capsule_v2`` contract's bounds."""
    if not _text(payload["source"], 200):
        raise ValueError("Invalid capsule source")
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] <= 2**53 - 1:
        raise ValueError("Invalid capsule seed")
    outputs = payload["recorded_model_outputs"]
    if type(outputs) is not list or len(outputs) > 1000:
        raise ValueError("Invalid recorded model outputs")


def _check_event(index, event):
    if type(event) is not dict or set(event) != {"event_id", "request", "outcome", "state"}:
        raise ValueError("Invalid recorded event")
    if type(event["event_id"]) is not int or event["event_id"] != index + 1:
        raise ValueError("Trace event ids must be 1..n in order")
    request = event["request"]
    if (type(request) is not dict or set(request) != {"tool", "arguments"}
            or type(request["tool"]) is not str or len(request["tool"]) > 200
            or type(request["arguments"]) is not dict):
        raise ValueError("Invalid tool request")
    outcome = event["outcome"]
    valid = type(outcome) is dict and (
        (set(outcome) == {"result"} and type(outcome["result"]) is dict)
        or (set(outcome) == {"error", "message"} and type(outcome["error"]) is str
            and ERROR_NAME.fullmatch(outcome["error"]) is not None
            and _text(outcome["message"], 2000)))
    if not valid or type(event["state"]) is not dict:
        raise ValueError("Invalid recorded outcome or state")


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


def _check_adapter(fixture, scenario, actor_id, recorded):
    """A recording that replays must also be what its named checked-in adapter does.

    Whether a run finished rests on a call past the cap that was never made, so the
    recorded requests can't show it: a truncated run relabelled ``completed`` with a fresh
    checksum replays cleanly. Running the adapter settles the status and the trace.
    """
    try:
        produced = run(fixture, scenario, recorded["adapter_id"], actor_id=actor_id)
    except ToolError as error:
        raise ValueError("Recorded run is not what its adapter produces") from error
    if (produced["status"], produced["trace"]) != (recorded["status"], recorded["trace"]):
        raise ValueError("Recorded run is not what its adapter produces")


def replay(path: Path) -> dict:
    """Re-execute recorded tool requests, then run the named checked-in adapter to check them.

    A capsule is data: it names an adapter from the checked-in set and supplies no code. No
    model is called. A matching checksum is accidental-change detection, not proof of authorship.
    A replay mismatch means the capsule's claimed evidence was not reproduced.
    """
    payload = load_capsule(path)
    _check_metadata(payload)
    actor_id = payload["actor_id"]
    if not _text(actor_id, 100):
        raise ValueError("Invalid capsule principal")
    try:
        fixture = JourneyFixture.from_json(payload["fixture"])
        scenario = JourneyScenario.from_json(payload["scenario"])
    except TypeError as error:  # e.g. an unhashable fault id
        raise ValueError(f"Invalid fixture or scenario: {error}") from error
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
    if all(matches.values()):
        _check_adapter(fixture, scenario, actor_id, recorded)
    return {"reproduced": all(matches.values()), "matches": matches, "checks": checks,
            "metrics": metrics(fixture, state), "guarded": fixture.guarded, "model_calls": 0}
