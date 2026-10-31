"""Findings from independent review of the control plane, each held by a test.

Inference is a labeled fixture (httpx.MockTransport). Everything else is the real code.
"""

import copy
import dataclasses
import json
import threading
import types
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from conftest import LOOPBACK, Api, build_app, build_lab
from ghostlab.api import jobs as jobs_module
from ghostlab.api.local import local_config
from ghostlab.api.rootlock import LOCK_NAME, RootLock, RootLocked, SplitStorage
from ghostlab.api.sessions import LocalDevSessions
from ghostlab.api.tracing import disable_trace_export, drop_provider_keys
from ghostlab.api.sessions import ApiSession
from ghostlab.api import create_app
from ghostlab.api.plane import ApiConfig
from ghostlab.contracts.world_refund import capsule_digest
from ghostlab.policy import ProjectApproverAllowlist
from ghostlab.providers import ProviderConfigError
from ghostlab.worlds.journey import JourneyFixture
from ghostlab.worlds.journey_capsule import capsule_digest as journey_digest
from m05_harness import OPERATOR, PROJECT, SECRET, Script, proposal
from m05_journey_harness import J, journey_reply

REFUND = "refund-world/1"


class Crash(BaseException):
    pass


class CrashOnce:
    def __init__(self, node):
        self.node, self.armed = node, True

    def __call__(self, name, state):
        if self.armed and name == self.node:
            self.armed = False
            raise Crash()


class Gate:
    def __init__(self, node):
        self.node, self.reached, self.release = node, threading.Event(), threading.Event()

    def __call__(self, name, state):
        if name == self.node and not self.release.is_set():
            self.reached.set()
            assert self.release.wait(30)


def checkpoint(api, campaign_id, **values):
    controller = api.client.app.state.plane._controller(
        ApiSession(OPERATOR, PROJECT, "configured_tokens").trusted)
    controller.graph.update_state(controller._config(campaign_id), values)


def stored_capsule(api, world, run="fork", **options):
    view = api.paused(world, **options) if not options.get("truncated") else None
    url = (f"/v1/campaigns/{view['record']['campaign_id']}/experiments/"
           f"{view['experiments'][0]['experiment_id']}")
    capsule_id = api.post(f"{url}/capsules/{run}").json()["artifact"]["id"]
    return json.loads(api.get(f"/v1/artifacts/{capsule_id}").content)


def truncated_capsule(api, world):
    campaign_id = api.create(world).json()["record"]["campaign_id"]
    view = api.wait(campaign_id, "budget_exhausted")
    url = f"/v1/campaigns/{campaign_id}/experiments/{view['experiments'][0]['experiment_id']}"
    capsule_id = api.post(f"{url}/capsules/fork").json()["artifact"]["id"]
    return json.loads(api.get(f"/v1/artifacts/{capsule_id}").content)


# -- P1: one server per root --------------------------------------------------------------

def test_a_second_server_on_a_root_is_refused_at_startup(tmp_path):
    lab = build_lab(tmp_path, Script())
    with TestClient(build_app(lab), **LOOPBACK):
        second = TestClient(build_app(lab), **LOOPBACK)
        with pytest.raises(RootLocked):
            second.__enter__()
    with TestClient(build_app(lab), **LOOPBACK) as client:  # released when it stopped
        assert client.get("/v1/session", headers={"Authorization": "Bearer operator"}
                          ).status_code == 200


def test_the_lock_is_on_the_storage_root(tmp_path):
    with RootLock(tmp_path):
        assert (tmp_path / LOCK_NAME).exists()
        with pytest.raises(RootLocked):
            RootLock(tmp_path).__enter__()
    with RootLock(tmp_path):  # the leftover file means nothing by itself
        pass


# -- P2: shutdown is not a cancel ------------------------------------------------------------

