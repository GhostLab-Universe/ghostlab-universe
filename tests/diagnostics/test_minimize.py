import json
import socket
from dataclasses import replace

import pytest

from ghostlab import capsule
from ghostlab.core import Fixture, Scenario, run as refund_run
from ghostlab.diagnostics.__main__ import main
from ghostlab.diagnostics.minimize import Budget, classify, minimize_capsule
from ghostlab.worlds import journey_capsule
from ghostlab.worlds.journey import JourneyFixture, JourneyScenario, scenario_json
from ghostlab.worlds.journey_run import run

PRIVACY = "delivered_messages_permitted"


@pytest.fixture
def original(tmp_path):
    fixture = JourneyFixture()
    data = scenario_json([
        ("stale_preference_memory", None, {"preference_version": 1}),
        ("delayed_itinerary_update", None, {"delay_events": 2}),
        ("sharing_revoked_while_queued", None, {"recipient_id": "participant-c"})])
    recorded = run(fixture, JourneyScenario.from_json(data), "journey-naive")
    path = tmp_path / "source.json"
    journey_capsule.export_capsule(path, fixture, data, recorded)
    return path


def test_three_faults_reduce_to_two_with_same_recipient_and_message(original, tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network was contacted")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    before = original.read_bytes()
    out = tmp_path / "reduced"
    report = minimize_capsule(original, out, invariant=PRIVACY)
    assert report["status"] == "reduced" and report["cardinality_minimal"]
    assert report["selected_faults"] == ["stale_preference_memory", "sharing_revoked_while_queued"]
    assert report["recipient_witnesses"] == ["participant-c"]
    assert report["model_calls"] == 0 and report["capsule_reproduced"]
    assert report["runs_used"] <= report["max_runs"]
    assert original.read_bytes() == before == (out / "original-capsule.json").read_bytes()
    proof = journey_capsule.replay(out / "minimal-capsule.json")
    assert proof["reproduced"] and proof["checks"][PRIVACY] is False
    reduced = json.loads((out / "minimal-capsule.json").read_text())["payload"]
    source = json.loads(before)["payload"]
    assert reduced["fixture"] == source["fixture"]
    assert reduced["actor_id"] == source["actor_id"]
    assert reduced["scenario"]["max_tool_calls"] == source["scenario"]["max_tool_calls"]
    regression = json.loads((out / "regression-case.json").read_text())
    assert regression["expected_passed"] is False


def test_refund_single_fault_is_already_minimal(tmp_path):
    fixture, scenario = Fixture(), Scenario(timeout_after_commit=True)
    source = tmp_path / "refund.json"
    capsule.export_capsule(source, fixture, scenario, refund_run(fixture, scenario, "vulnerable"))
    report = minimize_capsule(source, tmp_path / "out", invariant="total_refund_within_payment")
    assert report["status"] == "already_minimal" and report["cardinality_minimal"]
    assert report["selected_faults"] == ["timeout_after_commit"]
    assert report["capsule_reproduced"]


@pytest.mark.parametrize("max_runs", [1, 2, 3])
def test_budget_exhaustion_never_claims_a_minimum(original, tmp_path, max_runs):
    out = tmp_path / "bounded"
    report = minimize_capsule(original, out, invariant=PRIVACY, max_runs=max_runs)
    assert report["status"] == "run_budget_exhausted"
    assert report["runs_used"] <= max_runs
    if max_runs == 1:
        assert report["runs_used"] == 0  # two-pass replay cannot start with one slot
    assert not report["cardinality_minimal"] and not report["capsule_reproduced"]
    assert not (out / "regression-case.json").exists()


def test_deadline_stops_before_a_world_run(original, tmp_path):
    calls = iter([0.0, 1.0])
    report = minimize_capsule(original, tmp_path / "deadline", invariant=PRIVACY,
                              deadline_ms=1, clock=lambda: next(calls))
    assert report["status"] == "deadline_exceeded" and report["runs_used"] == 0
    assert not report["capsule_reproduced"]


def test_truncated_negative_is_inconclusive():
    run = {"status": "budget_exhausted", "checks": {PRIVACY: True}}
    assert classify(run, PRIVACY, set()) == "inconclusive"
    run["status"] = "completed"
    assert classify(run, PRIVACY, set()) == "not_reproduced"


def test_a_different_recipient_does_not_reproduce_the_witness():
    run = {"status": "completed", "checks": {PRIVACY: False}, "final_state": {
        "delivery_log": [{"recipient_id": "another-person", "event_id": 2}],
        "permission_timeline": []}}
    assert classify(run, PRIVACY, {"participant-c"}) == "not_reproduced"


def test_a_bad_checksum_is_not_repaired(original, tmp_path):
    body = json.loads(original.read_text())
    body["payload"]["seed"] = 1
    original.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="checksum"):
        minimize_capsule(original, tmp_path / "bad", invariant=PRIVACY)


