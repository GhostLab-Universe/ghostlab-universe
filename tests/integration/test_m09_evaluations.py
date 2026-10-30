"""The evaluation suite and its gate: results come from artifacts, regressions block."""

import json
import shutil

import pytest

from evals import gate
from evals.cases import CASES
from evals.evidence import canonical_sha256
from evals.harness import SECRET
from evals.release import build_record, target_problems
from evals.run import DEFAULT_EXPECTED, run_all
from evals.score import score_directory
from ghostlab.core import run_agent
from ghostlab.policy import LAB

from conftest import ROOT

EXPECTED = json.loads(DEFAULT_EXPECTED.read_text("utf-8"))
TARGETS = json.loads((ROOT / "deploy" / "release-targets.json").read_text("utf-8"))
COMMIT = "a" * 40


def copy_of(evaluated, tmp_path):
    out = tmp_path / "copy"
    shutil.copytree(evaluated[0], out)
    return out


def edit_evidence(out, case_id, change):
    path = out / "evidence" / f"{case_id}.json"
    data = json.loads(path.read_text("utf-8"))
    change(data)
    path.write_text(json.dumps(data), "utf-8")


def fork(data, campaign="main"):
    return next(run for name, run in data["campaigns"][campaign]["runs"].items()
                if name.endswith("/fork"))


def failures(out):
    return gate.check(score_directory(out), EXPECTED)


def test_the_suite_runs_clean_and_matches_the_reviewed_expectations(evaluated):
    out, errors = evaluated
    assert errors == []
    report = score_directory(out)
    assert gate.check(report, EXPECTED) == []
    assert report["aggregate"]["cases"] == {"total": len(CASES), "passed": len(CASES)}


def test_both_complete_flows_are_covered(evaluated):
    cases = {case["case_id"]: case for case in score_directory(evaluated[0])["cases"]}
    assert cases["flow.refund"]["status"] == "passed"
    assert cases["flow.journey"]["status"] == "passed"
    assert cases["flow.refund"]["metrics"]["successful_campaigns"] == 1
    assert cases["flow.journey"]["metrics"]["human_decisions"]["decided"] == 1


def test_scoring_reads_files_and_is_repeatable(evaluated):
    first, second = score_directory(evaluated[0]), score_directory(evaluated[0])
    assert first == second


def test_two_runs_give_identical_fingerprints(evaluated, tmp_path):
    assert run_all(tmp_path, only="flow.journey") == []
    again = {c["case_id"]: c for c in score_directory(tmp_path)["cases"]}
    for case_id, case in again.items():
        if case["fingerprint"] is not None:
            assert case["fingerprint"] == EXPECTED[case_id]


def test_unknown_values_are_reported_as_null_not_zero(evaluated):
    report = score_directory(evaluated[0])
    assert report["aggregate"]["cost"]["total_minor"] is None
    assert report["aggregate"]["cost"]["per_successful_task"] is None
    assert report["aggregate"]["latency"]["provider_ms"] is None
    for case in report["cases"]:
        assert case["metrics"]["cost_per_successful_task"] is None
    usage = next(c for c in report["cases"] if c["case_id"] == "adversarial.provider_errors")
    assert usage["metrics"]["fixture_tokens"]["output"]["unknown_count"] > 0


def test_comparison_is_declared_deferred_not_claimed(evaluated):
    report = score_directory(evaluated[0])
    assert "random vs one-pass vs adaptive exploration" in [d["item"] for d in report["deferred"]]
    text = json.dumps(report).lower()
    assert "superior" not in text and "outperform" not in text


def test_an_altered_stored_run_blocks_promotion(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)

    def hide_the_double_refund(data):
        fork(data)["body"]["final_state"]["ledger"].pop()

    edit_evidence(out, "flow.refund", hide_the_double_refund)
    found = failures(out)
    assert any("evidence_digests_match" in item for item in found)
    assert any("flow.refund" in item for item in found)


def test_a_consistent_forgery_still_blocks_on_the_oracle(evaluated, tmp_path):
    """The attacker reseals the digest too; the verdicts no longer add up."""
    out = copy_of(evaluated, tmp_path)

    def forge(data):
        run = fork(data)
        run["body"]["final_state"]["ledger"].pop()
        run["sha256"] = canonical_sha256(run["body"])

    edit_evidence(out, "flow.refund", forge)
    found = failures(out)
    assert any("findings_match_the_independent_oracle" in item for item in found)
    assert any("fork_shows_the_double_refund" in item for item in found)


def test_a_controller_claim_cannot_overrule_the_evidence(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)

    def claim_everything_passed(data):
        for finding in data["campaigns"]["main"]["findings"]:
            finding["verdict"] = "passed"

    edit_evidence(out, "flow.journey", claim_everything_passed)
    assert any("findings_match_the_independent_oracle" in item for item in failures(out))


def test_an_altered_expectation_blocks_promotion(evaluated):
    report = score_directory(evaluated[0])
    changed = json.loads(json.dumps(EXPECTED))
    changed["flow.refund"]["campaigns"]["main"]["outcome"] = "failed"
    assert any("flow.refund.campaigns.main.outcome" in item
               for item in gate.check(report, changed))