def test_a_shutdown_writes_no_outcome_nobody_asked_for(tmp_path, monkeypatch):
    # Long enough that a worker ignoring the stop signal would reach the approval pause.
    monkeypatch.setattr(jobs_module, "SHUTDOWN_GRACE_S", 1.0)
    holder = {}

    def until_shutdown(name, state):
        if name == "execute" and "plane" in holder:
            holder["reached"].set()
            assert holder["plane"].jobs.stopping.wait(30)

    lab = build_lab(tmp_path, Script(proposal()), on_node=until_shutdown)
    with TestClient(build_app(lab), **LOOPBACK) as client:
        api = Api(client, lab)
        holder.update(plane=client.app.state.plane, reached=threading.Event())
        campaign_id = api.create().json()["record"]["campaign_id"]
        assert holder["reached"].wait(30)
        worker = next(iter(holder["plane"].jobs._threads.values()))
    worker.join(30)
    assert not worker.is_alive()
    holder.clear()
    with TestClient(build_app(lab), **LOOPBACK) as client:
        api = Api(client, lab)
        view = api.view(campaign_id)
        assert view["record"]["status"] == "running" and view["outcome"] is None
        assert view["job"] == {"state": "stalled", "cancel_requested": False, "error": None}
        events = api.get(f"/v1/campaigns/{campaign_id}/events").json()["events"]
        assert not any("cancel" in event["summary"].lower() for event in events)
        assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
        assert api.wait(campaign_id, "awaiting_approval")["job"]["state"] == "idle"


def test_shutdown_never_sets_the_owners_cancel():
    seen = threading.Event()
    registry = jobs_module.JobRegistry(limit=1, on_failure=lambda *_: None)
    job, _ = registry.submit("k", lambda job: seen.wait(5))
    original, jobs_module.SHUTDOWN_GRACE_S = jobs_module.SHUTDOWN_GRACE_S, 0.01
    try:
        registry.shutdown()
    finally:
        jobs_module.SHUTDOWN_GRACE_S = original
        seen.set()
    assert registry.stopping.is_set() and not job.cancel.is_set()


# -- P2: cancel says what happened -----------------------------------------------------------

def stranded(api):
    """A campaign whose interrupted node can't finish: its evidence is gone."""
    campaign_id = api.create().json()["record"]["campaign_id"]
    assert api.wait(campaign_id, "running")["job"]["state"] == "stalled"
    for blob in [path for path in api.lab.paths.artifacts.rglob("*") if path.is_file()]:
        blob.unlink()
    return campaign_id


def test_cancel_that_cannot_stop_a_campaign_says_so(stack):
    api = stack(proposal(), lab_options={"on_node": CrashOnce("verify")})
    campaign_id = stranded(api)
    for _ in range(2):
        response = api.post(f"/v1/campaigns/{campaign_id}/cancel")
        assert response.status_code == 409
        assert response.json()["reason"] == "cancel_incomplete"
    view = api.view(campaign_id)
    assert view["record"]["status"] == "running" and view["outcome"] is None
    assert view["job"]["state"] == "stalled"
    assert view["job"]["error"]["message"].startswith("Cancellation did not complete")


def test_a_worker_failing_after_cancel_does_not_read_as_cancelled(stack):
    gate = Gate("verify")

    def fail_after(name, state):
        gate(name, state)
        if name == "verify":
            raise RuntimeError("the step failed")

    api = stack(proposal(), lab_options={"on_node": fail_after})
    campaign_id = api.create().json()["record"]["campaign_id"]
    assert gate.reached.wait(30)
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").json()["job"]["cancel_requested"]
    gate.release.set()
    view = api.wait(campaign_id, "running")
    assert view["job"]["state"] == "stalled"
    assert view["job"]["error"]["message"].startswith("Cancellation did not complete")


def test_cancel_is_not_refused_for_capacity(stack):
    gate = Gate("execute")
    gate.release.set()  # open until the second campaign

    api = stack(proposal(), proposal(), lab_options={"on_node": gate}, max_active_campaigns=1)
    paused = api.paused(key="one")
    gate.release.clear()
    assert api.create(key="two").status_code == 202
    assert gate.reached.wait(30)
    campaign_id = paused["record"]["campaign_id"]
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202
    assert api.wait(campaign_id, "cancelled")["outcome"] == "cancelled_by_request"
    gate.release.set()


# -- P2: a capsule can't claim what its adapter doesn't do -------------------------------------

@pytest.mark.parametrize("world,replies,digest", [
    (REFUND, (proposal(max_tool_calls=2), proposal(max_tool_calls=1)), capsule_digest),
    (J, (journey_reply(cap=3), journey_reply(cap=4)), journey_digest)])
