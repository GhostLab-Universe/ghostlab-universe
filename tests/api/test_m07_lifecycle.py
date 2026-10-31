"""Idempotency, cancellation, reconnect, restart, expiry and the worker's edges.

Inference is a labeled fixture (httpx.MockTransport). Everything else is the real code.
"""

import dataclasses
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from conftest import LOOPBACK, Api, build_app, build_lab, spec_json
from ghostlab.api.jobs import JobRegistry
from ghostlab.worlds.journey import JourneyFixture
from m05_harness import Script, proposal
from m05_journey_harness import J, journey_reply

REFUND = "refund-world/1"


class Gate:
    """Holds a campaign's worker at one graph node, so a test controls the timing."""

    def __init__(self, node):
        self.node, self.reached, self.release = node, threading.Event(), threading.Event()

    def __call__(self, name, state):
        if name == self.node:
            self.reached.set()
            assert self.release.wait(30)


class Crash(BaseException):
    """A process dying mid-node: nothing above the worker may treat it as handled."""


class CrashOnce:
    def __init__(self, node):
        self.node, self.armed = node, True

    def __call__(self, name, state):
        if self.armed and name == self.node:
            self.armed = False
            raise Crash()


def experiment_count(api):
    return len(api.lab.engine.calls)


# -- idempotency ------------------------------------------------------------------------

def test_a_retry_returns_the_same_campaign_and_runs_nothing_new(stack):
    api = stack(proposal())
    first = api.create(key="same")
    campaign_id = first.json()["record"]["campaign_id"]
    api.wait(campaign_id, "awaiting_approval")

    retry = api.create(key="same")
    assert retry.status_code == 202
    assert retry.headers["Idempotency-Replayed"] == "true"
    assert retry.json()["record"]["campaign_id"] == campaign_id
    assert retry.json()["record"]["status"] == "awaiting_approval"  # current, not stored
    assert len(api.script.requests) == 1
    assert experiment_count(api) == 2  # baseline and fork, once


def test_a_new_key_is_a_new_campaign(stack):
    api = stack(proposal(), proposal())
    one = api.create(key="one").json()["record"]["campaign_id"]
    two = api.create(key="two").json()["record"]["campaign_id"]
    assert one != two
    api.wait(one, "awaiting_approval")
    api.wait(two, "awaiting_approval")
    assert len(api.script.requests) == 2


def test_reusing_a_key_for_a_different_spec_is_refused(stack):
    api = stack(proposal())
    campaign_id = api.create(key="same").json()["record"]["campaign_id"]
    api.wait(campaign_id, "awaiting_approval")
    other = api.post("/v1/campaigns", key="same", json=spec_json(max_tool_calls=99))
    assert other.status_code == 409
    assert other.json()["reason"] == "idempotency_key_reused"
    world = api.post("/v1/campaigns", key="same", json=spec_json(J))
    assert world.status_code == 409
    assert len(api.script.requests) == 1


def test_a_key_belongs_to_one_session(stack):
    api = stack(proposal(), proposal())
    mine = api.create(key="shared").json()["record"]["campaign_id"]
    theirs = api.create(key="shared", who="outsider").json()["record"]["campaign_id"]
    assert mine != theirs
    api.wait(mine, "awaiting_approval")
    api.wait(theirs, "awaiting_approval", who="outsider")
    assert len(api.script.requests) == 2


def test_concurrent_duplicates_start_one_campaign(stack):
    api = stack(proposal())
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda _: api.create(key="race"), range(8)))
    assert {response.status_code for response in responses} == {202}
    ids = {response.json()["record"]["campaign_id"] for response in responses}
    assert len(ids) == 1
    assert sum("Idempotency-Replayed" not in r.headers for r in responses) == 1
    api.wait(ids.pop(), "awaiting_approval")
    assert len(api.script.requests) == 1


def test_a_retried_decision_returns_the_recorded_one(stack):
    api = stack(proposal())
    paused = api.paused()
    first = api.decide(paused, key="d-1", reason="Reviewed")
    campaign_id = paused["record"]["campaign_id"]
    api.wait(campaign_id, "completed")

    retry = api.decide(paused, key="d-1", reason="Reviewed")
    assert retry.status_code == 200
    assert retry.headers["Idempotency-Replayed"] == "true"
    assert retry.json()["decision"] == first.json()["decision"]
    assert experiment_count(api) == 3  # baseline, fork and one repair: nothing re-ran

    changed = api.decide(paused, "reject", key="d-1", reason="Reviewed")
    assert changed.status_code == 409
    assert changed.json()["reason"] == "idempotency_key_reused"
    another = api.decide(paused, key="d-2")
    assert another.status_code == 409
    assert another.json()["reason"] == "not_pending"


