"""The real server process: ``python -m ghostlab.api`` over a real socket.

``--scripted`` serves checked-in proposals through httpx.MockTransport: no model, no
network and no key. These tests start separate operating-system processes on purpose,
because the root lock and the entry point can't be shown from inside one.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REFUND, JOURNEY = "refund-world/1", "group-journey/1"
FIXTURE_SERVER = Path(__file__).with_name("fixture_server.py")
BUDGET = {"max_model_calls": 10, "max_tool_calls": 100, "max_input_tokens": 100_000,
          "max_output_tokens": 20_000, "max_cost_minor": None, "cost_currency": None,
          "max_wall_clock_ms": 1_200_000, "max_parallel_runs": 2}
START_S = 60
WAIT_S = 30


def spec(world):
    return {"schema_version": 1, "objective": "Find a failure after a fault",
            "success_criteria": ["A failing invariant is reproduced"], "world_version": world,
            "budget": BUDGET}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def environment(**changes):
    env = {key: value for key, value in os.environ.items()
           if key not in {"MODEL_API_KEY", "BASETEN_API_KEY", "GHOSTLAB_DEPLOYMENT"}}
    return {**env, "GHOSTLAB_AUTH_MODE": "local_dev", "LANGSMITH_TRACING": "false", **changes}


def spawn(root, port, *extra, command=None, **env):
    return subprocess.Popen(
        command or [sys.executable, "-m", "ghostlab.api", "--root", str(root), "--port", str(port),
                    *extra],
        env=environment(**env), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def client(port):
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30)


def wait_up(process, port):
    deadline = time.monotonic() + START_S
    with client(port) as http:
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read()
            try:
                return http.get("/v1/session").json()
            except httpx.TransportError:
                time.sleep(0.1)
    raise AssertionError("the server did not start")


def stop(process):
    process.kill()
    process.communicate(timeout=30)


@pytest.fixture
def scripted(tmp_path):
    port = free_port()
    process = spawn(tmp_path / "root", port, "--scripted")
    try:
        session = wait_up(process, port)
        with client(port) as http:
            yield http, session
    finally:
        stop(process)


def wait(http, campaign_id, *statuses):
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        view = http.get(f"/v1/campaigns/{campaign_id}").json()
        if view["record"]["status"] in statuses and view["job"]["state"] != "active":
            return view
        time.sleep(0.02)
    raise AssertionError(f"timed out: {view['record']['status']} {view['job']}")


def create(http, world, key, **params):
    response = http.post("/v1/campaigns", json=spec(world), params=params,
                         headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    assert response.json()["inference"] == "scripted"
    return response.json()["record"]["campaign_id"]


def decide(http, view, key):
    approval = view["approval"]
    response = http.post(f"/v1/approvals/{approval['approval_id']}/decision",
                         headers={"Idempotency-Key": key},
                         json={"schema_version": 1, "approval_id": approval["approval_id"],
                               "action_digest": approval["action_digest"], "decision": "approve",
                               "reason": "Reviewed the evidence"})
    assert response.status_code == 200, response.text
    assert response.json()["resumed"] is True


def inspected(http, view):
    """Evidence and Reality Diff, each labelled, from the failure a campaign found."""
    assert view["inference"] == "scripted"
    url = (f"/v1/campaigns/{view['record']['campaign_id']}/experiments/"
           f"{view['experiments'][0]['experiment_id']}")
    experiment = http.get(url).json()
    assert experiment["inference"] == "scripted"
    assert [f for f in experiment["findings"] if f["verdict"] == "failed"]
    diff = http.get(f"{url}/diff").json()
    assert diff["inference"] == "scripted" and diff["coverage"]["complete"] is True
    assert diff["diff"]["violated_invariants"] and diff["diff"]["first_divergence"]
    run = http.get(f"{url}/runs/fork").json()
    assert run["inference"] == "scripted"
    events = http.get(f"/v1/campaigns/{view['record']['campaign_id']}/events").json()
    assert events["inference"] == "scripted" and events["events"]
    approval = http.get(f"/v1/approvals/{view['approval']['approval_id']}").json()
    assert approval["inference"] == "scripted"
    assert approval["action"]["fixture_sha256"] == view["fixture_sha256"]
    return url, diff


def exported_and_replayed(http, url):
    capsule = http.post(f"{url}/capsules/repaired")
    assert capsule.status_code == 201, capsule.text
    assert capsule.json()["inference"] == "scripted"
    replayed = http.post(f"{url}/capsules/repaired/replay").json()
    assert (replayed["reproduced"], replayed["run_status"]) == (True, "completed")
    assert replayed["inference"] == "scripted" and replayed["model_calls"] == 0
    stored = http.post(f"/v1/capsules/{capsule.json()['artifact']['id']}/replay").json()
    assert stored["reproduced"] is True and stored["model_calls"] == 0


@pytest.mark.parametrize("world,key", [(REFUND, "refund-flow"), (JOURNEY, "journey-flow")])
def test_the_whole_flow_runs_on_the_real_server(scripted, world, key):
    http, session = scripted
    assert session["inference"] == "scripted"
    assert session["production_authenticated"] is False
    campaign_id = create(http, world, key)
    paused = wait(http, campaign_id, "awaiting_approval")
    url, _ = inspected(http, paused)
    decide(http, paused, "decision")
    done = wait(http, campaign_id, "completed")
    assert done["outcome"] == "repair_verified" and done["repair"]["verified"] is True
    assert done["inference"] == "scripted"
    assert http.get(f"{url}/runs/repaired").json()["inference"] == "scripted"
    exported_and_replayed(http, url)


def test_the_revocation_race_leaks_in_lab_and_holds_on_the_guarded_boundary(scripted):
    http, _ = scripted
    campaign_id = create(http, JOURNEY, "race", scripted_scenario="revocation_race")
    paused = wait(http, campaign_id, "awaiting_approval")
    url, diff = inspected(http, paused)
    assert paused["experiments"][0]["adapter_id"] == "journey-version-checked"
    assert http.get(url).json()["execution"] == {"mode": "lab", "guarded": False}
    assert diff["diff"]["violated_invariants"] == ["delivered_messages_permitted"]
    fork = http.get(f"{url}/runs/fork").json()
    assert [f["evidence_event_ids"] for f in fork["findings"]
            if f["invariant"] == "delivered_messages_permitted"] == [[18]]
    decide(http, paused, "decision")
    done = wait(http, campaign_id, "completed")
    assert done["outcome"] == "repair_verified"
    assert done["repair"]["held_by"] == "guarded_boundary"
    assert [d["event_id"] for d in done["repair"]["guard_denials"]] == [18]
    exported_and_replayed(http, url)


@pytest.mark.parametrize("world,key", [(REFUND, "never-refund"), (JOURNEY, "never-journey")])
def test_a_fault_that_never_fires_shows_coverage_incomplete(scripted, world, key):
    http, _ = scripted
    campaign_id = create(http, world, key, scripted_scenario="fault_never_fires")
    view = wait(http, campaign_id, "budget_exhausted")
    assert view["outcome"] == "coverage_incomplete" and view["inference"] == "scripted"
    assert view["approval"] is None and view["repair"] is None
    assert len(view["experiments"]) == 2
    for experiment in view["experiments"]:
        assert experiment["coverage"]["faults_triggered"] is False
        assert experiment["coverage"]["complete"] is False


def test_a_scenario_is_a_closed_choice_and_never_content(scripted):
    http, _ = scripted
    unknown = http.post("/v1/campaigns", json=spec(JOURNEY), headers={"Idempotency-Key": "a"},
                        params={"scripted_scenario": "my-own-proposal"})
    assert unknown.status_code == 422
    wrong_world = http.post("/v1/campaigns", json=spec(REFUND), headers={"Idempotency-Key": "b"},
                            params={"scripted_scenario": "revocation_race"})
    assert wrong_world.status_code == 422
    assert wrong_world.json()["reason"] == "scripted_scenario_unavailable"
    content = http.post("/v1/campaigns", headers={"Idempotency-Key": "c"},
                        json={**spec(JOURNEY), "proposal": {"adapter_id": "journey-naive"}})
    assert content.status_code == 422


def test_scripted_mode_is_off_by_default(tmp_path):
    port = free_port()
    process = spawn(tmp_path / "root", port)
    try:
        session = wait_up(process, port)
        assert session["inference"] == "disabled"
        with client(port) as http:
            refused = http.post("/v1/campaigns", json=spec(REFUND),
                                headers={"Idempotency-Key": "k"})
            assert refused.status_code == 403
            assert refused.json()["reason"] == "inference_not_authorized"
            scenario = http.post("/v1/campaigns", json=spec(JOURNEY),
                                 headers={"Idempotency-Key": "k"},
                                 params={"scripted_scenario": "revocation_race"})
            assert scenario.status_code == 422
            assert scenario.json()["reason"] == "scripted_scenario_unavailable"
    finally:
        stop(process)


@pytest.mark.parametrize("args,env,message", [
    (("--host", "0.0.0.0"), {}, "loopback"), ((), {"GHOSTLAB_DEPLOYMENT": "hosted"}, "hosted"),
    ((), {"GHOSTLAB_AUTH_MODE": ""}, "GHOSTLAB_AUTH_MODE")])
def test_scripted_mode_is_refused_outside_local_loopback(tmp_path, args, env, message):
    process = spawn(tmp_path / "root", free_port(), "--scripted", *args, **env)
    _, errors = process.communicate(timeout=60)
    assert process.returncode == 2
    assert message in errors


def test_a_second_process_is_refused_and_a_crash_does_not_block_a_restart(tmp_path):
    root, port = tmp_path / "root", free_port()
    first = spawn(root, port, "--scripted")
    try:
        wait_up(first, port)
        with client(port) as http:
            campaign_id = create(http, REFUND, "before-the-crash")
            wait(http, campaign_id, "awaiting_approval")

        second = spawn(root, free_port(), "--scripted")
        _, errors = second.communicate(timeout=60)
        assert second.returncode == 2 and "another server" in errors

        script = subprocess.Popen(
            [sys.executable, str(FIXTURE_SERVER), "--root", str(root), "--port", str(free_port())],
            env=environment(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output, _ = script.communicate(timeout=60)
        assert script.returncode != 0 and "another server" in output  # the fixture server too

        with client(port) as http:  # the holder is unaffected
            assert http.get(f"/v1/campaigns/{campaign_id}").status_code == 200
    finally:
        first.kill()  # no shutdown, no cleanup: as a crash
        first.communicate(timeout=30)

    port = free_port()
    restarted = spawn(root, port, "--scripted")
    try:
        wait_up(restarted, port)
        with client(port) as http:
            view = http.get(f"/v1/campaigns/{campaign_id}").json()
            assert view["record"]["status"] == "awaiting_approval"
            assert view["inference"] == "scripted"
            decide(http, view, "after-the-crash")
            assert wait(http, campaign_id, "completed")["outcome"] == "repair_verified"
    finally:
        stop(restarted)


def test_one_key_sent_at_two_servers_cannot_pay_twice(tmp_path):
    """The critic's repro: the second server can't exist, so one key meets one campaign."""
    root, port = tmp_path / "root", free_port()
    first = spawn(root, port, "--scripted")
    try:
        wait_up(first, port)
        second = spawn(root, free_port(), "--scripted")
        second.communicate(timeout=60)
        assert second.returncode == 2
        with client(port) as http:
            one = create(http, REFUND, "same-key")
            two = create(http, REFUND, "same-key")
            assert one == two
            view = wait(http, one, "awaiting_approval")
            assert view["record"]["usage"]["model_calls"] == 1
    finally:
        stop(first)


def test_openapi_and_session_agree_on_the_mode(scripted):
    http, session = scripted
    document = json.loads((Path(__file__).parents[2] / "src/ghostlab/api/openapi.v1.json")
                          .read_text(encoding="utf-8"))
    modes = document["components"]["schemas"]["SessionInfo"]["properties"]["inference"]["enum"]
    assert session["inference"] in modes