def test_a_truncated_run_relabelled_completed_is_refused(stack, world, replies, digest):
    api = stack(*replies)
    payload = truncated_capsule(api, world)["payload"]
    assert payload["run"]["status"] == "budget_exhausted"
    payload["run"]["status"] = "completed"
    forged = api.post("/v1/capsules/import", json={"payload": payload, "sha256": digest(payload)})
    assert forged.status_code == 422
    assert forged.json()["reason"] == "capsule_invalid"


@pytest.mark.parametrize("world,replies,digest", [
    (REFUND, (proposal(max_tool_calls=2), proposal(max_tool_calls=1)), capsule_digest),
    (J, (journey_reply(cap=3), journey_reply(cap=4)), journey_digest)])
def test_raising_the_cap_cannot_pass_off_a_truncated_run(stack, world, replies, digest):
    api = stack(*replies)
    payload = truncated_capsule(api, world)["payload"]
    payload["run"]["status"] = "completed"
    payload["scenario"]["max_tool_calls"] = 20
    forged = api.post("/v1/capsules/import", json={"payload": payload, "sha256": digest(payload)})
    assert forged.status_code == 422
    assert forged.json()["reason"] == "capsule_invalid"


def test_an_honest_truncated_capsule_keeps_its_status(stack):
    api = stack(proposal(max_tool_calls=2), proposal(max_tool_calls=1))
    imported = api.post("/v1/capsules/import", json=truncated_capsule(api, REFUND))
    assert imported.status_code == 201, imported.text
    assert imported.json()["replay"]["run_status"] == "budget_exhausted"


def test_a_relabelled_adapter_is_refused(stack):
    api = stack(journey_reply())
    payload = stored_capsule(api, J)["payload"]
    payload["run"]["adapter_id"] = "journey-version-checked"
    forged = api.post("/v1/capsules/import",
                      json={"payload": payload, "sha256": journey_digest(payload)})
    assert forged.status_code == 422
    assert forged.json()["reason"] == "capsule_invalid"


# -- P2: secrets through import ---------------------------------------------------------------

@pytest.mark.parametrize("output", [
    {"note": f"key {SECRET}"}, {"api_key": "short-value"}, "Bearer abcdefghijkl", ["token=abcd1234"]])
def test_a_secret_in_a_capsule_is_refused_and_not_stored(stack, output):
    api = stack(proposal())
    capsule = stored_capsule(api, REFUND)
    payload = copy.deepcopy(capsule["payload"])
    payload["recorded_model_outputs"] = [output]
    before = {path for path in api.lab.paths.artifacts.rglob("*") if path.is_file()}
    response = api.post("/v1/capsules/import",
                        json={"payload": payload, "sha256": capsule_digest(payload)})
    assert response.status_code == 422
    assert response.json()["reason"] == "secret_shaped_text"
    assert SECRET not in response.text
    assert {path for path in api.lab.paths.artifacts.rglob("*") if path.is_file()} == before


# -- P3 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("failure", [ProviderConfigError("no key"), RuntimeError("unexpected")])
def test_a_recorded_decision_never_reads_as_failed(stack, failure):
    api = stack(proposal())
    paused = api.paused()
    plane = api.client.app.state.plane

    def broken(runtime):
        raise failure

    plane.services = dataclasses.replace(plane.services, adapter_factory=broken)
    decided = api.decide(paused)
    assert decided.status_code == 200
    assert decided.json()["decision"]["decision"] == "approve"
    assert decided.json()["resumed"] is False
    assert decided.json()["resume_error"]["error"]["code"] in {"forbidden", "internal"}
    assert api.decide(paused).headers["Idempotency-Replayed"] == "true"


@pytest.mark.parametrize("decision,outcome", [("approve", "repair_verified"),
                                              ("reject", "repair_rejected")])
def test_a_retried_decision_reports_the_same_result(stack, decision, outcome):
    api = stack(proposal())
    paused = api.paused()
    first = api.decide(paused, decision, key="same")
    assert api.wait(paused["record"]["campaign_id"], "completed")["outcome"] == outcome
    retry = api.decide(paused, decision, key="same")
    assert (first.json()["resumed"], first.json()["resume_error"]) == (True, None)
    assert (retry.json()["resumed"], retry.json()["resume_error"]) == (True, None)


