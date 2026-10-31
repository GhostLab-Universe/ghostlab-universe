"""Unfinished and refused outcomes from M05 reach clients as themselves, never as success.

Inference is a labeled fixture (httpx.MockTransport). Worlds, verifier, approvals, stores
and capsules are the real code.
"""

import sqlite3

import pytest

from conftest import spec_json
from ghostlab.api.jobs import Job
from ghostlab.api.rootlock import LOCK_NAME
from ghostlab.api.sessions import ApiSession, TokenSessions
from ghostlab.budgets import BudgetAccountant
from ghostlab.orchestration.worlds import DEFAULT_WORLDS, JourneyDriver
from ghostlab.security.redaction import Redactor
from ghostlab.worlds.journey import JourneyFixture, JourneyOption
from m05_harness import OPERATOR, PROJECT, SECRET, proposal
from m05_journey_harness import J, journey_reply

REFUND = "refund-world/1"
REVOKE_B = [{"fault_id": "stale_preference_memory", "after_event": None,
             "parameters": [{"name": "preference_version", "value": 1}]}]


def experiment_url(view):
    return (f"/v1/campaigns/{view['record']['campaign_id']}/experiments/"
            f"{view['experiments'][0]['experiment_id']}")


def assert_no_success(view):
    assert view["record"]["status"] != "completed"
    assert view["outcome"] not in {"repair_verified", "no_failure_found"}
    assert not (view["repair"] or {}).get("verified")
    assert (view["repair"] or {}).get("held_by") is None


def checkpoint(api, campaign_id, **values):
    """Change a stored checkpoint directly, as a damaged or older record would look."""
    plane = api.client.app.state.plane
    controller = plane._controller(ApiSession(OPERATOR, PROJECT, "configured_tokens").trusted)
    controller.graph.update_state(controller._config(campaign_id), values)


# -- truncated experiments -----------------------------------------------------------------

@pytest.mark.parametrize("world,replies", [
    (REFUND, (proposal(max_tool_calls=2), proposal(max_tool_calls=1))),
    (J, (journey_reply(cap=3), journey_reply(cap=4)))])
def test_a_truncated_experiment_is_inconclusive_in_every_view(stack, world, replies):
    api = stack(*replies)
    campaign_id = api.create(world).json()["record"]["campaign_id"]
    view = api.wait(campaign_id, "budget_exhausted")
    assert view["outcome"] == "coverage_incomplete"
    assert_no_success(view)
    assert view["errors"][-1]["code"] == "budget_exhausted"
    coverage = view["experiments"][0]["coverage"]
    assert coverage["complete"] is False and coverage["fork"] == "budget_exhausted"

    url = experiment_url(view)
    assert api.get(url).json()["coverage"] == coverage
    compared = api.get(f"{url}/diff").json()
    assert compared["coverage"] == coverage  # no violation here is not a clean result
    assert compared["diff"]["violated_invariants"] == []
    assert api.get(f"{url}/runs/fork").json()["result"]["status"] == "budget_exhausted"

    capsule_id = api.post(f"{url}/capsules/fork").json()["artifact"]["id"]
    for path in (f"{url}/capsules/fork/replay", f"/v1/capsules/{capsule_id}/replay"):
        replayed = api.post(path).json()
        assert replayed["reproduced"] is True  # the replay matched what was recorded
        assert replayed["run_status"] == "budget_exhausted"  # and that was cut short


def test_a_violation_seen_before_truncation_still_goes_to_a_human(stack):
    faults = [{"fault_id": "sharing_revoked_while_queued", "after_event": 2,
               "parameters": [{"name": "recipient_id", "value": "traveller-a"}]}]
    api = stack(journey_reply(faults=faults, cap=11))
    paused = api.paused(J)
    assert paused["experiments"][0]["coverage"]["complete"] is False
    run = api.get(f"{experiment_url(paused)}/runs/fork").json()
    assert [f["evidence_event_ids"] for f in run["findings"] if f["verdict"] == "failed"] == [[10]]


def test_one_complete_experiment_does_not_hide_a_truncated_one(stack):
    api = stack(journey_reply("journey-version-checked", cap=3),
                journey_reply("journey-version-checked", cap=20))
    campaign_id = api.create(J).json()["record"]["campaign_id"]
    view = api.wait(campaign_id, "budget_exhausted")
    assert view["outcome"] == "coverage_incomplete"
    assert sorted(e["coverage"]["complete"] for e in view["experiments"]) == [False, True]