def test_a_new_or_missing_case_blocks_promotion(evaluated, tmp_path):
    changed = {key: value for key, value in EXPECTED.items() if key != "flow.refund"}
    assert any("flow.refund: not in the reviewed" in item
               for item in gate.check(score_directory(evaluated[0]), changed))
    out = copy_of(evaluated, tmp_path)
    (out / "evidence" / "flow.journey.json").unlink()
    assert any("evidence_missing" in item for item in failures(out))


def test_a_credential_in_evidence_or_telemetry_blocks_promotion(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)
    edit_evidence(out, "flow.refund", lambda data: data["observations"].update(leak=SECRET))
    assert any("no_credential_in_evidence" in item for item in failures(out))

    out = tmp_path / "second"
    shutil.copytree(evaluated[0], out)
    path = out / "telemetry" / "flow.journey.main.jsonl"
    path.write_text(path.read_text("utf-8").replace("fixture-model", SECRET), "utf-8")
    assert any("no_credential_in_telemetry" in item for item in failures(out))


def test_a_report_claiming_live_inference_is_refused(evaluated):
    report = {**score_directory(evaluated[0]), "live_provider_contacted": True}
    assert "this suite runs on mocked inference only" in gate.check(report, EXPECTED)


def test_the_cli_exit_code_is_what_blocks_ci(evaluated, tmp_path, capsys):
    out = copy_of(evaluated, tmp_path)
    assert gate.main([str(out), "--expected", str(DEFAULT_EXPECTED)]) == 0
    edit_evidence(out, "flow.refund", lambda d: fork(d)["body"]["final_state"]["ledger"].pop())
    assert gate.main([str(out), "--expected", str(DEFAULT_EXPECTED)]) == 1
    assert '"gate": "blocked"' in capsys.readouterr().out


def test_a_real_behaviour_regression_blocks_promotion(tmp_path, monkeypatch):
    """The repaired adapter goes back to retrying: the suite itself must catch it."""
    monkeypatch.setattr("ghostlab.orchestration.engine.run_agent",
                        lambda world, adapter: run_agent(world, "vulnerable"))
    errors = run_all(tmp_path, only="flow.refund")
    report = score_directory(tmp_path)
    case = next(c for c in report["cases"] if c["case_id"] == "flow.refund")
    assert errors or "repair_checks_status_and_refunds_once" in case["failed_expectations"]
    assert any(item.startswith("flow.refund") for item in gate.check(report, EXPECTED))


def test_removing_the_guard_blocks_promotion(tmp_path, monkeypatch):
    """The repair runs unguarded: the check-to-delivery race is open again."""
    monkeypatch.setattr("ghostlab.orchestration.worlds.execution_mode_for", lambda purpose: LAB)
    errors = run_all(tmp_path, only="flow.journey_check_to_delivery_race")
    report = score_directory(tmp_path)
    case = next(c for c in report["cases"]
                if c["case_id"] == "flow.journey_check_to_delivery_race")
    assert errors or "only_the_boundary_closes_it" in case["failed_expectations"]
    assert any(item.startswith("flow.journey_check") for item in gate.check(report, EXPECTED))


# --- release evidence ----------------------------------------------------------------------

def release(report, **changes):
    return build_record(**{"report": report, "expected": EXPECTED, "targets": TARGETS,
                           "commit": COMMIT, "root": ROOT, **changes})


def test_a_clean_report_is_promotable_and_statuses_stay_separate(evaluated):
    record = release(score_directory(evaluated[0]))
    assert record["promotable"] and record["blockers"] == []
    assert record["runtime"]["status"] != record["live_capability"]["status"]
    assert record["live_capability"]["status"] == "not_started"
    assert record["customer_connected_use_allowed"] is False
    assert len(record["dependency"]["uv_lock_sha256"]) == 64
    assert record["rollback_target"]["commit"] == TARGETS["rollback_target"]["commit"]


def test_a_failing_regression_blocks_the_release_record(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)
    edit_evidence(out, "flow.refund", lambda d: fork(d)["body"]["final_state"]["ledger"].pop())
    record = release(score_directory(out))
    assert record["promotable"] is False and record["blockers"]


@pytest.mark.parametrize("change, message", [
    ({"rollback_target": None}, "no explicit rollback commit"),
    ({"rollback_target": {"commit": "a" * 40}}, "doesn't say what happens to the stores"),
    ({"schema": "other"}, "unknown targets schema")])
def test_targets_must_be_explicit(change, message):
    assert any(message in item for item in target_problems({**TARGETS, **change}))


def test_mocked_evidence_cannot_claim_a_live_pass():
    statuses = {**TARGETS["statuses"], "live_capability": {"status": "passed"}}
    assert any("compatibility report" in item
               for item in target_problems({**TARGETS, "statuses": statuses}))


def test_the_candidate_cant_be_its_own_rollback(evaluated):
    record = release(score_directory(evaluated[0]), commit=TARGETS["rollback_target"]["commit"])
    assert "the candidate equals its own rollback target" in record["blockers"]
