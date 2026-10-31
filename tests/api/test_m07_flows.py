"""Whole journeys through HTTP: create, pause, decide, repaired result, capsule, replay.

Inference is a labeled fixture (httpx.MockTransport). Worlds, verifier, approvals,
stores and capsules are the real code.
"""

import json
import threading

from conftest import spec_json
from m05_harness import PROJECT, meta_reply, proposal
from m05_journey_harness import journey_reply

REFUND, JOURNEY = "refund-world/1", "group-journey/1"


def test_refund_flow_from_create_to_replayed_repair(stack):
    api = stack(proposal())
    created = api.create(REFUND)
    assert created.status_code == 202
    assert "Idempotency-Replayed" not in created.headers
    campaign_id = created.json()["record"]["campaign_id"]
    assert created.json()["record"]["project_id"] == PROJECT

    paused = api.wait(campaign_id, "awaiting_approval")
    assert paused["job"] == {"state": "idle", "cancel_requested": False, "error": None}
    experiment = paused["experiments"][0]
    assert experiment["runs"] == ["baseline", "fork"]

    base = f"/v1/campaigns/{campaign_id}/experiments/{experiment['experiment_id']}"
    compared = api.get(f"{base}/diff").json()
    assert compared["coverage"] == {"baseline": "completed", "fork": "completed",
                                    "faults_triggered": True, "complete": True}
    diff = compared["diff"]
    assert diff["world_version"] == REFUND
    assert diff["baseline_metrics"] == {"refunded_minor": 250000}
    assert diff["faulted_metrics"] == {"refunded_minor": 500000}
    assert diff["first_divergence"]["event_id"] == 2

    fork = api.get(f"{base}/runs/fork").json()
    assert fork["result"]["adapter_id"] == "vulnerable"
    assert fork["result"]["checks"]["total_refund_within_payment"] is False
    assert any(f["verdict"] == "failed" for f in fork["findings"])

    approval = api.get(f"/v1/approvals/{paused['approval']['approval_id']}", who="reviewer").json()
    assert approval["effective_status"] == "pending"
    assert approval["action"]["adapter"] == "repaired"

    decided = api.decide(paused)
    assert decided.status_code == 200, decided.text
    assert decided.json()["resumed"] is True
    assert decided.json()["decision"]["approver"]["principal_id"] == "reviewer-1"

    done = api.wait(campaign_id, "completed")
    assert done["outcome"] == "repair_verified"
    assert done["repair"]["verified"] is True
    assert done["repair"]["refunded_minor"] == 250000
    assert done["record"]["usage"]["model_calls"] == 1
    assert done["experiments"][0]["runs"] == ["baseline", "fork", "repaired"]
    repaired = api.get(f"{base}/runs/repaired").json()
    assert repaired["result"]["checks"]["total_refund_within_payment"] is True

    exported = api.post(f"{base}/capsules/repaired")
    assert exported.status_code == 201, exported.text
    assert exported.json()["capsule_schema_version"] == 1

    replayed = api.post(f"{base}/capsules/repaired/replay").json()
    assert replayed["reproduced"] is True and replayed["run_status"] == "completed"
    assert replayed["model_calls"] == 0
    assert replayed["metrics"] == {"refunded_minor": 250000}
    assert len(api.script.requests) == 1  # nothing after the proposal reached the provider