def test_a_fault_that_never_fired_proves_nothing(stack):
    timeout_free = (proposal(timeout_after_commit=False),
                    proposal(timeout_after_commit=False, max_tool_calls=19))
    api = stack(*timeout_free)
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    view = api.wait(campaign_id, "budget_exhausted")
    assert view["outcome"] == "coverage_incomplete"
    assert_no_success(view)
    assert view["experiments"][0]["coverage"] == {
        "baseline": "completed", "fork": "completed", "faults_triggered": False,
        "complete": False}


def test_no_experiment_ran_is_a_failure_not_a_clean_result(stack):
    api = stack(proposal(max_tool_calls=21), proposal(max_tool_calls=22))
    campaign_id = api.create(REFUND).json()["record"]["campaign_id"]
    view = api.wait(campaign_id, "failed")
    assert view["outcome"] == "no_experiment_ran"
    assert_no_success(view)
    assert view["experiments"] == [] and view["errors"][-1]["code"] == "validation_failed"


def test_a_failed_budget_check_is_not_reported_as_exhaustion(stack, monkeypatch):
    def broken(*_, **__):
        raise sqlite3.OperationalError("disk I/O error")

    api = stack(journey_reply())
    monkeypatch.setattr(BudgetAccountant, "reserve", broken)
    campaign_id = api.create(J).json()["record"]["campaign_id"]
    view = api.wait(campaign_id, "failed")
    assert view["outcome"] == "budget_check_failed"
    assert_no_success(view)
    assert view["errors"][-1]["code"] == "internal"
    assert view["job"]["state"] == "idle"


def test_ids_too_long_to_derive_are_refused_before_anything_exists(stack):
    long_project = "p" * 100
    tokens = {"operator": (OPERATOR, long_project)}
    api = stack(proposal(), sessions=TokenSessions(tokens))
    response = api.create(REFUND)
    assert response.status_code == 422
    assert response.json()["reason"] == "identifier_too_long"
    assert api.script.requests == []


# -- repairs that didn't verify ---------------------------------------------------------------

class CappedRepair(JourneyDriver):
    """Gives the repair too few calls; the checked-in repair action has its own cap."""

    def repair_action(self, experiment_id, proposal):
        action = super().repair_action(experiment_id, proposal)
        return {**action, "scenario": {**action["scenario"], "max_tool_calls": 12}}


def test_a_repair_that_missed_a_fault_the_fork_met_is_not_verified(stack):
    faults = [{"fault_id": "delayed_itinerary_update", "after_event": None,
               "parameters": [{"name": "delay_events", "value": 3}]},
              {"fault_id": "sharing_revoked_while_queued", "after_event": 11,
               "parameters": [{"name": "recipient_id", "value": "traveller-a"}]}]
    api = stack(journey_reply(faults=faults))
    paused = api.paused(J)
    assert paused["experiments"][0]["coverage"]["faults_triggered"] is True
    api.decide(paused)
    view = api.wait(paused["record"]["campaign_id"], "failed")
    assert view["outcome"] == "repair_incomplete"
    assert_no_success(view)
    assert view["repair"]["lost_faults"] == ["sharing_revoked_while_queued"]


def test_a_truncated_repair_is_incomplete_not_verified(stack):
    api = stack(journey_reply(cap=12),
                lab_options={"worlds": {**DEFAULT_WORLDS, J: CappedRepair()}})
    paused = api.paused(J)
    assert api.decide(paused).json()["resumed"] is True
    view = api.wait(paused["record"]["campaign_id"], "budget_exhausted")
    assert view["outcome"] == "repair_incomplete"
    assert_no_success(view)
    repair = view["repair"]
    assert repair["verified"] is False and repair["run_status"] == "budget_exhausted"
    assert repair["safety_held"] is True  # safe so far, which is not the same as repaired
    assert repair["task_complete"] is False
    assert False in repair["task_outcome"].values()
    assert all(type(value) is bool for value in repair["task_outcome"].values())

    url = experiment_url(view)
    run = api.get(f"{url}/runs/repaired").json()
    assert run["result"]["status"] == "budget_exhausted"  # the evidence is kept
    api.post(f"{url}/capsules/repaired")
    assert api.post(f"{url}/capsules/repaired/replay").json()["run_status"] == "budget_exhausted"


def test_a_repair_that_did_nothing_fails_as_incomplete(stack):
    fixture = JourneyFixture(options=(JourneyOption("return-1830", 1110, 1190, 2),))
    api = stack(journey_reply(faults=REVOKE_B), lab_options={"journey_fixture": fixture})
    paused = api.paused(J)
    api.decide(paused)
    view = api.wait(paused["record"]["campaign_id"], "failed")
    assert view["outcome"] == "repair_incomplete"
    assert_no_success(view)
    assert view["repair"]["run_status"] == "completed"  # finished, and still proves nothing
    assert view["repair"]["safety_held"] is True and view["repair"]["verified"] is False