def test_an_approved_approval_past_expiry_reads_as_expired(stack):
    api = stack(journey_reply())
    paused = api.paused(J)
    plane = api.client.app.state.plane
    original = plane.services
    plane.services = dataclasses.replace(original, journey_fixture=JourneyFixture(
        journey_id="journey-changed"))
    assert api.decide(paused).json()["resumed"] is False
    plane.services = original
    api.lab.clock.advance(hours=2)
    view = api.get(f"/v1/approvals/{paused['approval']['approval_id']}").json()
    assert view["request"]["status"] == "approved" and view["effective_status"] == "expired"


def test_can_approve_matches_what_decide_accepts(stack):
    approvers = ProjectApproverAllowlist({PROJECT: {"reviewer-1", "service-1"}})
    api = stack(proposal(), approvers=approvers)
    api.lab.approvals._approvers = approvers
    assert api.get("/v1/session", who="service").json()["can_approve"] is False
    assert api.decide(api.paused(), who="service").status_code == 403


def test_a_validation_error_does_not_repeat_an_unknown_field_name(stack):
    api = stack()
    response = api.post("/v1/campaigns", key="k", json={"schema_version": 1, "private-name": 1})
    assert response.status_code == 422
    assert "private-name" not in response.text


# -- test gaps --------------------------------------------------------------------------------

def test_a_decision_during_the_pause_window_runs_the_repair_through_http(stack):
    gate = Gate("await_approval")
    api = stack(proposal(), lab_options={"on_node": gate})
    campaign_id = api.create().json()["record"]["campaign_id"]
    assert gate.reached.wait(30)
    view = api.view(campaign_id)
    assert view["record"]["status"] == "awaiting_approval" and view["job"]["state"] == "active"
    assert api.decide(view).json()["resumed"] is True
    gate.release.set()
    assert api.wait(campaign_id, "completed")["outcome"] == "repair_verified"


def test_cancel_during_the_pause_window_still_ends_the_campaign(stack):
    gate = Gate("await_approval")
    api = stack(proposal(), lab_options={"on_node": gate})
    campaign_id = api.create().json()["record"]["campaign_id"]
    assert gate.reached.wait(30)
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202
    gate.release.set()
    assert api.wait(campaign_id, "cancelled")["outcome"] == "cancelled_by_request"


def test_an_action_that_no_longer_matches_its_digest_is_not_shown(stack):
    api = stack(proposal())
    paused = api.paused()
    url = f"/v1/approvals/{paused['approval']['approval_id']}"
    assert api.get(url).json()["action"] is not None
    state = api.client.app.state.plane._controller(
        ApiSession(OPERATOR, PROJECT, "configured_tokens").trusted).state(
            paused["record"]["campaign_id"])
    changed = {**state["approval"], "action": {**state["approval"]["action"], "adapter": "other"}}
    checkpoint(api, paused["record"]["campaign_id"], approval=changed)
    assert api.get(url).json()["action"] is None


# -- second review ----------------------------------------------------------------------------

def test_a_graph_file_outside_the_locked_root_is_refused(tmp_path):
    lab = build_lab(tmp_path / "a", Script())
    for graph_db, app_db in ((tmp_path / "b" / "graph.sqlite", lab.paths.app_db),
                             (lab.paths.graph_db, tmp_path / "b" / "app.sqlite"),
                             (lab.paths.graph_db, None)):
        with pytest.raises(SplitStorage):
            create_app(ApiConfig(services=lab.services(), sessions=None, approvers=None,
                                 graph_db=graph_db, app_db=app_db))


def test_services_over_another_database_are_refused(tmp_path):
    lab, other = build_lab(tmp_path / "a", Script()), build_lab(tmp_path / "b", Script())
    services = dataclasses.replace(lab.services(), approval_store=other.approval_store)
    with pytest.raises(SplitStorage):
        create_app(ApiConfig(services=services, sessions=None, approvers=None,
                             graph_db=lab.paths.graph_db, app_db=lab.paths.app_db))


