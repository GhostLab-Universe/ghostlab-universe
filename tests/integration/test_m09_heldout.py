"""The held-out evaluation: the timings are chosen by rule, and tuned ones can't get in."""

import json

from evals import heldout
from evals.evidence import canonical_sha256
from ghostlab.worlds.journey import JourneyFixture

from conftest import ROOT

RULE = json.loads(heldout.RULE_PATH.read_text("utf-8"))


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