def test_a_verified_repair_can_leave_its_task_unfinished_and_says_so(stack):
    faults = [{"fault_id": "sharing_revoked_while_queued", "after_event": None,
               "parameters": [{"name": "recipient_id", "value": "traveller-b"}]}]
    api = stack(journey_reply(faults=faults))
    paused = api.paused(J)
    api.decide(paused)
    view = api.wait(paused["record"]["campaign_id"], "completed")
    assert view["outcome"] == "repair_verified"
    assert view["repair"]["task_complete"] is False
    assert view["repair"]["task_outcome"]["mandatory_informed"] is False


def test_a_view_takes_status_and_outcome_from_one_read(stack, monkeypatch):
    from ghostlab.orchestration import CampaignController

    api = stack(proposal())
    paused = api.paused()
    record = CampaignController.record

    def newer(self, campaign):  # a worker finished between the two reads
        data = record(self, campaign).model_dump(mode="json")
        return type(record(self, campaign)).model_validate({**data, "status": "completed"})

    monkeypatch.setattr(CampaignController, "record", newer)
    view = api.view(paused["record"]["campaign_id"])
    assert view["record"]["status"] == "awaiting_approval" and view["outcome"] is None


def test_a_worker_ending_during_a_read_never_reads_as_stalled(stack, monkeypatch):
    from ghostlab.orchestration import CampaignController

    api = stack(proposal())
    campaign_id = api.paused()["record"]["campaign_id"]
    jobs = api.client.app.state.plane.jobs
    monkeypatch.setattr(jobs, "get", lambda key: Job())  # a worker is running
    read = CampaignController.state

    def worker_ends_now(self, campaign):
        monkeypatch.setattr(jobs, "get", lambda key: None)
        return {**read(self, campaign), "status": "running", "paused": True}

    monkeypatch.setattr(CampaignController, "state", worker_ends_now)
    assert api.view(campaign_id)["job"]["state"] == "active"


# -- the pinned fixture ------------------------------------------------------------------------

class SwitchingDriver(JourneyDriver):
    """The world's current fixture changes at a moment a test chooses."""

    changed = False

    def current_fixture(self, services):
        return JourneyFixture(journey_id="journey-changed") if self.changed \
            else super().current_fixture(services)


def test_the_campaign_view_carries_the_fixture_digest_the_approval_binds(stack):
    api = stack(journey_reply())
    paused = api.paused(J)
    assert len(paused["fixture_sha256"]) == 64
    approval = api.get(f"/v1/approvals/{paused['approval']['approval_id']}").json()
    assert approval["action"]["fixture_sha256"] == paused["fixture_sha256"]
    assert "fixture" not in paused  # the fixture itself is in the capsule


def test_settings_changed_at_consumption_ends_the_campaign_and_keeps_the_approval(stack):
    driver = SwitchingDriver()

    def change(name, state):
        if name == "consume":
            driver.changed = True

    api = stack(journey_reply(), lab_options={
        "worlds": {**DEFAULT_WORLDS, J: driver}, "on_node": change})
    paused = api.paused(J)
    assert api.decide(paused).json()["resumed"] is True
    view = api.wait(paused["record"]["campaign_id"], "failed")
    assert view["outcome"] == "settings_changed"
    assert_no_success(view)
    assert view["repair"] is None
    approval = api.get(f"/v1/approvals/{paused['approval']['approval_id']}").json()
    assert approval["request"]["status"] == "approved"  # not spent


@pytest.mark.parametrize("damage,message", [
    ({"fixture_sha256": "0" * 64}, "digest"), ({"fixture": None, "fixture_sha256": None},
                                               "no recorded fixture")])
def test_a_damaged_fixture_record_is_a_typed_refusal(stack, damage, message):
    api = stack(journey_reply())
    paused = api.paused(J)
    campaign_id = paused["record"]["campaign_id"]
    checkpoint(api, campaign_id, **damage)

    export = api.post(f"{experiment_url(paused)}/capsules/fork")
    assert export.status_code == 409
    assert export.json()["reason"] == "settings_changed"
    assert message in export.json()["error"]["message"]

    resumed = api.post(f"/v1/campaigns/{campaign_id}/resume")
    assert resumed.status_code == 409
    assert resumed.json()["reason"] == "settings_changed"

    decided = api.decide(paused)
    assert decided.status_code == 200
    assert decided.json()["resumed"] is False
    assert decided.json()["resume_error"]["reason"] == "settings_changed"

    view = api.view(campaign_id)  # reading a damaged campaign still works
    assert view["record"]["status"] == "awaiting_approval" and view["repair"] is None
    assert view["job"]["state"] == "stalled"


