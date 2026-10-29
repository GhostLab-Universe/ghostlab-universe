"""Contract set 1.1: world packs and Analyst outputs, without changing set 1.

No __init__.py here, so the stdlib core suite still runs without Pydantic.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ghostlab.capsule import replay
from ghostlab.contracts import (BY_NAME, CONTRACT_SET_VERSION, CONTRACTS, ApprovalRequest,
                                CapsuleV1, ContextBundle, ExperimentResult, TrustedScope)
from ghostlab.contracts import world_refund as refund
from ghostlab.contracts.export import ROOT
from ghostlab.contracts.world import (CapsuleV2, WorldCampaignSpec, WorldExperimentProposal,
                                      WorldInvariantFinding, WorldMemoryQuery, WorldMemoryRecord,
                                      WorldRealityDiff, WorldRunResult, WorldScenario,
                                      WorldToolDefinition, WorldToolInvocation, WorldTrustedScope)
from ghostlab.contracts.world_packs import PACKS, check_arguments, is_executable, pack
from ghostlab.core import (Fixture, RefundWorld, Scenario, ToolError, experiment, run,
                           run_agent, verify)

SCHEMAS = ROOT / "schemas" / "v1"
EXAMPLES = ROOT / "examples" / "contracts"
J = "group-journey/1"


def ex(name: str) -> dict:
    path = EXAMPLES / f"{name}.json"
    path = path if path.exists() else EXAMPLES / "core" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def rejects(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))


# --- set 1 is frozen ----------------------------------------------------------------

def test_contract_set_1_is_unchanged():
    snapshot = json.loads((SCHEMAS / "contract-set-1.0.snapshot.json").read_text("utf-8"))
    index = {c["name"]: c for c in json.loads((SCHEMAS / "index.json").read_text("utf-8"))
             ["contracts"]}
    bundle = json.loads((SCHEMAS / "contracts.bundle.json").read_text("utf-8"))
    canon = lambda v: hashlib.sha256(  # noqa: E731
        json.dumps(v, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert len(snapshot["contracts"]) == 30
    for name, frozen in snapshot["contracts"].items():
        assert (index[name]["version"], index[name]["type"], index[name]["sha256"]) == \
            (frozen["version"], frozen["type"], frozen["sha256"]), name
    for name, digest in snapshot["bundle_defs"].items():  # generated TS names/types stay put
        assert canon(bundle["$defs"][name]) == digest, name
    assert CONTRACT_SET_VERSION == "ghostlab-contracts/1.1"
    assert len(CONTRACTS) == 47


# --- the refund pack describes the real core ----------------------------------------

def _arguments(tool):
    values = {"order_id": "synthetic-order-1", "customer_id": "synthetic-customer-1",
              "idempotency_key": "intent-1", "amount_minor": 250_000}
    return {name: values[name] for name in tool.parameters["properties"]}


def test_refund_pack_tools_match_the_core():
    world = pack("refund-world/1")
    assert {t.name for t in world.tools} == {"get_order", "issue_refund", "get_refund_status"}
    for tool in world.tools:
        arguments = _arguments(tool)
        assert check_arguments(tool.parameters, arguments) == []
        RefundWorld(Fixture(), Scenario()).call(tool.name, arguments)  # accepted by the core
        with pytest.raises(ToolError):  # and the core rejects anything outside the pack shape
            RefundWorld(Fixture(), Scenario()).call(tool.name, {**arguments, "extra": "x"})


def test_refund_pack_invariants_faults_and_adapters_match_the_core():
    world = pack("refund-world/1")
    state = RefundWorld(Fixture(), Scenario()).snapshot()
    assert world.invariant_names == set(verify(Fixture(), state))
    assert [f.fault_id for f in world.faults] == ["timeout_after_commit"]
    assert "timeout_after_commit" in Scenario.__dataclass_fields__
    assert {a.adapter_id for a in world.adapters} == {"vulnerable", "repaired"}
    for adapter in world.adapters:
        run_agent(RefundWorld(Fixture(), Scenario()), adapter.adapter_id)
    with pytest.raises(ValueError):
        run_agent(RefundWorld(Fixture(), Scenario()), "journey-naive")


def test_only_implemented_packs_are_executable():
    assert is_executable("refund-world/1")
    assert not is_executable(J) and not is_executable("service-relay/1")
    assert not is_executable("creator-release/1")  # deferred: not registered at all
    assert {p.status for p in PACKS.values()} == {"implemented", "specified"}


# --- refund evidence maps losslessly ------------------------------------------------

def test_refund_runs_diffs_and_proposals_round_trip():
    result = ExperimentResult.model_validate(experiment())
    for name in ("baseline", "faulted", "repaired"):
        original = getattr(result, name)
        generic = refund.run_to_world(original)
        assert generic.world_version == "refund-world/1"
        assert refund.run_from_world(generic) == original
    diff = refund.diff_to_world(result.diff)
    assert diff.faulted_metrics == {"refunded_minor": 500_000}
    assert diff.first_divergence.event_id == 2
    proposal = BY_NAME["experiment_proposal"].model.model_validate(
        ex("experiment_proposal.timeout_after_commit"))
    assert refund.proposal_from_world(refund.proposal_to_world(proposal)) == proposal
    for timeout in (True, False):
        scenario = {"timeout_after_commit": timeout, "max_tool_calls": 7}
        assert refund.scenario_from_world(refund.scenario_to_world(scenario)).model_dump() == \
            scenario
    finding = refund.finding_to_world(ex("invariant_finding.over_refund"))
    assert (finding.observed_value, finding.unit) == (500_000, "minor_units")


def test_v1_capsule_round_trips_through_v2_and_still_replays(tmp_path):
    original = ex("capsule_v1.refund_failure")
    v2 = refund.capsule_v1_to_v2(original)
    assert v2.payload.schema_version == 2 and v2.sha256 == refund.capsule_digest(
        v2.payload.model_dump(mode="json"))
    back = refund.capsule_v2_to_v1(v2)
    assert back.model_dump(mode="json") == original  # identical, including the checksum
    path = tmp_path / "capsule.json"
    path.write_text(back.model_dump_json(indent=2), encoding="utf-8")
    assert replay(path)["reproduced"] and replay(path)["model_calls"] == 0
    CapsuleV1.model_validate(original)  # v1 loading is untouched


def test_non_refund_evidence_does_not_map_to_refund_v1():
    with pytest.raises(ValueError):
        refund.run_from_world(ex("world_run_result.journey_fixture"))
    with pytest.raises(ValueError):
        refund.scenario_from_world(ex("world_scenario.group_journey_revocation"))


# --- names are checked against the registered pack -----------------------------------

@pytest.mark.parametrize("model,name,mutate", [
    (WorldScenario, "world_scenario.group_journey_revocation",
     lambda p: p["faults"][0].update(fault_id="timeout_after_commit")),
    (WorldScenario, "world_scenario.group_journey_revocation",
     lambda p: p["faults"][1]["parameters"][0].update(value="1")),
    (WorldScenario, "world_scenario.group_journey_revocation",
     lambda p: p["faults"][1]["parameters"][0].update(value=True)),
    (WorldScenario, "world_scenario.group_journey_revocation",
     lambda p: p["faults"][0].update(parameters=[])),
    (WorldScenario, "world_scenario.group_journey_revocation",
     lambda p: p.update(world_version="creator-release/1")),
    (WorldExperimentProposal, "world_experiment_proposal.group_journey_revocation",
     lambda p: p.update(adapter_id="vulnerable")),
    (WorldExperimentProposal, "world_experiment_proposal.group_journey_revocation",
     lambda p: p["scenario"].update(world_version="refund-world/1", faults=[])),
    (WorldExperimentProposal, "world_experiment_proposal.group_journey_revocation",
     lambda p: p.update(actor_id="traveller-a")),
    (WorldTrustedScope, "world_trusted_scope.journey_target",
     lambda p: p["allowed_tools"].append("issue_refund")),
    (WorldToolDefinition, "world_tool_definition.journey_deliver_message",
     lambda p: p["parameters"]["required"].clear()),
    (WorldToolDefinition, "world_tool_definition.journey_deliver_message",
     lambda p: p.update(name="send_anything")),
    (WorldToolInvocation, "world_tool_invocation.journey_deliver_message",
     lambda p: p["arguments"].update(extra="x")),
    (WorldToolInvocation, "world_tool_invocation.journey_deliver_message",
     lambda p: p.update(arguments={})),
    (WorldToolInvocation, "world_tool_invocation.journey_deliver_message",
     lambda p: p.update(tool="hold_availability",
                        arguments={"journey_id": "j", "option_id": "o", "intent_version": True})),
    (WorldRunResult, "world_run_result.journey_fixture",
     lambda p: p["checks"].pop("delivered_messages_permitted")),
    (WorldRunResult, "world_run_result.journey_fixture",
     lambda p: p["checks"].update(total_refund_within_payment=True)),
    (WorldRunResult, "world_run_result.journey_fixture",
     lambda p: p.update(adapter_id="repaired")),
    (WorldRunResult, "world_run_result.journey_fixture",
     lambda p: p["trace"].reverse()),
    (WorldRealityDiff, "world_reality_diff.journey_fixture",
     lambda p: p.update(violated_invariants=["unique_idempotency_keys"])),
    (WorldInvariantFinding, "world_invariant_finding.journey_revoked_delivery",
     lambda p: p.update(invariant="authorized_refunds")),
    (WorldInvariantFinding, "world_invariant_finding.journey_revoked_delivery",
     lambda p: p.update(evidence_event_ids=[])),
    (WorldInvariantFinding, "world_invariant_finding.journey_revoked_delivery",
     lambda p: p.update(unit=None)),
    (CapsuleV2, "capsule_v2.journey_fixture",
     lambda p: p["payload"].update(world_version="service-relay/1")),
    (CapsuleV2, "capsule_v2.journey_fixture",
     lambda p: p["payload"].update(schema_version=True)),
    (CapsuleV2, "capsule_v2.journey_fixture",
     lambda p: p["payload"].update(actor_id="someone-else")),
    (WorldCampaignSpec, "world_campaign_spec.group_journey",
     lambda p: p.update(world_version="creator-release/1")),
    (WorldCampaignSpec, "world_campaign_spec.group_journey",
     lambda p: p.update(project_id="other")),
    (WorldMemoryRecord, "world_memory_record.journey_finding",
     lambda p: p.update(world_version="anything/1")),
], ids=lambda v: v if isinstance(v, str) else "")
def test_world_names_are_validated_against_the_pack(model, name, mutate):
    payload = copy.deepcopy(ex(name))
    mutate(payload)
    rejects(model, payload)


@pytest.mark.parametrize("name,mutate", [
    ("repair_suggestion.journey_recheck_permission", lambda p: p.update(adapter_id="journey-naive")),
    ("repair_suggestion.refund_status_check", lambda p: p.update(adapter_id="vulnerable")),
    ("repair_suggestion.refund_status_check", lambda p: p.update(adapter_id=None)),
    ("repair_suggestion.refund_status_check",
     lambda p: p.update(target_invariants=["delivered_messages_permitted"])),
    ("repair_suggestion.refund_status_check", lambda p: p.update(code="rm -rf /")),
    ("repair_suggestion.refund_status_check", lambda p: p.update(verdict="passed")),
    ("evidence_interpretation.refund_over_refund",
     lambda p: p["claims"][0].update(invariant="delivered_messages_permitted")),
    ("evidence_interpretation.refund_over_refund",
     lambda p: p["claims"][0].update(cited=[{"kind": "approval", "id": "a-1", "sha256": None}])),
    ("evidence_interpretation.refund_over_refund", lambda p: p.update(verdict="failed")),
    ("evidence_interpretation.refund_over_refund",
     lambda p: p.update(hypothesis_assessment="proven")),
    ("evidence_interpretation.refund_over_refund", lambda p: p.update(scope={"role": "x"})),
], ids=lambda v: v if isinstance(v, str) else "")
def test_analyst_outputs_cannot_carry_verdicts_code_or_foreign_names(name, mutate):
    payload = copy.deepcopy(ex(name))
    mutate(payload)
    rejects(BY_NAME[name.split(".", 1)[0]].model, payload)


def test_model_facing_contracts_have_no_free_form_objects():
    def walk(schema, defs, path):
        if "$ref" in schema:
            return walk(defs[schema["$ref"].split("/")[-1]], defs, path)
        for key in ("anyOf", "oneOf", "allOf"):
            for option in schema.get(key, []):
                walk(option, defs, path)
        if schema.get("type") == "object":
            assert "properties" in schema, f"free-form object at {path}"
            assert schema.get("additionalProperties") is False, path
            for name, prop in schema["properties"].items():
                walk(prop, defs, f"{path}.{name}")
        if schema.get("type") == "array":
            walk(schema.get("items", {}), defs, f"{path}[]")
    for contract in (c for c in CONTRACTS if c.model_facing):
        schema = contract.model.model_json_schema()
        walk(schema, schema.get("$defs", {}), contract.name)


# --- a specified journey campaign is expressible end to end --------------------------

def test_journey_campaign_walks_every_contract_it_touches():
    spec = WorldCampaignSpec.model_validate(ex("world_campaign_spec.group_journey"))
    assert spec.world_version == J and not is_executable(spec.world_version)
    record = BY_NAME["world_campaign_record"].model.model_validate(
        ex("world_campaign_record.group_journey"))
    assert record.spec == spec
    # Model roles hold no world tools, so world-neutral scopes, bundles and approvals work.
    supervisor = TrustedScope.model_validate({**ex("trusted_scope.explorer"),
                                              "role": "supervisor", "delegated_by": None})
    ContextBundle.model_validate({**ex("context_bundle.explorer"),
                                  "objective": spec.objective})
    ApprovalRequest.model_validate({**ex("approval_request.apply_repair"),
                                    "scope": supervisor.model_dump(mode="json")})
    target = WorldTrustedScope.model_validate(ex("world_trusted_scope.journey_target"))
    proposal = WorldExperimentProposal.model_validate(
        ex("world_experiment_proposal.group_journey_revocation"))
    assert proposal.world_version == target.world_version == spec.world_version
    world = pack(J)
    definitions = [WorldToolDefinition.model_validate({
        "world_version": J, "name": name, "description": world.tool(name).description,
        "parameters": world.tool(name).parameters}) for name in target.allowed_tools]
    invocation = WorldToolInvocation.model_validate(
        ex("world_tool_invocation.journey_deliver_message"))
    assert invocation.tool in {d.name for d in definitions}
    BY_NAME["world_tool_result"].model.model_validate(ex("world_tool_result.journey_delivered"))
    run_result = WorldRunResult.model_validate(ex("world_run_result.journey_fixture"))
    assert run_result.adapter_id == proposal.adapter_id
    finding = WorldInvariantFinding.model_validate(
        ex("world_invariant_finding.journey_revoked_delivery"))
    assert not run_result.checks[finding.invariant] and finding.verdict == "failed"
    diff = WorldRealityDiff.model_validate(ex("world_reality_diff.journey_fixture"))
    assert diff.violated_invariants == [finding.invariant]
    capsule = CapsuleV2.model_validate(ex("capsule_v2.journey_fixture"))
    assert capsule.payload.scenario == proposal.scenario and capsule.payload.run == run_result
    memory = WorldMemoryRecord.model_validate(ex("world_memory_record.journey_finding"))
    assert memory.evidence[0].id == finding.finding_id
    query = WorldMemoryQuery.model_validate(ex("world_memory_query.journey_findings"))
    assert query.world_version == memory.world_version == J
    for name in ("evidence_interpretation.journey_revocation",
                 "repair_suggestion.journey_recheck_permission"):
        output = BY_NAME[name.split(".")[0]].model.model_validate(ex(name))
        assert output.world_version == J


def test_m03_analyst_skills_resolve_to_published_contracts():
    for path in sorted((ROOT / "skills").glob("*.json")):
        wanted = json.loads(path.read_text(encoding="utf-8"))["output_schema"]
        contract = BY_NAME.get(wanted["name"])
        assert contract is not None and contract.version == wanted["version"], path.name


def test_pack_manifests_are_published_and_fixed():
    for version, world in PACKS.items():
        name = version.split("/")[0].replace("-", "_")
        published = json.loads((EXAMPLES / "core" / f"world_pack.{name}.json")
                               .read_text(encoding="utf-8"))
        assert published == world.model_dump(mode="json")
