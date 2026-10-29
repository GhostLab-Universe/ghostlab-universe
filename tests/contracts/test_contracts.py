"""M00 phase-A contract acceptance.

No __init__.py here on purpose: ``python -m unittest discover -s tests`` must not
recurse into this directory, so the core suite still runs without Pydantic.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ghostlab.capsule import export_capsule, replay
from ghostlab.contracts import (BY_NAME, CONTRACTS, TASK_TRANSITIONS, CapsuleV1,
                                ExperimentResult, RunResult)
from ghostlab.contracts.export import ROOT, contract_for, render_all
from ghostlab.core import Fixture, Scenario, experiment, run

EXAMPLES = ROOT / "examples" / "contracts"
EXAMPLE_PATHS = sorted(EXAMPLES.glob("*.json")) + sorted((EXAMPLES / "core").glob("*.json"))


VARIANTS = {"model_reply": "model_reply.fixture_tool_call",
            "run_result": "run_result.faulted_vulnerable"}


def load(name: str) -> dict:
    """The representative example for a contract, as a mutable dict."""
    stem = VARIANTS.get(name, name)
    path = next(p for p in EXAMPLE_PATHS
                if p.name.removesuffix(".json") == stem or p.name.split(".", 1)[0] == stem)
    return json.loads(path.read_text(encoding="utf-8"))


def rejects(name: str, payload) -> None:
    name = name.split(".", 1)[0]
    with pytest.raises(ValidationError):
        BY_NAME[name].model.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError):
        BY_NAME[name].model.model_validate(payload)


# --- published artifacts agree with the models ---------------------------------

def test_published_files_match_models():
    stale = [rel for rel, text in render_all().items()
             if not (ROOT / rel).exists() or (ROOT / rel).read_text(encoding="utf-8") != text]
    assert not stale, f"Run `python -m ghostlab.contracts.export`: {stale}"


def test_every_contract_has_a_schema_and_example():
    covered = {contract_for(str(p)).name for p in EXAMPLE_PATHS}
    assert covered == {c.name for c in CONTRACTS}
    for contract in CONTRACTS:
        assert (ROOT / "schemas" / "v1" / f"{contract.name}.schema.json").exists()


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=lambda p: p.name)
def test_examples_validate_in_json_and_python_modes(path):
    model = contract_for(str(path)).model
    text = path.read_text(encoding="utf-8")
    parsed = model.model_validate_json(text)
    assert model.model_validate(json.loads(text)) == parsed
    assert json.loads(parsed.model_dump_json()) == json.loads(text)  # lossless round trip


def test_schemas_forbid_unknown_fields_everywhere():
    def walk(schema):
        if isinstance(schema, dict):
            if schema.get("type") == "object" and "properties" in schema:
                assert schema.get("additionalProperties") is False, schema.get("title")
            for value in schema.values():
                walk(value)
        elif isinstance(schema, list):
            for value in schema:
                walk(value)
    for contract in CONTRACTS:
        walk(contract.model.model_json_schema())


def test_model_facing_schemas_meet_strict_output_shape():
    """Documented Meta strict-output restrictions; live acceptance is untested."""
    def walk(schema, defs):
        if "$ref" in schema:
            return walk(defs[schema["$ref"].split("/")[-1]], defs)
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for prop in schema["properties"].values():
                walk(prop, defs)
    facing = [c for c in CONTRACTS if c.model_facing]
    assert facing
    for contract in facing:
        schema = contract.model.model_json_schema()
        assert schema["type"] == "object"
        walk(schema, schema.get("$defs", {}))


# --- the existing core's real output satisfies the contracts -------------------

def test_live_core_experiment_validates():
    result = experiment()
    parsed = ExperimentResult.model_validate_json(json.dumps(result))
    assert parsed.diff.first_divergence.event_id == 2
    assert parsed.faulted.final_state.total_refunded_minor == 500_000
    assert parsed.repaired.final_state.total_refunded_minor == 250_000
    RunResult.model_validate(run(Fixture(), Scenario(max_tool_calls=1), "vulnerable"))


def test_v1_capsule_validates_and_still_replays(tmp_path):
    fixture, scenario = Fixture(), Scenario(timeout_after_commit=True)
    path = tmp_path / "capsule.json"
    export_capsule(path, fixture, scenario, run(fixture, scenario, "vulnerable"))
    capsule = CapsuleV1.model_validate_json(path.read_text(encoding="utf-8"))
    assert capsule.payload.run.final_state.total_refunded_minor == 500_000
    # Re-serialize through the contract and confirm the unchanged v1 loader accepts it.
    path.write_text(capsule.model_dump_json(indent=2), encoding="utf-8")
    replayed = replay(path)
    assert replayed["reproduced"] and replayed["model_calls"] == 0


def test_published_v1_capsule_example_replays(tmp_path):
    path = tmp_path / "capsule.json"
    path.write_text((EXAMPLES / "core" / "capsule_v1.refund_failure.json")
                    .read_text(encoding="utf-8"), encoding="utf-8")
    assert replay(path)["reproduced"]


def test_core_does_not_import_contract_libraries():
    code = ("import sys, ghostlab.core, ghostlab.capsule, ghostlab.cli; "
            "bad = [m for m in ('pydantic', 'ghostlab.contracts', 'langgraph', 'fastapi') "
            "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


# --- rejection cases required by the phase-A acceptance ------------------------

@pytest.mark.parametrize("name,mutate", [
    ("ref", lambda p: p.update(extra=1)),
    ("agent_envelope", lambda p: p.update(principal={"principal_id": "x", "kind": "human"})),
    ("agent_envelope", lambda p: p["scope"].update(admin=True)),
    ("capsule_v1", lambda p: p["payload"].update(note="unexpected")),
    ("capsule_v1", lambda p: p["payload"]["run"]["trace"][0]["request"].update(meta=1)),
    ("model_reply", lambda p: p.update(scope={"project_id": "p"})),
    ("model_reply", lambda p: p["tool_calls"][0].update(actor_id="admin")),
    ("experiment_proposal", lambda p: p.update(actor_id="synthetic-customer-1")),
    ("experiment_proposal", lambda p: p["scenario"].update(allowed_tools=["shell"])),
    ("approval_submission", lambda p: p.update(approver={"principal_id": "a", "kind": "human"})),
    ("campaign_spec", lambda p: p.update(project_id="other-project")),
], ids=lambda v: v if isinstance(v, str) else "")
def test_unknown_fields_rejected(name, mutate):
    payload = load(name)
    mutate(payload)
    rejects(name, payload)


@pytest.mark.parametrize("name,path,value", [
    ("task_spec", ["status"], "done"),
    ("campaign_record", ["status"], "paused"),
    ("agent_envelope", ["status"], "ok"),
    ("approval_request", ["status"], "auto_approved"),
    ("budget_reservation", ["status"], "spent"),
    ("memory_record", ["status"], "true"),
    ("run_result", ["status"], "passed"),
    ("model_reply", ["status"], "success"),
    ("tool_result", ["status"], "maybe"),
    ("invariant_finding", ["verdict"], "probably_failed"),
    ("trusted_scope", ["issued_by"], "model"),
    ("agent_envelope", ["sender_role"], "admin"),
    ("model_request", ["stream"], True),
], ids=lambda v: v if isinstance(v, str) else "")
def test_invalid_statuses_rejected(name, path, value):
    payload = load(name)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    rejects(name, payload)


@pytest.mark.parametrize("name,locate", [
    ("tool_invocation", lambda p: (p["call"]["arguments"], "amount_minor")),
    ("run_result", lambda p: (p["final_state"], "total_refunded_minor")),
    ("run_result", lambda p: (p["final_state"]["order"], "paid_minor")),
    ("run_result", lambda p: (p["final_state"]["ledger"][0], "amount_minor")),
    ("reality_diff", lambda p: (p, "faulted_refunded_minor")),
    ("invariant_finding", lambda p: (p, "observed_minor")),
    ("budget_limits", lambda p: (p, "max_tool_calls")),
    ("budget_usage", lambda p: (p, "tool_calls")),
    ("budget_reservation", lambda p: (p, "amount")),
    ("artifact_ref", lambda p: (p, "size_bytes")),
], ids=lambda v: v if isinstance(v, str) else "")
@pytest.mark.parametrize("bad", [True, False, 2500.0, "250000", -1])
def test_non_integer_or_negative_amounts_rejected(name, locate, bad):
    payload = load(name)
    target, key = locate(payload)
    target[key] = bad
    rejects(name, payload)


@pytest.mark.parametrize("ref", [
    {"kind": "shell", "id": "run-1", "sha256": None},
    {"kind": "run", "id": "", "sha256": None},
    {"kind": "run", "id": "run 1", "sha256": None},
    {"kind": "run", "id": "../etc/passwd", "sha256": None},
    {"kind": "run", "id": "run-1", "sha256": "ABC"},
    {"kind": "run", "id": "run-1", "sha256": "F" * 64},
    {"kind": "run", "id": "run-1"},
    {"kind": "run", "id": 7, "sha256": None},
    "run-1",
], ids=str)
def test_malformed_references_rejected(ref):
    rejects("ref", ref)
    envelope = load("agent_envelope")
    envelope["input_refs"] = [ref]
    rejects("agent_envelope", envelope)


@pytest.mark.parametrize("name,mutate", [
    ("agent_envelope", lambda p: p.update(trace_id="not-a-trace")),
    ("agent_envelope", lambda p: p.update(created_at="2026-10-29T10:00:00+05:30")),
    ("agent_envelope", lambda p: p.update(created_at="2026-02-30T10:00:00Z")),
    ("agent_envelope", lambda p: p.update(created_at=1761732000)),
    ("model_request", lambda p: p.update(context_bundle={"kind": "run", "id": "r", "sha256": None})),
    ("artifact_ref", lambda p: p.update(sha256="0" * 63)),
    ("artifact_ref", lambda p: p.update(media_type="application json")),
], ids=lambda v: v if isinstance(v, str) else "")
def test_malformed_identifiers_and_timestamps_rejected(name, mutate):
    payload = load(name)
    mutate(payload)
    rejects(name, payload)


# --- cross-field invariants encoded in the contracts ---------------------------

@pytest.mark.parametrize("name,mutate", [
    # Trust: model/tool content cannot become instructions or verified facts.
    ("context_bundle", lambda p: p["items"][0]["provenance"].update(source_kind="tool_result",
                                                                    trust="untrusted")),
    ("context_bundle", lambda p: p["items"][1]["provenance"].update(trust="trusted")),
    ("context_bundle", lambda p: p.update(estimated_tokens=p["token_budget"] + 1)),
    ("context_bundle", lambda p: p["scope"].update(role="analyst")),
    ("memory_record", lambda p: p["provenance"].update(source_kind="model_output",
                                                       trust="untrusted")),
    ("memory_record", lambda p: p.update(evidence=[])),
    ("memory_record", lambda p: p.update(effective_until=p["effective_from"])),
    ("agent_envelope", lambda p: p["scope"].update(role="analyst")),
    ("agent_envelope", lambda p: p.update(status="failed", errors=[])),
    # Approvals bind to an exact digest and expire after creation.
    ("approval_request", lambda p: p.update(expires_at=p["created_at"])),
    ("approval_request", lambda p: p["payload"].update(sha256="0" * 64)),
    # Task graph and budgets.
    ("task_spec", lambda p: p.update(depends_on=[p["task_id"]])),
    ("task_spec", lambda p: p.update(depends_on=["a", "a"])),
    ("task_spec", lambda p: p.update(attempt=5, max_attempts=1)),
    ("budget_limits", lambda p: p.update(max_cost_minor=100)),
    ("budget_reservation", lambda p: p.update(actual=1)),
    ("trusted_scope", lambda p: p.update(allowed_tools=["get_order", "get_order"])),
    ("trusted_scope", lambda p: p.update(allowed_tools=["shell"])),
    # Provider replies are internally consistent.
    ("model_reply", lambda p: p.update(status="completed")),
    ("model_reply", lambda p: p["usage"].update(cost_minor=10)),
    ("model_reply", lambda p: p.update(tool_calls=p["tool_calls"] * 2)),
    ("tool_result", lambda p: p.update(error=load("error_info"))),
    ("tool_invocation", lambda p: p["call"].update(tool="get_order")),
    ("invariant_finding", lambda p: p.update(evidence_event_ids=[])),
    ("capsule_v1", lambda p: p["payload"].update(actor_id="another-principal")),
    ("capsule_v1", lambda p: p["payload"]["scenario"].update(max_tool_calls=1)),
], ids=lambda v: v if isinstance(v, str) else "")
def test_cross_field_rules_rejected(name, mutate):
    payload = copy.deepcopy(load(name))
    mutate(payload)
    rejects(name, payload)


def test_failed_tool_result_requires_error():
    payload = load("tool_result")
    payload.update(status="error", output=None, error=None)
    rejects("tool_result", payload)


def test_task_transitions_are_closed():
    statuses = set(BY_NAME["task_spec"].model.model_json_schema()["properties"]["status"]["enum"])
    assert set(TASK_TRANSITIONS) == statuses
    assert all(targets <= statuses for targets in TASK_TRANSITIONS.values())
    for terminal in ("succeeded", "failed", "cancelled", "skipped"):
        assert not TASK_TRANSITIONS[terminal]


def test_contracts_are_immutable():
    ref = BY_NAME["ref"].model.model_validate(load("ref"))
    with pytest.raises(ValidationError):
        ref.id = "changed"
