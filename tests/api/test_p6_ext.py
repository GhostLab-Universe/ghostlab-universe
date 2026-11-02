"""P6 extension endpoints over the M07 stack: real middleware and plane."""

import json

from ghostlab.contracts.export import ROOT

REFUND = "refund-world/1"
RESERVATION = "reservation-world/1"
EXAMPLES = ROOT / "examples" / "contracts" / "core"


def refund_snapshot(api):
    response = api.get(f"/v1/extensions/environments/{REFUND}/fixtures/synthetic-inr-2500")
    assert response.status_code == 200, response.text
    return response.json()["snapshot"]


def refund_ids(api):
    snapshot = refund_snapshot(api)
    return snapshot["order_id"], snapshot["customer_id"]


def test_list_environments(stack):
    api = stack()
    response = api.get("/v1/extensions/environments")
    assert response.status_code == 200, response.text
    worlds = {env["world_version"]: env for env in response.json()["environments"]}
    assert {REFUND, RESERVATION} <= set(worlds)
    assert set(worlds[REFUND]["targets"]) == {"vulnerable", "repaired"}
    assert worlds[RESERVATION]["capabilities"]["snapshot_restore"] is False


def test_describe_and_fixture(stack):
    api = stack()
    response = api.get(f"/v1/extensions/environments/{REFUND}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["tools"] == ["get_order", "issue_refund", "get_refund_status"]
    assert detail["faults"] == ["timeout_after_commit"]
    assert detail["package"] == "ghostlab.adapters.refund_env"
    order_id, customer_id = refund_ids(api)
    assert order_id and customer_id
    missing = api.get(f"/v1/extensions/environments/{REFUND}/fixtures/nope")
    assert missing.status_code == 404, missing.text
    unknown = api.get("/v1/extensions/environments/nope-1")
    assert unknown.status_code == 404, unknown.text


def test_qualify_reservation(stack):
    api = stack()
    response = api.post("/v1/extensions/qualify", json={
        "world_version": RESERVATION, "fixture": "half_full",
        "scenario": {"timeout_after_commit": False, "stale_snapshot": False,
                     "skip_capacity_check": False, "max_tool_calls": 20},
        "script": [{"tool": "check_availability", "arguments": {}},
                   {"tool": "reserve", "arguments": {"holder": "carol", "seats": 2}}]})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["passed"] is True
    assert report["checks"]
    assert "snapshot_restore" in report["missing_capabilities"]


def test_fidelity_refund(stack):
    api = stack()
    order_id, customer_id = refund_ids(api)
    identity = {"order_id": order_id, "customer_id": customer_id}
    response = api.post("/v1/extensions/fidelity", json={
        "world_version": REFUND, "fixture": "synthetic-inr-2500",
        "scenario": {"timeout_after_commit": False, "max_tool_calls": 20},
        "actor_id": customer_id,
        "script": [{"tool": "get_order", "arguments": identity}], "repeats": 2,
        "declared_tested": ["reset"]})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["passed"] is True
    assert report["determinism_identical"] is True
    assert report["replay_reproduced"] is True


def test_campaign_finds_refund_failure(stack):
    api = stack()
    order_id, customer_id = refund_ids(api)
    response = api.post("/v1/extensions/campaigns", json={
        "world_version": REFUND, "attempts": [{
            "name": "vuln-timeout", "arm": "fixed-scenario",
            "fixture_name": "synthetic-inr-2500",
            "scenario": {"timeout_after_commit": True, "max_tool_calls": 20},
            "actor_id": customer_id,
            "task": {"task_id": "demo", "params": {"order_id": order_id,
                                                  "customer_id": customer_id}},
            "target": {"kind": "registered", "id": "vulnerable"}, "cap": 20}]})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["attempts_run"] == 1
    assert report["rules_failed"]
    assert report["caps_respected"] is True
    assert report["guided_excluded"] is True
    assert report["details"]["attempts"][0]["trace"]


def test_inspect_replay_retest(stack):
    api = stack()
    frozen = json.loads((EXAMPLES / "capsule_v2.refund_failure.json").read_text(
        encoding="utf-8"))
    inspected = api.post("/v1/extensions/inspect", json={"envelope": frozen})
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["replay_supported"] is True
    snapshot = refund_snapshot(api)
    order_id, customer_id = snapshot["order_id"], snapshot["customer_id"]
    script = api.post("/v1/extensions/retest", json={
        "world_version": REFUND,
        "target": {"kind": "registered", "id": "vulnerable"},
        "fixture": snapshot,
        "scenario": {"timeout_after_commit": True, "max_tool_calls": 20},
        "actor_id": customer_id,
        "task": {"task_id": "demo", "params": {"order_id": order_id,
                                               "customer_id": customer_id}},
        "cap": 20})
    assert script.status_code == 200, script.text
    assert script.json()["failed"]
    assert script.json()["replay_equivalence_claimed"] is False


def test_minimize_refund(stack):
    api = stack()
    snapshot = refund_snapshot(api)
    order_id, customer_id = snapshot["order_id"], snapshot["customer_id"]
    response = api.post("/v1/extensions/minimize", json={
        "world_version": REFUND,
        "target": {"kind": "registered", "id": "vulnerable"},
        "fixture": snapshot,
        "scenario_template": {"max_tool_calls": 20},
        "fault_keys": ["timeout_after_commit"], "actor_id": customer_id,
        "task": {"task_id": "demo", "params": {"order_id": order_id,
                                               "customer_id": customer_id}},
        "cap": 20, "invariant": "total_refund_within_payment"})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "already_minimal"
    assert report["regression"]["faults"] == ["timeout_after_commit"]


def test_invalid_extension_requests(stack):
    api = stack()
    bad_world = api.post("/v1/extensions/qualify", json={
        "world_version": "nope-1", "fixture": "half_full", "scenario": {},
        "script": [{"tool": "check_availability", "arguments": {}}]})
    assert bad_world.status_code == 404, bad_world.text
    bad_body = api.post("/v1/extensions/inspect", json={"envelope": {"nope": 1}})
    assert bad_body.status_code == 422, bad_body.text


def reservation_snapshot(api, name="half_full"):
    response = api.get(f"/v1/extensions/environments/{RESERVATION}/fixtures/{name}")
    assert response.status_code == 200, response.text
    return response.json()["snapshot"]


def test_campaign_script_target(stack):
    api = stack()
    response = api.post("/v1/extensions/campaigns", json={
        "world_version": RESERVATION, "attempts": [{
            "name": "script-probe", "arm": "fixed-scenario",
            "fixture_name": "half_full",
            "scenario": {"timeout_after_commit": False, "stale_snapshot": False,
                         "skip_capacity_check": False, "max_tool_calls": 20},
            "actor_id": "console-probe",
            "task": {"task_id": "probe", "params": {}},
            "target": {"kind": "script", "actions": [
                {"tool": "check_availability", "arguments": {}},
                {"tool": "reserve", "arguments": {"holder": "carol", "seats": 1}}]},
            "cap": 20}]})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["attempts_run"] == 1
    assert report["details"]["attempts"][0]["tool_calls"] == 2


def test_retest_script_target(stack):
    api = stack()
    order_id, customer_id = refund_ids(api)
    response = api.post("/v1/extensions/retest", json={
        "world_version": REFUND,
        "target": {"kind": "script", "actions": [
            {"tool": "get_order", "arguments": {"order_id": order_id,
                                               "customer_id": customer_id}}]},
        "fixture": refund_snapshot(api),
        "scenario": {"timeout_after_commit": False, "max_tool_calls": 20},
        "actor_id": customer_id,
        "task": {"task_id": "probe", "params": {}},
        "cap": 20})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_adapter_id"] == "api-script"
    assert body["failed"] == []
    assert body["replay_equivalence_claimed"] is False


def test_minimize_script_target(stack):
    api = stack()
    response = api.post("/v1/extensions/minimize", json={
        "world_version": RESERVATION,
        "target": {"kind": "script", "actions": [
            {"tool": "reserve", "arguments": {"holder": "demo", "seats": 2}}]},
        "fixture": reservation_snapshot(api, "near_full"),
        "scenario_template": {"timeout_after_commit": False, "stale_snapshot": False,
                              "max_tool_calls": 20},
        "fault_keys": ["skip_capacity_check"], "actor_id": "console-probe",
        "task": {"task_id": "probe", "params": {}},
        "cap": 20, "invariant": "capacity_respected"})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "already_minimal"
    assert report["selected_faults"] == ["skip_capacity_check"]


def test_minimize_baseline_not_reproduced(stack):
    api = stack()
    order_id, customer_id = refund_ids(api)
    response = api.post("/v1/extensions/minimize", json={
        "world_version": REFUND,
        "target": {"kind": "registered", "id": "repaired"},
        "fixture": refund_snapshot(api),
        "scenario_template": {"max_tool_calls": 20},
        "fault_keys": ["timeout_after_commit"], "actor_id": customer_id,
        "task": {"task_id": "demo", "params": {"order_id": order_id,
                                               "customer_id": customer_id}},
        "cap": 20, "invariant": "total_refund_within_payment"})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["reason"] == "baseline_not_reproduced"
    assert "did not show" in body["error"]["message"]