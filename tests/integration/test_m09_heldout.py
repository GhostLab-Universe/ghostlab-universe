"""The held-out evaluation: the timings are chosen by rule, and tuned ones can't get in.

The whole selection takes minutes, so this reruns two of its cases and reads the recorded
result for the rest. Inference is a labeled fixture throughout.
"""

import json
import shutil

import pytest

from evals import demo, heldout
from evals.evidence import canonical_sha256
from ghostlab.worlds.journey import JourneyFixture

from conftest import ROOT

RULE = json.loads(heldout.RULE_PATH.read_text("utf-8"))
RESULT = json.loads(heldout.RESULT_PATH.read_text("utf-8"))
RACE_CASE, UNTESTED_CASE = "heldout-062", "heldout-032"  # a found failure, and an inconclusive


def timings(case):
    return {(fault["fault_id"], heldout._timing(fault)) for fault in case["faults"]}


def test_the_selection_reproduces_from_the_recorded_seed():
    assert RULE["seed"] == heldout.SEED
    assert heldout.select() == RULE["cases"]
    assert canonical_sha256(RULE["cases"]) == RULE["selection_sha256"]
    assert RULE["case_count"] == len(RULE["cases"])


def test_no_selected_fault_uses_a_tuned_timing():
    for case in RULE["cases"]:
        for fault_id, value in timings(case):
            assert value not in heldout.TUNED[fault_id], case["case_id"]


def test_every_timing_in_tests_and_cases_is_excluded():
    """A new tuned timing means the held-out set has to be redrawn, not quietly reused."""
    found, unresolved = heldout.scan()
    assert not {key for key in found if key[1] not in heldout.TUNED[key[0]]}
    assert unresolved <= {"after_event", "events", "n"}  # parameters of a helper or a sweep


def test_the_scan_sees_the_tuned_cases():
    found = heldout.scan()[0]
    assert {(heldout.REVOKED, 17), (heldout.REVOKED, 15), (heldout.REVOKED, 14),
            (heldout.REVOKED, None), (heldout.STALE, 5), (heldout.STALE, None),
            (heldout.DELAYED, 18), (heldout.DELAYED, 3)} <= set(found)
    assert any("evals/cases.py" in place for place in found[heldout.REVOKED, 15])


def test_the_exclusion_list_is_what_keeps_a_timing_out():
    without_race = {**heldout.TUNED, heldout.REVOKED: heldout.TUNED[heldout.REVOKED] - {17}}
    assert any((heldout.REVOKED, 17) in timings(case) for case in heldout.select(without_race))
    with_nine = {**heldout.TUNED, heldout.REVOKED: heldout.TUNED[heldout.REVOKED] | {9}}
    assert not any((heldout.REVOKED, 9) in timings(case) for case in heldout.select(with_nine))


def test_the_revocation_only_space_is_taken_whole():
    fixture = JourneyFixture()
    held_out = [value for value in range(1, heldout.CAP + 1)
                if value not in heldout.TUNED[heldout.REVOKED]]
    expected = {(adapter, recipient, value) for adapter in heldout.ADAPTERS
                for recipient in fixture.participants for value in held_out}
    selected = {(case["adapter"], case["faults"][0]["parameters"][0]["value"],
                 case["faults"][0]["after_event"]) for case in RULE["cases"]
                if case["fault_set"] == [heldout.REVOKED]}
    assert selected == expected


def test_the_rule_records_what_it_excluded_and_why():
    recorded = {item["fault_id"]: item for item in RULE["exclusions"]}
    assert set(recorded) == set(heldout.TUNED)
    for fault_id, tuned in heldout.TUNED.items():
        assert {json.dumps(value) for value in recorded[fault_id]["values"]} == {
            json.dumps(value) for value in tuned}
    assert RULE["base_commit"] == heldout.BASE_COMMIT
    assert set(RULE["classification"]) >= {"failure_found", "inconclusive", "errored",
                                           "fault_fired_no_violation", "unsafe_enforced_run"}


def test_held_out_results_stay_out_of_the_gate():
    expected = json.loads((ROOT / "evals" / "expected.json").read_text("utf-8"))
    assert not any(case_id.startswith("heldout") for case_id in expected)


# --- the recorded result ------------------------------------------------------------------

def test_the_result_is_for_this_selection_and_drops_nothing():
    assert RESULT["selection_sha256"] == RULE["selection_sha256"]
    assert [row["case_id"] for row in RESULT["cases"]] == [case["case_id"]
                                                           for case in RULE["cases"]]
    assert (RESULT["inference"], RESULT["live_provider_contacted"]) == ("mocked", False)


def test_the_counts_come_from_the_rows_and_add_up():
    counts = RESULT["counts"]
    assert heldout.counts(RESULT["cases"]) == counts
    assert (counts["failures_found"] + counts["fault_fired_no_violation"]
            + counts["inconclusive"] + counts["errored"]) == counts["cases"]


def test_an_inconclusive_case_stays_inconclusive_in_the_record():
    for row in RESULT["cases"]:
        if row["class"] == "inconclusive":
            assert row["campaign"]["outcome"] == "coverage_incomplete", row["case_id"]
            assert row["fork"]["coverage"]["complete"] is False
            assert row["repair"] is None