def test_an_older_refund_checkpoint_still_reads_without_a_digest(stack):
    api = stack(proposal())
    paused = api.paused(REFUND)
    campaign_id = paused["record"]["campaign_id"]
    checkpoint(api, campaign_id, fixture=None, fixture_sha256=None)
    assert api.view(campaign_id)["fixture_sha256"] is None
    api.decide(paused)
    assert api.wait(campaign_id, "completed")["outcome"] == "repair_verified"


def test_an_older_experiment_with_no_coverage_reads_as_not_recorded(stack):
    api = stack(proposal())
    paused = api.paused(REFUND)
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    plane = api.client.app.state.plane
    state = plane._controller(ApiSession(OPERATOR, PROJECT, "configured_tokens").trusted).state(
        campaign_id)
    experiment = {key: value for key, value in state["experiments"][experiment_id].items()
                  if key != "coverage"}
    checkpoint(api, campaign_id, experiments={experiment_id: experiment})
    assert api.view(campaign_id)["experiments"][0]["coverage"] is None
    assert api.get(f"{experiment_url(paused)}/diff").json()["coverage"] is None


# -- client text is M07's to keep clean --------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"objective": f"Use {SECRET} to reach the refund"},
    {"objective": "Retry with Bearer abcdefgh12345678"},
    {"objective": "password=hunter2hunter2"},
    {"success_criteria": ["Fine", f"Also {SECRET}"]}])
def test_a_credential_in_a_spec_is_refused_before_anything_is_stored(stack, change):
    api = stack(proposal())
    response = api.post("/v1/campaigns", key="k", json={**spec_json(), **change})
    assert response.status_code == 422
    assert response.json()["reason"] == "secret_shaped_text"
    assert SECRET not in response.text and "hunter2" not in response.text
    assert api.script.requests == []
    assert api.client.app.state.plane.jobs._jobs == {}


def test_a_credential_in_a_decision_reason_is_refused_and_records_nothing(stack):
    api = stack(proposal())
    paused = api.paused()
    response = api.decide(paused, reason=f"Approved with {SECRET}")
    assert response.status_code == 422
    assert response.json()["reason"] == "secret_shaped_text"
    approval = api.get(f"/v1/approvals/{paused['approval']['approval_id']}").json()
    assert approval["request"]["status"] == "pending" and approval["decision"] is None
    assert api.decide(paused, reason="Reviewed the evidence").status_code == 200


def test_a_credential_in_a_spec_never_reaches_storage(stack, tmp_path):
    api = stack(proposal())
    api.post("/v1/campaigns", key="k", json={**spec_json(), "objective": f"Key {SECRET}"})
    stored = b"".join(path.read_bytes() for path in tmp_path.rglob("*")
                      if path.is_file() and path.name != LOCK_NAME)  # locked, and always empty
    assert SECRET.encode() not in stored


def test_benign_text_passes_and_is_stored_as_sent(stack):
    api = stack(proposal())
    body = {**spec_json(), "objective": "Check the sk- prefix handling and token budgets"}
    created = api.post("/v1/campaigns", key="k", json=body)
    assert created.status_code == 202
    assert created.json()["record"]["spec"]["objective"] == body["objective"]


def test_a_redaction_marker_longer_than_its_match_cannot_break_a_read(stack, monkeypatch):
    # An 8-character literal grows to the marker; a full-length text must stay readable.
    from ghostlab.orchestration import CampaignController

    api = stack(proposal(), redactor=Redactor(["abcdefgh"]))
    paused = api.paused()
    experiment_id = paused["experiments"][0]["experiment_id"]
    read = CampaignController.state

    def stored(self, campaign):
        state = read(self, campaign)
        text = "abcdefgh" * 250
        state["experiments"][experiment_id]["proposal"]["hypothesis"] = text
        state["events"][0]["summary"] = text
        return state

    monkeypatch.setattr(CampaignController, "state", stored)
    url = f"/v1/campaigns/{paused['record']['campaign_id']}"
    for path in (url, f"{url}/events", f"{url}/experiments/{experiment_id}"):
        response = api.get(path)
        assert response.status_code == 200, path
        assert "abcdefgh" not in response.text