def test_only_a_failed_invariant_can_be_selected(original, tmp_path):
    with pytest.raises(ValueError, match="failed"):
        minimize_capsule(original, tmp_path / "bad", invariant="made-up-check")


def test_replayable_actions_cannot_impersonate_an_adapter(original, tmp_path):
    body = json.loads(original.read_text())
    # Naive actions labelled as checked replay as actions but cannot justify a
    # fresh checked-adapter minimization experiment.
    p = body["payload"]
    p["run"]["adapter_id"] = "journey-version-checked"
    body["sha256"] = journey_capsule.capsule_digest(p)
    original.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="adapter produces"):
        journey_capsule.replay(original)
    with pytest.raises(ValueError, match="adapter produces"):
        minimize_capsule(original, tmp_path / "bad", invariant=PRIVACY)


def test_existing_output_is_never_overwritten(original, tmp_path):
    out = tmp_path / "existing"
    out.mkdir()
    marker = out / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(ValueError, match="already exist"):
        minimize_capsule(original, out, invariant=PRIVACY)
    assert marker.read_text() == "keep"


def test_truncated_smaller_trial_cannot_certify_minimality(tmp_path):
    fixture = JourneyFixture()
    data = scenario_json([("stale_preference_memory", None, {"preference_version": 1})],
                         max_tool_calls=4)
    recorded = run(fixture, JourneyScenario.from_json(data), "journey-naive")
    source = tmp_path / "truncated-source.json"
    journey_capsule.export_capsule(source, fixture, data, recorded)
    report = minimize_capsule(source, tmp_path / "unknown", invariant="commitments_use_approved_intent")
    assert report["capsule_reproduced"] and report["status"] == "smallest_found"
    assert report["cardinality_minimal"] is False
    assert report["trials"][0]["result"] == "inconclusive"


def test_a_failure_with_no_injection_is_reported_as_baseline_failure(tmp_path):
    from ghostlab.worlds.journey import JourneyOption
    fixture = replace(JourneyFixture(), outbound=(900, 1000), options=(
        JourneyOption("early-return", 930, 1050, 2),))
    data = scenario_json([("sharing_revoked_while_queued", None, {"recipient_id": "participant-c"})])
    recorded = run(fixture, JourneyScenario.from_json(data), "journey-naive")
    source = tmp_path / "already-bad.json"
    journey_capsule.export_capsule(source, fixture, data, recorded)
    report = minimize_capsule(source, tmp_path / "baseline", invariant="mandatory_participants_feasible")
    assert report["status"] == "baseline_failure" and report["selected_faults"] == []
    assert report["cardinality_minimal"] and report["capsule_reproduced"]


@pytest.mark.parametrize("value", [True, 0, 65, 1.5])
def test_invalid_run_budgets_are_refused(value):
    with pytest.raises(ValueError):
        Budget(value, 1000, lambda: 0.0)


