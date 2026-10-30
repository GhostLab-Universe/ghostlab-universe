"""Versioned data-only capsule export and recorded-action replay."""

import hashlib
import json
from pathlib import Path

from .core import (ADAPTER_VERSION, Fixture, RefundWorld, Scenario, ToolError, WORLD_VERSION,
                   verify)


SCHEMA_VERSION = 1
MAX_CAPSULE_BYTES = 2_000_000
SOURCE = "trusted-synthetic-refund-adapter"


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def export_capsule(path: Path, fixture: Fixture, scenario: Scenario, recorded_run):
    from dataclasses import asdict
    payload = {
        "schema_version": SCHEMA_VERSION, "world_version": WORLD_VERSION,
        "fixture": asdict(fixture), "scenario": asdict(scenario),
        "actor_id": recorded_run["actor_id"],
        "source": SOURCE, "seed": 0,
        "recorded_model_outputs": [], "run": recorded_run,
    }
    envelope = {"payload": payload, "sha256": _digest(payload)}
    text = json.dumps(envelope, indent=2)
    if len(text.encode()) > MAX_CAPSULE_BYTES:
        raise ValueError("Capsule too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def load_capsule(path: Path):
    if path.stat().st_size > MAX_CAPSULE_BYTES:
        raise ValueError("Capsule too large")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if type(envelope) is not dict or set(envelope) != {"payload", "sha256"}:
        raise ValueError("Invalid capsule envelope")
    payload = envelope["payload"]
    if _digest(payload) != envelope["sha256"]:
        raise ValueError("Capsule checksum mismatch")
    required_fields = {"schema_version", "world_version", "fixture", "scenario",
                       "source", "seed", "recorded_model_outputs", "actor_id", "run"}
    if type(payload) is not dict or set(payload) != required_fields:
        raise ValueError("Invalid capsule payload")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported capsule schema")
    if payload.get("world_version") != WORLD_VERSION:
        raise ValueError("Unsupported world version")
    return payload


def _check_metadata(payload, recorded):
    """Fields replay cannot reproduce, held to the ``capsule_v1`` contract's values."""
    if payload["source"] != SOURCE:
        raise ValueError("Invalid capsule source")
    seed = payload["seed"]
    if type(seed) is not int or not 0 <= seed <= 2**53 - 1:
        raise ValueError("Invalid capsule seed")
    outputs = payload["recorded_model_outputs"]
    if type(outputs) is not list or len(outputs) > 1_000:
        raise ValueError("Invalid recorded model outputs")
    if type(recorded["adapter"]) is not str or recorded["adapter"] not in {"vulnerable",
                                                                            "repaired"}:
        raise ValueError("Unknown recorded adapter")
    if recorded["adapter_version"] != ADAPTER_VERSION:
        raise ValueError("Unknown recorded adapter version")
    if type(recorded["status"]) is not str or recorded["status"] not in {"completed",
                                                                          "budget_exhausted"}:
        raise ValueError("Invalid recorded run status")


def replay(path: Path):
    """Re-execute recorded tool requests; never import code or call a model.

    A matching checksum is accidental-change detection, not proof of authorship.
    A replay mismatch means the capsule's claimed evidence was not reproduced.
    """
    payload = load_capsule(path)
    if type(payload["fixture"]) is not dict or type(payload["scenario"]) is not dict:
        raise ValueError("Invalid fixture or scenario object")
    if type(payload["actor_id"]) is not str or not payload["actor_id"]:
        raise ValueError("Invalid capsule principal")
    fixture = Fixture(**payload["fixture"])
    scenario = Scenario(**payload["scenario"])
    recorded = payload["run"]
    if type(recorded) is not dict or set(recorded) != {
        "adapter", "adapter_version", "actor_id", "status", "trace", "final_state", "checks"
    }:
        raise ValueError("Invalid recorded run")
    trace = recorded["trace"]
    if type(trace) is not list or len(trace) > scenario.max_tool_calls:
        raise ValueError("Invalid trace length")
    if recorded["actor_id"] != payload["actor_id"]:
        raise ValueError("Recorded principal does not match capsule")
    _check_metadata(payload, recorded)
    world = RefundWorld(fixture, scenario, actor_id=payload["actor_id"])
    for event in trace:
        if type(event) is not dict or set(event) != {"event_id", "request", "outcome", "state"}:
            raise ValueError("Invalid recorded event")
        request = event["request"]
        if type(request) is not dict or set(request) != {"tool", "arguments"}:
            raise ValueError("Invalid tool request")
        if type(request["tool"]) is not str or type(request["arguments"]) is not dict:
            raise ValueError("Invalid tool request")
        try:
            world.call(request["tool"], request["arguments"])
        except ToolError:
            pass  # Recreated errors are compared to the original trace below.
    state = world.snapshot()
    checks = verify(fixture, state)
    matches = {
        "trace": world.trace == trace,
        "final_state": state == recorded["final_state"],
        "checks": checks == recorded["checks"],
    }
    return {"reproduced": all(matches.values()), "matches": matches,
            "checks": checks, "total_refunded_minor": state["total_refunded_minor"],
            "model_calls": 0}