def test_journey_flow_from_create_to_replayed_repair(stack):
    api = stack(journey_reply())
    campaign_id = api.create(JOURNEY).json()["record"]["campaign_id"]
    paused = api.wait(campaign_id, "awaiting_approval")
    assert paused["record"]["spec"]["world_version"] == JOURNEY

    experiment = paused["experiments"][0]
    base = f"/v1/campaigns/{campaign_id}/experiments/{experiment['experiment_id']}"
    view = api.get(base).json()
    assert view["execution"] == {"mode": "lab", "guarded": False}
    assert "delivered_messages_permitted" in view["diff"]["violated_invariants"]
    assert api.get(f"{base}/runs/fork").json()["execution"]["guarded"] is False

    assert api.decide(paused).status_code == 200
    done = api.wait(campaign_id, "completed")
    assert done["outcome"] == "repair_verified"
    assert done["repair"]["execution"] == {"mode": "enforced", "guarded": True}
    assert done["repair"]["held_by"] == "adapter"
    assert done["repair"]["safety_held"] is True
    assert done["repair"]["lost_faults"] == []

    repaired = api.get(f"{base}/runs/repaired").json()
    assert repaired["execution"]["guarded"] is True
    assert repaired["result"]["world_version"] == JOURNEY

    exported = api.post(f"{base}/capsules/repaired")
    assert exported.status_code == 201, exported.text
    assert exported.json()["capsule_schema_version"] == 2
    replayed = api.post(f"{base}/capsules/repaired/replay").json()
    assert replayed["reproduced"] is True and replayed["guarded"] is True
    assert replayed["model_calls"] == 0
    assert len(api.script.requests) == 1


def test_a_capsule_travels_between_projects_as_data(stack):
    for world, reply in ((REFUND, proposal()), (JOURNEY, journey_reply())):
        api = stack(reply)
        campaign_id = api.create(world).json()["record"]["campaign_id"]
        paused = api.wait(campaign_id, "awaiting_approval")
        experiment_id = paused["experiments"][0]["experiment_id"]
        exported = api.post(f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}"
                            "/capsules/fork").json()
        capsule_id = exported["artifact"]["id"]
        downloaded = api.get(f"/v1/artifacts/{capsule_id}")
        assert downloaded.status_code == 200
        assert downloaded.headers["etag"] == f'"{exported["artifact"]["sha256"]}"'
        assert api.post(f"/v1/capsules/{capsule_id}/replay").json()["reproduced"] is True

        assert api.get(f"/v1/artifacts/{capsule_id}", who="outsider").status_code == 404
        assert api.post(f"/v1/capsules/{capsule_id}/replay", who="outsider").status_code == 404

        capsule = json.loads(downloaded.content)
        imported = api.post("/v1/capsules/import", who="outsider", json=capsule)
        assert imported.status_code == 201, imported.text
        assert imported.json()["replay"]["reproduced"] is True
        own_id = imported.json()["capsule"]["artifact"]["id"]
        assert own_id != capsule_id
        again = api.post("/v1/capsules/import", who="outsider", json=capsule)
        assert again.json()["capsule"] == imported.json()["capsule"]  # a retry: nothing new
        assert api.post(f"/v1/capsules/{own_id}/replay", who="outsider").status_code == 200
        assert api.post(f"/v1/capsules/{own_id}/replay").status_code == 404
        assert len(api.script.requests) == 1


def test_a_refund_capsule_may_be_imported_in_either_published_form(stack):
    from ghostlab.contracts.world_refund import capsule_v1_to_v2

    api = stack(proposal())
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    paused = api.wait(campaign_id, "awaiting_approval")
    experiment_id = paused["experiments"][0]["experiment_id"]
    capsule_id = api.post(f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}"
                          "/capsules/fork").json()["artifact"]["id"]
    v1 = json.loads(api.get(f"/v1/artifacts/{capsule_id}").content)
    v2 = capsule_v1_to_v2(v1).model_dump(mode="json")
    imported = api.post("/v1/capsules/import", json=v2)
    assert imported.status_code == 201, imported.text
    assert imported.json()["replay"]["reproduced"] is True
    assert imported.json()["capsule"]["capsule_schema_version"] == 1


def test_an_experiment_with_no_failure_finishes_without_an_approval(stack):
    clean = proposal(adapter="repaired")
    second = proposal(adapter="repaired", max_tool_calls=19)
    api = stack(clean, second)
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    done = api.wait(campaign_id, "completed")
    assert done["outcome"] == "no_failure_found"
    assert done["approval"] is None and done["repair"] is None
    assert len(done["experiments"]) == 2


def test_a_rejected_repair_ends_the_campaign_without_running_it(stack):
    api = stack(journey_reply())
    paused = api.paused(JOURNEY)
    decided = api.decide(paused, "reject", reason="Not this repair")
    assert decided.status_code == 200
    done = api.wait(paused["record"]["campaign_id"], "completed")
    assert done["outcome"] == "repair_rejected"
    assert done["repair"] is None
    assert "repaired" not in done["experiments"][0]["runs"]
    experiment_id = done["experiments"][0]["experiment_id"]
    missing = api.get(f"/v1/campaigns/{paused['record']['campaign_id']}"
                      f"/experiments/{experiment_id}/runs/repaired")
    assert missing.status_code == 404