def test_each_recorded_verdict_matches_the_recorded_oracle():
    for row in RESULT["cases"]:
        oracle = row["fork"]["oracle"]
        violated = bool(oracle["unpermitted_deliveries"] or oracle["stale_commitments"])
        assert violated == (row["class"] == "failure_found"), row["case_id"]


def test_a_recorded_repair_ran_enforced_and_each_replay_called_no_model():
    for row in RESULT["cases"]:
        assert row["fork"]["execution"] == {"mode": "lab", "guarded": False}
        assert row["model_attempts"] == 1
        if row["repair"]:
            assert row["repair"]["execution"] == {"mode": "enforced", "guarded": True}
        for replay in row["replays"].values():
            assert replay["reported_model_calls"] == 0
            assert replay["provider_requests_during_replay"] == 0


# --- verdicts read stored evidence --------------------------------------------------------

def selected(*case_ids):
    return [case for case in RULE["cases"] if case["case_id"] in case_ids]


def row_of(report, case_id):
    return next(row for row in report["cases"] if row["case_id"] == case_id)


@pytest.fixture(scope="module")
def stored(tmp_path_factory):
    out = tmp_path_factory.mktemp("heldout")
    selection = selected(RACE_CASE, UNTESTED_CASE)
    heldout.run_heldout(out, heldout.held_out_cases(selection))
    return out, selection


@pytest.fixture
def copied(stored, tmp_path):
    out = tmp_path / "copy"
    shutil.copytree(stored[0], out)
    return out, stored[1]


def edit(out, case_id, change):
    path = out / "evidence" / f"{case_id}.json"
    data = json.loads(path.read_text("utf-8"))
    change(data["campaigns"]["main"])
    path.write_text(json.dumps(data), "utf-8")


def fork_of(campaign):
    return next(run for name, run in campaign["runs"].items() if name.endswith("/fork"))


def test_a_rerun_gives_the_recorded_rows(stored):
    report = heldout.score_heldout(*stored)
    for case_id in (RACE_CASE, UNTESTED_CASE):
        assert row_of(report, case_id) == row_of(RESULT, case_id)


def test_a_controller_claim_cant_change_the_oracles_verdict(copied):
    out, selection = copied

    def deny_everything(campaign):
        for finding in campaign["findings"]:
            finding["verdict"] = "passed"
        campaign["outcome"] = "no_failure_found"
        campaign["repair"]["verified"] = False

    edit(out, RACE_CASE, deny_everything)
    row = row_of(heldout.score_heldout(out, selection), RACE_CASE)
    assert row["class"] == "failure_found"
    assert row["fork"]["oracle"]["unpermitted_deliveries"] == [10]
    assert "findings_match_the_independent_oracle" in row["integrity_failures"]


def test_the_verdict_follows_the_stored_run_and_the_digest_catches_the_edit(copied):
    out, selection = copied
    edit(out, RACE_CASE, lambda campaign: fork_of(campaign)["body"]["final_state"].update(
        delivery_log=[row for row in fork_of(campaign)["body"]["final_state"]["delivery_log"]
                      if row["event_id"] != 10]))
    row = row_of(heldout.score_heldout(out, selection), RACE_CASE)
    assert row["fork"]["oracle"]["unpermitted_deliveries"] == []
    assert "evidence_digests_match" in row["integrity_failures"]


def test_a_coverage_claim_cant_turn_an_inconclusive_run_clean(copied):
    out, selection = copied

    def claim_tested(campaign):
        for coverage in campaign["coverage"].values():
            coverage.update(faults_triggered=True, complete=True)

    edit(out, UNTESTED_CASE, claim_tested)
    row = row_of(heldout.score_heldout(out, selection), UNTESTED_CASE)
    assert "coverage_matches_the_stored_runs" in row["integrity_failures"]


def test_the_demo_prints_what_the_runbook_shows(tmp_path, capsys):
    assert demo.main(["--out", str(tmp_path / "demo")]) == 0
    output = capsys.readouterr().out
    assert output.startswith("inference: mocked")
    for text in ('"mode": "lab"', '"first_divergence_event": 18', '"held_by": "guarded_boundary"',
                 '"error": "PermissionWithdrawnDenied"', '"reported_model_calls": 0',
                 '"outcome": "coverage_incomplete"', '"faults_triggered": false'):
        assert text in output
    runbook = (ROOT / "docs" / "demo.md").read_text("utf-8")
    for text in ("Inference is mocked", "Pending authorization", "journey-demo", "journey-replay",
                 "coverage_incomplete"):
        assert text in runbook


def test_a_case_that_raises_is_counted_not_dropped(tmp_path):
    selection = selected(RACE_CASE)
    (tmp_path / "errors.json").write_text(json.dumps({RACE_CASE: "RuntimeError"}), "utf-8")
    report = heldout.score_heldout(tmp_path, selection)
    assert report["counts"]["errored"] == report["counts"]["cases"] == 1
    assert report["counts"]["cases_with_integrity_failures"] == 1
    assert row_of(report, RACE_CASE)["error"] == "RuntimeError"
