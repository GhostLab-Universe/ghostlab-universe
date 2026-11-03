"""Experiment config endpoints over the M07 stack: real middleware and plane."""

from ghostlab.extensions.operator_registry import OperatorRegistry

RESERVATION = "reservation-world/1"
REF = "ghostlab.adapters.reservation_env:ReservationEnvironment"


def config_body(**overrides):
    body = {"config_version": 1, "name": "api-config", "world_version": RESERVATION,
            "target": {"kind": "script",
                       "actions": [{"tool": "reserve",
                                    "arguments": {"holder": "amy", "seats": 1}}]},
            "fixture": {"name": "half_full"}, "faults": [],
            "rules": ["capacity_respected"],
            "limits": {"max_tool_calls": 10, "max_total_tool_calls": 20,
                       "max_wall_clock_ms": 5000},
            "repetitions": {"count": 2}}
    body.update(overrides)
    return body


def register_only(api):
    root = api.lab.paths.graph_db.parent
    operators = OperatorRegistry(root / "operator_registry.sqlite")
    try:
        operators.get(RESERVATION)
        return operators
    except LookupError:
        pass
    operators.register(
        world_version=RESERVATION, environment_id="reservation-environment",
        environment_version="reservation-environment/1", adapter_ref=REF,
        display_name="Reservation demo",
        declared_tools=[{"name": "reserve", "parameters": ["holder", "seats"]}],
        declared_modes=["recorded-actions"], registered_by="operator-test")
    return operators

def qualify_world(operators):
    operators.record_qualification(
        world_version=RESERVATION,
        checks=[{"name": "api-test", "passed": True}], passed=True)


def register_and_qualify(api):
    qualify_world(register_only(api))


def create_and_resolve(api, body=None):
    created = api.post("/v1/extensions/exp-configs",
                       json={"config": body or config_body()})
    assert created.status_code == 200, created.text
    config_id = created.json()["config_id"]
    resolved = api.post(f"/v1/extensions/exp-configs/{config_id}/resolve",
                        json={})
    assert resolved.status_code == 200, resolved.text
    return config_id, resolved.json()


def test_policy_and_create_list(stack):
    api = stack()
    policy = api.get("/v1/extensions/exp-policy")
    assert policy.status_code == 200, policy.text
    body = policy.json()
    assert "script" in body["allowed_targets"]
    assert "skip_capacity_check" in body["allowed_hooks"]
    created = api.post("/v1/extensions/exp-configs",
                       json={"config": config_body()})
    assert created.status_code == 200, created.text
    record = created.json()
    assert record["config_id"].startswith("req-")
    assert record["requested_by"] == "operator-1"
    listing = api.get("/v1/extensions/exp-configs")
    assert listing.status_code == 200, listing.text
    assert [item["config_id"] for item in listing.json()["configs"]] == [
        record["config_id"]]
    fetched = api.get(f"/v1/extensions/exp-configs/{record['config_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["digest"] == record["digest"]


def test_create_rejects_invalid(stack):
    api = stack()
    bad = config_body()
    bad["extra"] = True
    response = api.post("/v1/extensions/exp-configs", json={"config": bad})
    assert response.status_code == 422, response.text
    bad = config_body(limits={"max_tool_calls": 5000,
                              "max_total_tool_calls": 200,
                              "max_wall_clock_ms": 5000})
    response = api.post("/v1/extensions/exp-configs", json={"config": bad})
    assert response.status_code == 422, response.text
    missing = api.get("/v1/extensions/exp-configs/req-0000000000000000")
    assert missing.status_code == 404, missing.text


def test_run_refused_until_qualified_then_passes(stack):
    api = stack()
    operators = register_only(api)
    _, resolved = create_and_resolve(api)
    assert resolved["resolved"]["provenance"]["qualification_status"] == \
        "not qualified"
    refused = api.post(
        f"/v1/extensions/exp-resolved/{resolved['resolved_id']}/run",
        json={"repetition": 1, "attempt": 1})
    assert refused.status_code == 422, refused.text
    runs = api.get("/v1/extensions/exp-runs")
    assert runs.status_code == 200, runs.text
    assert runs.json()["runs"] == []
    qualify_world(operators)
    _, qualified = create_and_resolve(api)
    assert qualified["resolved"]["provenance"]["qualification_status"] == \
        "qualified for declared capabilities"
    assert qualified["resolved_id"] != resolved["resolved_id"]
    ran = api.post(
        f"/v1/extensions/exp-resolved/{qualified['resolved_id']}/run",
        json={"repetition": 1, "attempt": 1})
    assert ran.status_code == 200, ran.text
    run = ran.json()["run"]
    assert run["status"] == "completed"
    assert run["failed_rules"] == []
    assert run["tool_calls"] == 1
    fetched = api.get(f"/v1/extensions/exp-runs/{ran.json()['run_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["run"]["evidence_digest"] == run["evidence_digest"]


def test_run_reports_violation_and_diff(stack):
    api = stack()
    register_and_qualify(api)
    body = config_body(
        target={"kind": "script", "actions": [{"tool": "reserve",
                                              "arguments": {"holder": "amy",
                                                          "seats": 9}}]},
        faults=[{"hook": "skip_capacity_check", "params": {},
                 "required": True}])
    _, resolved = create_and_resolve(api, body)
    ran = api.post(
        f"/v1/extensions/exp-resolved/{resolved['resolved_id']}/run",
        json={"repetition": 1, "attempt": 1})
    assert ran.status_code == 200, ran.text
    run = ran.json()["run"]
    assert run["failed_rules"] == ["capacity_respected"]
    assert run["faults_fired"] == ["skip_capacity_check"]
    _, other = create_and_resolve(api)
    compared = api.get(
        "/v1/extensions/exp-diff",
        params={"left_id": resolved["resolved_id"],
                "right_id": other["resolved_id"]})
    assert compared.status_code == 200, compared.text
    payload = compared.json()
    assert payload["equal"] is False
    assert "effective.scenario.skip_capacity_check" in [
        entry["path"] for entry in payload["diffs"]]


def test_cross_project_isolated(stack):
    api = stack()
    created = api.post("/v1/extensions/exp-configs",
                       json={"config": config_body()})
    assert created.status_code == 200, created.text
    config_id = created.json()["config_id"]
    foreign = api.get(f"/v1/extensions/exp-configs/{config_id}", who="outsider")
    assert foreign.status_code == 404, foreign.text
    listing = api.get("/v1/extensions/exp-configs", who="outsider")
    assert listing.status_code == 200, listing.text
    assert listing.json()["configs"] == []