def test_a_malformed_model_reply_is_a_recorded_failure_not_a_server_error(stack):
    api = stack(meta_reply("not json"), meta_reply("not json"))
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    done = api.wait(campaign_id, "failed")
    assert done["outcome"] == "explorer_failed"
    assert done["errors"][-1]["code"] == "provider_invalid_output"
    assert done["experiments"] == []


def test_events_poll_in_order_and_a_reconnect_loses_nothing(stack):
    api = stack(proposal())
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    paused = api.wait(campaign_id, "awaiting_approval")
    url = f"/v1/campaigns/{campaign_id}/events"

    first = api.get(url, params={"limit": 2}).json()
    assert [event["sequence"] for event in first["events"]] == [1, 2]
    assert first["has_more"] is True and first["next_after"] == 2
    rest = api.get(url, params={"after": first["next_after"]}).json()
    assert rest["has_more"] is False
    assert rest["events"][-1]["kind"] == "approval_requested"
    assert rest["next_after"] == paused["event_count"]

    api.decide(paused)
    api.wait(campaign_id, "completed")
    later = api.get(url, params={"after": rest["next_after"]}).json()
    assert later["events"][0]["sequence"] == rest["next_after"] + 1
    assert later["events"][-1]["summary"] == "Campaign completed: repair_verified"
    empty = api.get(url, params={"after": later["next_after"]}).json()
    assert empty["events"] == [] and empty["next_after"] == later["next_after"]


def test_the_spec_is_recorded_as_sent(stack):
    api = stack(proposal())
    created = api.create(REFUND).json()
    assert created["record"]["spec"] == spec_json(REFUND)


def test_a_new_campaign_reads_as_empty_rather_than_failing(stack):
    gate_open, reached = threading.Event(), threading.Event()

    def hold(name, state):
        if name == "propose":
            reached.set()
            assert gate_open.wait(30)

    api = stack(proposal(), lab_options={"on_node": hold})
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    assert reached.wait(30)
    view = api.view(campaign_id)
    assert view["record"]["status"] == "running"
    assert view["experiments"] == [] and view["approval"] is None and view["repair"] is None
    assert view["outcome"] is None and view["errors"] == []
    assert view["record"]["usage"]["model_calls"] == 0
    missing = f"/v1/campaigns/{campaign_id}/experiments/exp-missing"
    for path in (missing, f"{missing}/diff", f"{missing}/runs/fork"):
        assert api.get(path).status_code == 404
    assert api.post(f"{missing}/capsules/fork").status_code == 404
    assert api.post(f"{missing}/capsules/fork/replay").status_code == 404
    gate_open.set()
    api.wait(campaign_id, "awaiting_approval")


def test_a_run_with_no_repair_has_no_repaired_evidence(stack):
    api = stack(proposal())
    paused = api.paused(REFUND)
    base = (f"/v1/campaigns/{paused['record']['campaign_id']}/experiments/"
            f"{paused['experiments'][0]['experiment_id']}")
    assert api.get(f"{base}/runs/repaired").status_code == 404
    assert api.post(f"{base}/capsules/repaired").status_code == 404
    assert api.post(f"{base}/capsules/repaired/replay").status_code == 404
    assert api.post(f"{base}/capsules/fork/replay").status_code == 404  # not exported yet


def test_a_principal_with_no_world_identity_is_a_recorded_refusal(stack):
    api = stack(proposal())
    created = api.create(REFUND, who="stranger")
    assert created.status_code == 202
    campaign_id = created.json()["record"]["campaign_id"]
    done = api.wait(campaign_id, "failed", who="stranger")
    assert done["outcome"] == "policy_denied"
    assert done["errors"][-1]["code"] == "forbidden"
    assert "unbound_principal" in done["errors"][-1]["message"]
    assert done["experiments"] == []