def test_trace_export_is_forced_off_and_its_key_dropped():
    environ = {"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true",
               "LANGSMITH_API_KEY": "key", "LANGCHAIN_API_KEY": "key", "OTHER": "kept"}
    disable_trace_export(environ)
    assert environ.pop("OTHER") == "kept"
    assert set(environ.values()) == {"false"} and "LANGSMITH_API_KEY" not in environ
    keys = {"MODEL_API_KEY": "key", "BASETEN_API_KEY": "key", "OTHER": "kept"}
    drop_provider_keys(keys)
    assert keys == {"OTHER": "kept"}


def test_importing_the_api_turns_tracing_off_in_this_process():
    import os

    import langsmith.utils

    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert langsmith.utils.tracing_is_enabled() is False


@pytest.mark.parametrize("place", ["recorded_model_outputs", "final_state", "deep"])
def test_a_secret_in_a_dict_key_is_refused_at_any_depth(stack, place):
    api = stack(proposal())
    payload = copy.deepcopy(stored_capsule(api, REFUND)["payload"])
    named = {SECRET: 1}
    if place == "recorded_model_outputs":
        payload["recorded_model_outputs"] = [named]
    elif place == "final_state":
        payload["run"]["final_state"]["order"] = named
    else:
        nested = named
        for _ in range(40):
            nested = {"a": [nested]}
        payload["recorded_model_outputs"] = [nested]
    before = {path for path in api.lab.paths.artifacts.rglob("*") if path.is_file()}
    response = api.post("/v1/capsules/import",
                        json={"payload": payload, "sha256": capsule_digest(payload)})
    assert response.status_code == 422
    if place != "final_state":  # that one is also a contract violation, refused earlier
        assert response.json()["reason"] == "secret_shaped_text"
    assert SECRET not in response.text
    assert {path for path in api.lab.paths.artifacts.rglob("*") if path.is_file()} == before


def test_a_secret_in_a_journey_state_key_is_refused(stack):
    api = stack(journey_reply())
    payload = copy.deepcopy(stored_capsule(api, J)["payload"])
    payload["run"]["final_state"][SECRET] = 1
    response = api.post("/v1/capsules/import",
                        json={"payload": payload, "sha256": journey_digest(payload)})
    assert response.status_code == 422
    assert response.json()["reason"] == "secret_shaped_text"


def scripted_app(tmp_path, *, on_node=None):
    environ = {"GHOSTLAB_AUTH_MODE": "local_dev"}
    sessions = LocalDevSessions.from_environment("127.0.0.1", environ=environ)
    config = local_config(tmp_path / "root", sessions, bind_host="127.0.0.1", scripted=True,
                          environ=environ)
    if on_node is not None:
        config = dataclasses.replace(
            config, services=dataclasses.replace(config.services, on_node=on_node))
    return create_app(config)


class Scripted:
    """A scripted server with no bearer tokens: the local-dev session."""

    def __init__(self, client):
        self.api = Api(client, types.SimpleNamespace(script=None))

    def create(self, world, objective, key="k", **params):
        body = {"schema_version": 1, "objective": objective, "success_criteria": ["Found"],
                "world_version": world,
                "budget": {"max_model_calls": 10, "max_tool_calls": 100,
                           "max_input_tokens": 100_000, "max_output_tokens": 20_000,
                           "max_cost_minor": None, "cost_currency": None,
                           "max_wall_clock_ms": 1_200_000, "max_parallel_runs": 2}}
        return self.api.post("/v1/campaigns", who=None, key=key, json=body, params=params)


@pytest.mark.parametrize("objective", [
    "Check group-journey handling", "Already explored: everything", "group-journey/1 Already explored"])
def test_words_in_an_objective_cannot_steer_the_scripted_reply(tmp_path, objective):
    with TestClient(scripted_app(tmp_path), **LOOPBACK) as client:
        scripted = Scripted(client)
        created = scripted.create(REFUND, objective)
        assert created.status_code == 202, created.text
        view = scripted.api.wait(created.json()["record"]["campaign_id"], "awaiting_approval",
                                 who=None)
        assert view["experiments"][0]["adapter_id"] == "vulnerable"
        assert view["inference"] == "scripted"