def test_capsule_export_is_naturally_idempotent(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    experiment_id = paused["experiments"][0]["experiment_id"]
    url = f"/v1/campaigns/{campaign_id}/experiments/{experiment_id}/capsules/fork"
    assert api.post(url).json() == api.post(url).json()


# -- approvals: denied, expired, used -----------------------------------------------------

def test_an_expired_approval_cannot_resume_work(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    url = f"/v1/approvals/{paused['approval']['approval_id']}"
    api.lab.clock.advance(hours=2)

    current = api.get(url).json()
    assert current["effective_status"] == "expired"
    assert current["request"]["status"] == "pending"  # a read changes nothing

    response = api.decide(paused)
    assert response.status_code == 409
    assert response.json()["reason"] == "expired"
    done = api.wait(campaign_id, "failed")
    assert done["outcome"] == "approval_expired"
    assert done["repair"] is None
    assert experiment_count(api) == 2
    assert api.get(url).json()["request"]["status"] == "expired"


def test_resuming_past_expiry_ends_the_campaign_without_a_repair(stack):
    api = stack(journey_reply())
    paused = api.paused(J)
    campaign_id = paused["record"]["campaign_id"]
    api.lab.clock.advance(hours=2)
    assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
    done = api.wait(campaign_id, "failed")
    assert done["outcome"] == "approval_expired" and done["repair"] is None


def test_resume_grants_nothing_while_the_approval_is_pending(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    for _ in range(2):
        assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
        view = api.wait(campaign_id, "awaiting_approval")
    assert view["repair"] is None and experiment_count(api) == 2


def test_resume_after_a_rejection_does_not_run_the_repair(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    api.decide(paused, "reject")
    api.wait(campaign_id, "completed")
    assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
    assert api.wait(campaign_id, "completed")["outcome"] == "repair_rejected"
    assert experiment_count(api) == 2


def test_a_finished_repair_cannot_be_run_twice(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    api.decide(paused)
    api.wait(campaign_id, "completed")
    for _ in range(2):
        assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
    assert api.wait(campaign_id, "completed")["outcome"] == "repair_verified"
    assert experiment_count(api) == 3
    approval = api.get(f"/v1/approvals/{paused['approval']['approval_id']}").json()
    assert approval["request"]["status"] == "consumed"


def test_changed_settings_keep_the_decision_and_block_the_repair(stack):
    api = stack(journey_reply())
    paused = api.paused(J)
    campaign_id = paused["record"]["campaign_id"]
    plane = api.client.app.state.plane
    original = plane.services
    plane.services = dataclasses.replace(original, journey_fixture=JourneyFixture(
        journey_id="journey-changed"))

    decided = api.decide(paused)
    assert decided.status_code == 200
    body = decided.json()
    assert body["resumed"] is False
    assert body["resume_error"]["reason"] == "settings_changed"
    assert body["approval"]["status"] == "approved"
    blocked = api.post(f"/v1/campaigns/{campaign_id}/resume")
    assert blocked.status_code == 409
    assert blocked.json()["reason"] == "settings_changed"
    assert api.wait(campaign_id, "awaiting_approval")["repair"] is None

    plane.services = original
    assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
    assert api.wait(campaign_id, "completed")["outcome"] == "repair_verified"


def test_resume_while_a_worker_is_running_adds_nothing(stack):
    gate = Gate("execute")
    api = stack(proposal(), lab_options={"on_node": gate})
    campaign_id = api.create().json()["record"]["campaign_id"]
    assert gate.reached.wait(30)
    resumed = api.post(f"/v1/campaigns/{campaign_id}/resume")
    assert resumed.status_code == 202
    assert resumed.json()["job"]["state"] == "active"
    gate.release.set()
    assert api.wait(campaign_id, "awaiting_approval")["job"]["error"] is None
    assert api.client.app.state.plane.jobs._jobs == {}  # nothing was queued behind the worker
    assert experiment_count(api) == 2


# -- cancel -----------------------------------------------------------------------------

def test_cancel_stops_a_running_campaign_at_its_next_guard(stack):
    gate = Gate("execute")
    api = stack(proposal(), lab_options={"on_node": gate})
    campaign_id = api.create().json()["record"]["campaign_id"]
    assert gate.reached.wait(30)

    view = api.view(campaign_id)
    assert view["record"]["status"] == "running"  # not "awaiting_approval"
    assert view["job"]["state"] == "active"

    cancelled = api.post(f"/v1/campaigns/{campaign_id}/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json()["job"]["cancel_requested"] is True
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202  # a retry
    gate.release.set()

    done = api.wait(campaign_id, "cancelled")
    assert done["outcome"] == "cancelled_by_request"
    assert done["errors"][-1]["code"] == "cancelled"
    assert experiment_count(api) == 0
    assert {t["status"] for t in done["tasks"]} <= {"cancelled", "succeeded"}
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202  # still a retry


def test_cancelling_a_paused_campaign_ends_it_with_its_approval_unspent(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202
    done = api.wait(campaign_id, "cancelled")
    assert done["outcome"] == "cancelled_by_request" and done["repair"] is None
    assert experiment_count(api) == 2
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202  # still a retry
    url = f"/v1/approvals/{paused['approval']['approval_id']}"
    assert api.get(url).json()["request"]["status"] == "pending"

    late = api.decide(paused)  # recording it runs nothing: the campaign has stopped
    assert late.status_code == 200
    assert late.json()["resumed"] is False
    assert late.json()["resume_error"]["reason"] == "campaign_finished"
    assert api.wait(campaign_id, "cancelled")["repair"] is None
    assert experiment_count(api) == 2


def test_cancel_after_a_decision_leaves_the_approval_unspent(stack):
    gate = Gate("consume")
    api = stack(proposal(), lab_options={"on_node": gate})
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    assert api.decide(paused).status_code == 200
    assert gate.reached.wait(30)
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202
    gate.release.set()
    done = api.wait(campaign_id, "cancelled")
    assert done["repair"] is None and experiment_count(api) == 2
    approval = api.get(f"/v1/approvals/{paused['approval']['approval_id']}").json()
    assert approval["request"]["status"] == "approved"  # not consumed by a stopped campaign


def test_cancel_reaches_a_paused_campaign_whose_settings_changed(stack):
    api = stack(journey_reply())
    paused = api.paused(J)
    campaign_id = paused["record"]["campaign_id"]
    plane = api.client.app.state.plane
    plane.services = dataclasses.replace(plane.services, journey_fixture=JourneyFixture(
        journey_id="journey-changed"))
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202
    assert api.wait(campaign_id, "cancelled")["outcome"] == "cancelled_by_request"


def test_a_finished_campaign_refuses_cancel(stack):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    api.decide(paused)
    api.wait(campaign_id, "completed")
    response = api.post(f"/v1/campaigns/{campaign_id}/cancel")
    assert response.status_code == 409
    assert response.json()["reason"] == "campaign_finished"
    assert api.view(campaign_id)["record"]["status"] == "completed"


def test_cancel_during_the_repair_stops_before_it_runs(stack):
    gate = Gate("execute_repair")
    api = stack(proposal(), lab_options={"on_node": gate})
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    assert api.decide(paused).status_code == 200
    assert gate.reached.wait(30)
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202
    gate.release.set()
    done = api.wait(campaign_id, "cancelled")
    assert done["repair"] == {"authorized": True, "verified": False, "run": None,
                              "run_status": None, "safety_held": None, "task_complete": None,
                              "task_outcome": None, "adapter": None, "execution": None,
                              "guard_denials": None, "held_by": None, "lost_faults": None,
                              "refunded_minor": None}
    assert experiment_count(api) == 2


# -- worker edges, stalls and restarts ----------------------------------------------------

def test_a_decision_during_the_pause_window_is_not_lost():
    ran, gate = [], threading.Event()
    registry = JobRegistry(limit=2, on_failure=lambda key, error, cancelling: None)
    job, started = registry.submit("k", lambda job: gate.wait(30))
    assert started
    joined, started = registry.submit("k", lambda job: ran.append("resume"), follow=True)
    assert joined is job and not started
    gate.set()
    job.settled.wait(30)
    for thread in list(registry._threads.values()):
        thread.join(30)
    assert ran == ["resume"]
    assert registry.get("k") is None


def test_too_many_running_campaigns_is_a_retryable_refusal(stack):
    gate = Gate("execute")
    api = stack(proposal(), proposal(), lab_options={"on_node": gate}, max_active_campaigns=1)
    first = api.create(key="first").json()["record"]["campaign_id"]
    assert gate.reached.wait(30)
    second = api.create(key="second")
    assert second.status_code == 429
    assert second.json()["reason"] == "too_many_campaigns"
    assert second.json()["error"]["retryable"] is True
    assert second.headers["Retry-After"] == "5"
    gate.release.set()
    api.wait(first, "awaiting_approval")
    assert api.create(key="second").status_code == 202


def test_a_crashed_worker_reads_as_stalled_and_continues_from_its_checkpoint(stack):
    crash = CrashOnce("verify")
    api = stack(proposal(), lab_options={"on_node": crash})
    campaign_id = api.create().json()["record"]["campaign_id"]
    stalled = api.wait(campaign_id, "running")
    assert stalled["job"]["state"] == "stalled"
    assert stalled["job"]["error"]["code"] == "internal"
    assert "Crash" not in str(stalled["job"]["error"])

    assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
    assert api.wait(campaign_id, "awaiting_approval")["job"]["error"] is None
    assert len(api.script.requests) == 1  # continued, did not repeat the model call
    assert experiment_count(api) == 2


def test_cancel_reaches_a_stalled_campaign(stack):
    crash = CrashOnce("execute")
    api = stack(proposal(), lab_options={"on_node": crash})
    campaign_id = api.create().json()["record"]["campaign_id"]
    api.wait(campaign_id, "running")
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel").status_code == 202
    assert api.wait(campaign_id, "cancelled")["outcome"] == "cancelled_by_request"
    assert experiment_count(api) == 0


def test_a_crash_after_approval_continues_and_runs_the_repair_once(stack):
    crash = CrashOnce("consume")
    api = stack(proposal(), lab_options={"on_node": crash})
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    assert api.decide(paused).status_code == 200
    stalled = api.wait(campaign_id, "awaiting_approval")
    assert stalled["job"]["state"] == "stalled"
    assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
    done = api.wait(campaign_id, "completed")
    assert done["outcome"] == "repair_verified"
    assert experiment_count(api) == 3


def restart(lab, **options):
    """A new server process over the same storage: the registry and threads are gone."""
    return TestClient(build_app(lab, **options), **LOOPBACK)


def test_a_paused_campaign_survives_a_restart_and_finishes(tmp_path):
    lab = build_lab(tmp_path, Script(journey_reply()))
    with restart(lab) as client:
        api = Api(client, lab)
        paused = api.paused(J)
    with restart(lab) as client:
        api = Api(client, lab)
        campaign_id = paused["record"]["campaign_id"]
        reconnected = api.view(campaign_id)
        assert reconnected["record"]["status"] == "awaiting_approval"
        assert reconnected["job"]["state"] == "idle"
        assert reconnected["approval"] == paused["approval"]
        assert api.create(J).json()["record"]["campaign_id"] == campaign_id  # still a retry
        assert api.decide(paused).status_code == 200
        assert api.wait(campaign_id, "completed")["outcome"] == "repair_verified"


def test_a_process_lost_mid_run_is_stalled_after_restart_with_no_recorded_error(tmp_path):
    crash = CrashOnce("verify")
    lab = build_lab(tmp_path, Script(proposal()), on_node=crash)
    with restart(lab) as client:
        api = Api(client, lab)
        campaign_id = api.create().json()["record"]["campaign_id"]
        api.wait(campaign_id, "running")
    with restart(lab) as client:
        api = Api(client, lab)
        view = api.view(campaign_id)
        assert view["record"]["status"] == "running"
        assert view["job"] == {"state": "stalled", "cancel_requested": False, "error": None}
        assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
        assert api.wait(campaign_id, "awaiting_approval")["job"]["state"] == "idle"
        assert len(lab.script.requests) == 1


def test_a_decided_approval_reads_as_stalled_after_a_restart(tmp_path):
    crash = CrashOnce("consume")
    lab = build_lab(tmp_path, Script(proposal()), on_node=crash)
    with restart(lab) as client:
        api = Api(client, lab)
        paused = api.paused()
        campaign_id = paused["record"]["campaign_id"]
        assert api.decide(paused).status_code == 200
        api.wait(campaign_id, "awaiting_approval")
    with restart(lab) as client:
        api = Api(client, lab)
        view = api.view(campaign_id)
        assert view["record"]["status"] == "awaiting_approval"
        assert view["job"] == {"state": "stalled", "cancel_requested": False, "error": None}
        assert api.post(f"/v1/campaigns/{campaign_id}/resume").status_code == 202
        assert api.wait(campaign_id, "completed")["outcome"] == "repair_verified"
        assert len(lab.engine.calls) == 3


def test_a_pending_approval_stays_idle_after_a_restart(tmp_path):
    lab = build_lab(tmp_path, Script(proposal()))
    with restart(lab) as client:
        paused = Api(client, lab).paused()
    with restart(lab) as client:
        view = Api(client, lab).view(paused["record"]["campaign_id"])
        assert view["job"]["state"] == "idle"


def test_shutdown_lets_a_short_run_reach_its_pause(tmp_path):
    lab = build_lab(tmp_path, Script(proposal()))
    with restart(lab) as client:
        api = Api(client, lab)
        campaign_id = api.create().json()["record"]["campaign_id"]
    with restart(lab) as client:
        assert Api(client, lab).view(campaign_id)["record"]["status"] == "awaiting_approval"


@pytest.mark.parametrize("who", ["reviewer", "stranger", "outsider"])
def test_only_the_owner_may_resume_or_cancel(stack, who):
    api = stack(proposal())
    paused = api.paused()
    campaign_id = paused["record"]["campaign_id"]
    assert api.post(f"/v1/campaigns/{campaign_id}/resume", who=who).status_code == 404
    assert api.post(f"/v1/campaigns/{campaign_id}/cancel", who=who).status_code == 404