def test_cli_reports_real_reduction(original, tmp_path, capsys):
    assert main([str(original), "--invariant", PRIVACY, "--out", str(tmp_path / "cli")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["selected_faults"] == ["stale_preference_memory", "sharing_revoked_while_queued"]


def test_disclosing_a_different_template_is_not_the_same_failure(original):
    from ghostlab.diagnostics.minimize import load_problem, failure_witness
    problem = load_problem(original)
    expected = failure_witness(problem, problem.original_run, PRIVACY)
    changed = problem.run(["sharing_revoked_while_queued"])
    assert changed["checks"][PRIVACY] is False
    assert classify(changed, PRIVACY, {"participant-c"}, problem=problem,
                    expected_witness=expected) == "not_reproduced"
    assert expected[0]["message"]["template_id"] == "itinerary-v1"
    assert failure_witness(problem, changed, PRIVACY)[0]["message"]["template_id"] == "itinerary-v2"


def test_double_booking_cannot_reduce_to_a_late_booking(tmp_path):
    from ghostlab.diagnostics.minimize import load_problem, failure_witness
    fixture = replace(JourneyFixture(), intents=((1, 1200), (2, 1140), (3, 1080)), current_version=3)
    data = scenario_json([
        ("stale_preference_memory", None, {"preference_version": 2}),
        ("delayed_itinerary_update", None, {"delay_events": 2}),
        ("sharing_revoked_while_queued", None, {"recipient_id": "participant-c"})])
    recorded = run(fixture, JourneyScenario.from_json(data), "journey-naive")
    source = tmp_path / "double.json"
    journey_capsule.export_capsule(source, fixture, data, recorded)
    problem = load_problem(source)
    invariant = "mandatory_participants_feasible"
    witness = failure_witness(problem, recorded, invariant)
    assert any(key[0] == "double" for key in witness["problem_keys"])
    late = problem.run(["stale_preference_memory"])
    assert late["checks"][invariant] is False
    assert not any(key[0] == "double" for key in failure_witness(problem, late, invariant)["problem_keys"])
    assert classify(late, invariant, set(), problem=problem, expected_witness=witness) == "not_reproduced"
    report = minimize_capsule(source, tmp_path / "same-double", invariant=invariant)
    assert report["selected_faults"] == ["stale_preference_memory", "delayed_itinerary_update"]
    assert report["failure_witness"] == witness


def test_budget_stop_exposes_only_an_unconfirmed_candidate(original, tmp_path):
    out = tmp_path / "unconfirmed"
    report = minimize_capsule(original, out, invariant=PRIVACY, max_runs=9)
    assert report["candidate_faults"] == ["stale_preference_memory", "sharing_revoked_while_queued"]
    assert report["selected_faults"] is None and report["removed_faults"] == []
    assert not report["capsule_reproduced"]
    assert not (out / "minimal-capsule.json").exists()
    assert not (out / "regression-case.json").exists()


def test_output_claim_is_atomic_and_competing_directory_is_preserved(original, tmp_path, monkeypatch):
    out = tmp_path / "race"
    mkdir = type(out).mkdir
    def competing(self, *args, **kwargs):
        if self == out:
            mkdir(out)
            (out / "report.json").write_text("competitor")
        return mkdir(self, *args, **kwargs)
    monkeypatch.setattr(type(out), "mkdir", competing)
    with pytest.raises(ValueError, match="already exist"):
        minimize_capsule(original, out, invariant=PRIVACY)
    assert (out / "report.json").read_text() == "competitor"
    assert list(out.iterdir()) == [out / "report.json"]


def test_cli_deep_json_is_a_sanitized_input_error(tmp_path, capsys):
    source = tmp_path / "deep.json"
    source.write_bytes(b"[" * 5000 + b"0" + b"]" * 5000)
    with pytest.raises(SystemExit) as error:
        main([str(source), "--invariant", PRIVACY, "--out", str(tmp_path / "out")])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "Traceback" not in output.err and not output.out