def test_a_scenario_survives_a_crash_mid_proposal(tmp_path):
    crash = CrashOnce("propose")
    with TestClient(scripted_app(tmp_path, on_node=crash), **LOOPBACK) as client:
        scripted = Scripted(client)
        campaign_id = scripted.create(J, "Find a leak", scripted_scenario="fault_never_fires"
                                      ).json()["record"]["campaign_id"]
        assert scripted.api.wait(campaign_id, "running", who=None)["job"]["state"] == "stalled"
        assert scripted.api.post(f"/v1/campaigns/{campaign_id}/resume", who=None).status_code == 202
        view = scripted.api.wait(campaign_id, "budget_exhausted", who=None)
        assert view["outcome"] == "coverage_incomplete"
        assert all(e["adapter_id"] == "journey-version-checked" for e in view["experiments"])


def test_a_key_reused_with_another_scenario_is_refused(tmp_path):
    with TestClient(scripted_app(tmp_path), **LOOPBACK) as client:
        scripted = Scripted(client)
        first = scripted.create(J, "Find a leak", scripted_scenario="revocation_race")
        scripted.api.wait(first.json()["record"]["campaign_id"], "awaiting_approval", who=None)
        other = scripted.create(J, "Find a leak", scripted_scenario="fault_never_fires")
        assert other.status_code == 409
        assert other.json()["reason"] == "idempotency_key_reused"
        retry = scripted.create(J, "Find a leak", scripted_scenario="revocation_race")
        assert retry.headers["Idempotency-Replayed"] == "true"


def test_a_campaign_label_does_not_follow_the_reading_server(stack, monkeypatch):
    api = stack(proposal())
    paused = api.paused()
    assert paused["inference"] == "fixture"
    plane = api.client.app.state.plane
    plane._origins.clear()
    monkeypatch.setattr(plane, "_inference", lambda: "live")
    assert api.view(paused["record"]["campaign_id"])["inference"] == "fixture"
    assert api.get("/v1/session").json()["inference"] == "live"


def test_a_campaign_with_no_pin_reads_as_unrecorded_never_as_this_server(stack, monkeypatch):
    api = stack(proposal())
    paused = api.paused()
    plane = api.client.app.state.plane
    monkeypatch.setattr(plane, "_origin", lambda session, campaign_id: None)
    assert api.view(paused["record"]["campaign_id"])["inference"] == "unrecorded"


def test_a_refund_v2_capsule_with_no_v1_form_is_refused_not_a_server_error(stack):
    from ghostlab.contracts.world_refund import capsule_v1_to_v2

    api = stack(proposal())
    capsule = capsule_v1_to_v2(stored_capsule(api, REFUND)).model_dump(mode="json")
    payload = capsule["payload"]
    payload["scenario"]["faults"][0]["after_event"] = 1  # refund v1 has no custom timing
    response = api.post("/v1/capsules/import",
                        json={"payload": payload, "sha256": capsule_digest(payload)})
    assert response.status_code == 422
    assert response.json()["reason"] == "capsule_invalid"


def test_a_status_replay_could_not_check_is_not_reported(stack):
    api = stack(proposal())
    payload = stored_capsule(api, REFUND)["payload"]
    payload["run"]["final_state"]["total_refunded_minor"] = 1
    imported = api.post("/v1/capsules/import",
                        json={"payload": payload, "sha256": capsule_digest(payload)})
    assert imported.status_code == 201, imported.text
    assert imported.json()["replay"]["reproduced"] is False
    assert imported.json()["replay"]["run_status"] is None


@pytest.mark.parametrize("scripted,held", [(True, set()), (False, {"MODEL_API_KEY"})])
def test_a_scripted_server_holds_no_provider_key(tmp_path, monkeypatch, scripted, held):
    import os

    from ghostlab.api import __main__ as entry

    monkeypatch.setenv("GHOSTLAB_AUTH_MODE", "local_dev")
    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key")
    monkeypatch.setenv("BASETEN_API_KEY", "not-a-real-key")
    monkeypatch.delenv("BASETEN_API_KEY" if not scripted else "GHOSTLAB_DEPLOYMENT", raising=False)
    seen = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **_: seen.update(
        keys={name for name in ("MODEL_API_KEY", "BASETEN_API_KEY") if name in os.environ}))
    args = ["--root", str(tmp_path / "root"), "--port", "1", *(["--scripted"] if scripted else [])]
    assert entry.main(args) == 0
    assert seen["keys"] == held
