"""The evaluation suite and its gate: results come from artifacts, regressions block."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

from evals import gate
from evals.cases import CASES
from evals.evidence import canonical_sha256, scan_stores
from evals.harness import FIXTURE_LABEL, SECRET, SYNTHETIC_SECRET
from evals.identity import code_sha256_at, evidence_sha256
from evals.release import (NO_VERIFIABLE_ORIGIN, REQUIRED_PROBES, SECURITY_GATES, build_record,
                           target_problems)
from evals.run import DEFAULT_EXPECTED, main as run_main
from evals.run import run_all
from evals.score import RUN_IDENTITY, score_directory
from ghostlab.budgets import BudgetAccountant
from ghostlab.observability import read_records
from ghostlab.core import run_agent
from ghostlab.orchestration import (REQUIRED_COMPATIBILITY_CHECKS, CampaignController,
                                    JourneyDriver, RefundDriver)
from ghostlab.orchestration import worlds
from ghostlab.policy import LAB

from conftest import ROOT

EXPECTED = json.loads(DEFAULT_EXPECTED.read_text("utf-8"))
TARGETS = json.loads((ROOT / "deploy" / "release-targets.json").read_text("utf-8"))
COMMIT = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                        text=True, check=True).stdout.strip()


def copy_of(evaluated, tmp_path):
    out = tmp_path / "copy"
    shutil.copytree(evaluated[0], out)
    return out


def edit_evidence(out, case_id, change):
    path = out / "evidence" / f"{case_id}.json"
    data = json.loads(path.read_text("utf-8"))
    change(data)
    path.write_text(json.dumps(data), "utf-8")


def seal(out, *, commit=COMMIT, dirty=False, code=None):
    """Write run.json and report.json the way evals.run does."""
    (out / RUN_IDENTITY).write_text(json.dumps({
        "commit": commit, "dirty": dirty, "package_path": "src/ghostlab",
        "code_sha256": code or code_sha256_at(ROOT, COMMIT),
        "evidence_sha256": evidence_sha256(out)}), "utf-8")
    (out / gate.REPORT_FILE).write_text(json.dumps(score_directory(out)), "utf-8")
    return out


def bound_copy(evaluated, tmp_path, **options):
    """Evidence as evals.run leaves it: tied to a commit, with its report stored."""
    out = tmp_path / "bound"
    shutil.copytree(evaluated[0], out)
    return seal(out, **options)


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
    assert any("every_invariant_verdict_matches_a_recomputation" in item for item in found)
    assert any("fork_shows_the_double_refund" in item for item in found)


def test_a_controller_claim_cannot_overrule_the_evidence(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)

    def claim_everything_passed(data):
        for finding in data["campaigns"]["main"]["findings"]:
            finding["verdict"] = "passed"

    edit_evidence(out, "flow.journey", claim_everything_passed)
    assert any("every_invariant_verdict_matches_a_recomputation" in item for item in failures(out))


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
    assert any("no_secret_in_evidence" in item for item in failures(out))

    out = tmp_path / "second"
    shutil.copytree(evaluated[0], out)
    path = out / "telemetry" / "flow.journey.main.jsonl"
    path.write_text(path.read_text("utf-8").replace("fixture-model", SECRET), "utf-8")
    assert any("no_secret_in_telemetry" in item for item in failures(out))


def test_the_synthetic_secret_counts_as_a_leak_too(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)
    edit_evidence(out, "flow.refund",
                  lambda data: data["observations"].update(leak=SYNTHETIC_SECRET))
    assert any("no_secret_in_evidence" in item for item in failures(out))


def test_store_scan_finds_both_secrets_and_names_neither(tmp_path):
    (tmp_path / "plain.bin").write_bytes(b"nothing")
    (tmp_path / "a.sqlite").write_bytes(b"\x00" + SECRET.encode())
    (tmp_path / "b.sqlite").write_bytes(SYNTHETIC_SECRET.encode() + b"\x00")
    scan = scan_stores(tmp_path)
    assert scan == {"files_scanned": 3,
                    "files_with_secret": ["a.sqlite:secret[0]", "b.sqlite:secret[1]"]}


def test_a_fault_claimed_without_evidence_blocks(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)

    def claim_the_fault_fired(data):
        for item in data["campaigns"]["refund_no_fault"]["coverage"].values():
            item["faults_triggered"] = item["complete"] = True

    edit_evidence(out, "adversarial.untested_fault", claim_the_fault_fired)
    assert any("coverage_matches_the_stored_runs" in item for item in failures(out))


def test_coverage_that_disagrees_with_the_stored_runs_blocks(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)

    def claim_full_coverage(data):
        for item in data["campaigns"]["journey_tool_cap"]["coverage"].values():
            item["complete"] = True

    edit_evidence(out, "adversarial.budget_exhaustion", claim_full_coverage)
    assert any("coverage_matches_the_stored_runs" in item for item in failures(out))


def test_a_report_claiming_live_inference_is_refused(evaluated):
    report = {**score_directory(evaluated[0]), "live_provider_contacted": True}
    assert "this suite runs on mocked inference only" in gate.check(report, EXPECTED)


def test_the_cli_exit_code_is_what_blocks_ci(evaluated, tmp_path, capsys):
    out = bound_copy(evaluated, tmp_path)
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


def test_storing_model_text_unredacted_blocks_promotion(tmp_path, monkeypatch):
    """The proposal scrub goes away: the store scan must catch the secret on disk."""
    monkeypatch.setattr("ghostlab.orchestration.controller.CampaignController"
                        "._redacted_proposal", lambda self, driver, proposal: proposal)
    assert run_all(tmp_path, only="model_echoed_secret.refund") == []
    report = score_directory(tmp_path)
    case = next(c for c in report["cases"]
                if c["case_id"] == "adversarial.model_echoed_secret.refund")
    assert "no_secret_in_stores" in case["failed_expectations"]
    assert "absent_from_returned_state" in case["failed_expectations"]


def test_calling_a_truncated_experiment_clean_blocks_promotion(tmp_path, monkeypatch):
    """Back to the old outcome: the experiment looks complete, so the campaign is clean."""
    real = CampaignController._execute

    def pretend_complete(self, state):
        update = real(self, state)
        for experiment in update.get("experiments", {}).values():
            experiment["coverage"]["complete"] = True
        return update

    monkeypatch.setattr(CampaignController, "_execute", pretend_complete)
    assert run_all(tmp_path, only="budget_exhaustion") == []
    report = score_directory(tmp_path)
    case = next(c for c in report["cases"] if c["case_id"] == "adversarial.budget_exhaustion")
    assert "a_truncated_experiment_proves_nothing" in case["failed_expectations"]
    assert "coverage_matches_the_stored_runs" in case["failed_expectations"]


def test_counting_an_unmet_fault_as_tested_blocks_promotion(tmp_path, monkeypatch):
    """Back to judging by run status alone: a clean run with no fault passes as clean."""
    monkeypatch.setattr(RefundDriver, "faults_triggered", lambda self, proposal, fork: True)
    monkeypatch.setattr(JourneyDriver, "faults_triggered", lambda self, proposal, fork: True)
    assert run_all(tmp_path, only="untested_fault") == []
    case = next(c for c in score_directory(tmp_path)["cases"]
                if c["case_id"] == "adversarial.untested_fault")
    assert "a_clean_run_that_met_no_fault_is_not_clean" in case["failed_expectations"]
    assert "coverage_matches_the_stored_runs" in case["failed_expectations"]


def test_dropping_the_identity_setting_blocks_promotion(tmp_path, monkeypatch):
    """Resume stops checking the binding, so a withdrawn identity reaches the approval."""
    for driver in (RefundDriver, JourneyDriver):
        monkeypatch.setattr(driver, "identity_problem", lambda self, *args: None)
    errors = run_all(tmp_path, only="permission_revocation")
    case = next(c for c in score_directory(tmp_path)["cases"]
                if c["case_id"] == "adversarial.permission_revocation")
    assert errors or "resume_refuses_a_withdrawn_or_changed_identity" in (
        case["failed_expectations"])
    assert case["status"] == "failed"


def test_ignoring_a_lost_fault_blocks_promotion(tmp_path, monkeypatch):
    """The repair gets credit for a fault it never met."""
    monkeypatch.setattr(JourneyDriver, "faults_fired", lambda self, proposal, run: set())
    assert run_all(tmp_path, only="repair_incomplete") == []
    case = next(c for c in score_directory(tmp_path)["cases"]
                if c["case_id"] == "adversarial.repair_incomplete")
    assert "a_repair_that_missed_a_fault_is_not_verified" in case["failed_expectations"]


def test_losing_consume_recovery_blocks_promotion(tmp_path, monkeypatch):
    """Recovery stops recognising its own consumed approval, so the case no longer completes."""
    monkeypatch.setattr(CampaignController, "_consumed_here", lambda self, state, approval: False)
    assert run_all(tmp_path, only="uncertain_commit") == []
    case = next(c for c in score_directory(tmp_path)["cases"]
                if c["case_id"] == "adversarial.uncertain_commit")
    assert case["status"] == "failed"


def test_removing_the_guard_blocks_promotion(tmp_path, monkeypatch):
    """The repair runs unguarded: the check-to-delivery race is open again."""
    monkeypatch.setattr("ghostlab.orchestration.worlds.execution_mode_for", lambda purpose: LAB)
    errors = run_all(tmp_path, only="flow.journey_check_to_delivery_race")
    report = score_directory(tmp_path)
    case = next(c for c in report["cases"]
                if c["case_id"] == "flow.journey_check_to_delivery_race")
    assert errors or "only_the_boundary_closes_it" in case["failed_expectations"]
    assert any(item.startswith("flow.journey_check") for item in gate.check(report, EXPECTED))


# --- verdicts: every invariant, and no silent gaps ---------------------------------------

def run_and_score(tmp_path, only):
    assert run_all(tmp_path, only=only) == []
    return {c["case_id"]: c for c in score_directory(tmp_path)["cases"]}


FINDING_MUTATIONS = ["authorized_refunds", "no_duplicate_full_refund",
                     "unique_idempotency_keys", "total_refund_within_payment"]
# Both fork failures are hidden for the first and third, and one of them for the others.


@pytest.mark.parametrize("kept", FINDING_MUTATIONS)
def test_findings_that_stop_reporting_an_invariant_block_promotion(tmp_path, monkeypatch, kept):
    """Probe 4: only one invariant keeps its real verdict, and the fork always had failures."""
    real = worlds.derive_findings

    def lenient(run_id, run, *, recorded_at):
        checks = {name: value if name == kept else True for name, value in run["checks"].items()}
        return real(run_id, {**run, "checks": checks}, recorded_at=recorded_at)

    monkeypatch.setattr(worlds, "derive_findings", lenient)
    errors = run_all(tmp_path, only="flow.refund")
    case = next(c for c in score_directory(tmp_path)["cases"] if c["case_id"] == "flow.refund")
    # With every failure hidden, the campaign plans again and asks for a reply nobody
    # scripted, so the run stops. Otherwise the verdicts must disagree.
    assert errors == ["flow.refund: run raised AssertionError"] or (
        "every_invariant_verdict_matches_a_recomputation" in case["failed_expectations"])


def test_a_verifier_that_misses_infeasibility_blocks_promotion(tmp_path, monkeypatch):
    """The third journey invariant now fails in a real case, so losing it shows."""
    real = JourneyDriver.findings

    def lenient(self, fixture, run_id, run, recorded_at, execution):
        return [{**item, "verdict": "passed"}
                if item["invariant"] == "mandatory_participants_feasible" else item
                for item in real(self, fixture, run_id, run, recorded_at, execution)]

    monkeypatch.setattr(JourneyDriver, "findings", lenient)
    cases = run_and_score(tmp_path, "stale_intent")
    assert "every_invariant_verdict_matches_a_recomputation" in (
        cases["adversarial.stale_intent"]["failed_expectations"])


@pytest.mark.parametrize("driver", [RefundDriver, JourneyDriver])
def test_a_run_with_no_findings_blocks_promotion(tmp_path, monkeypatch, driver):
    real = driver.findings

    def none_for_repair(self, fixture, run_id, run, recorded_at, execution):
        found = real(self, fixture, run_id, run, recorded_at, execution)
        return [] if "repair" in run_id else found

    monkeypatch.setattr(driver, "findings", none_for_repair)
    case_id = "flow.refund" if driver is RefundDriver else "flow.journey"
    cases = run_and_score(tmp_path, case_id)
    assert "every_invariant_verdict_matches_a_recomputation" in (
        cases[case_id]["failed_expectations"])


def test_deleting_or_flipping_findings_in_evidence_blocks(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)
    edit_evidence(out, "flow.refund", lambda d: d["campaigns"]["main"].update(findings=[]))

    def flip_a_passing_invariant(data):
        for finding in data["campaigns"]["main"]["findings"]:
            if finding["invariant"] == "unique_idempotency_keys":
                finding["verdict"] = "failed"

    edit_evidence(out, "flow.journey", lambda d: d["campaigns"]["main"].update(findings=[]))
    found = failures(out)
    assert any("flow.refund: failed expectation every_invariant" in item for item in found)
    assert any("flow.journey: failed expectation every_invariant" in item for item in found)
    out = tmp_path / "flip"
    shutil.copytree(evaluated[0], out)
    edit_evidence(out, "flow.refund", flip_a_passing_invariant)
    assert any("every_invariant_verdict_matches_a_recomputation" in item for item in failures(out))


def test_findings_are_pinned_in_the_fingerprint(evaluated):
    case = next(c for c in score_directory(evaluated[0])["cases"] if c["case_id"] == "flow.refund")
    pinned = [item[:3] for item in case["fingerprint"]["campaigns"]["main"]["findings"]]
    assert ["fork", "total_refund_within_payment", "failed"] in pinned
    assert ["repaired", "total_refund_within_payment", "passed"] in pinned


def _reseal(run):
    run["sha256"] = canonical_sha256(run["body"])


@pytest.mark.parametrize("name, change, expectation", [
    ("wrong_customer", lambda body: [row.update(customer_id="synthetic-customer-2")
                                     for event in body["trace"] for row in event["state"]["ledger"]],
     "stored_runs_are_internally_consistent"),
    ("final_state_differs", lambda body: body["final_state"]["ledger"].append(
        dict(body["final_state"]["ledger"][0])), "stored_runs_are_internally_consistent"),
    ("trace_renumbered", lambda body: body["trace"][0].update(event_id=99),
     "stored_runs_are_internally_consistent"),
    ("mutation_outside_issue_refund", lambda body: body["trace"][0]["state"]["ledger"].append(
        dict(body["final_state"]["ledger"][0])), "stored_runs_are_internally_consistent")])
def test_a_resealed_inconsistent_run_blocks(evaluated, tmp_path, name, change, expectation):
    out = copy_of(evaluated, tmp_path)

    def forge(data):
        run = data["campaigns"]["main"]["runs"]["repaired"]
        change(run["body"])
        _reseal(run)

    edit_evidence(out, "flow.refund", forge)
    assert any(f"flow.refund: failed expectation {expectation}" in item for item in failures(out))


def test_dropping_the_decision_event_blocks_promotion(tmp_path, monkeypatch):
    real = CampaignController._event

    def without_decisions(self, state, kind, summary, **kwargs):
        if kind == "approval_decided":
            return list(state.get("events", []))
        return real(self, state, kind, summary, **kwargs)

    monkeypatch.setattr(CampaignController, "_event", without_decisions)
    cases = run_and_score(tmp_path, "flow.refund")
    assert "approval_events_match_the_decisions" in cases["flow.refund"]["failed_expectations"]


def test_events_are_pinned_in_the_fingerprint(evaluated):
    case = next(c for c in score_directory(evaluated[0])["cases"] if c["case_id"] == "flow.refund")
    assert "approval_decided" in case["fingerprint"]["campaigns"]["main"]["events"]


def test_citing_the_wrong_events_blocks_promotion(tmp_path, monkeypatch):
    real = CampaignController._verify

    def wrong_events(self, state):
        update = real(self, state)
        update["findings"] = [{**item, "evidence_event_ids": [1] if item["evidence_event_ids"]
                               else []} for item in update["findings"]]
        return update

    monkeypatch.setattr(CampaignController, "_verify", wrong_events)
    cases = run_and_score(tmp_path, "flow.")
    for case_id in ("flow.refund", "flow.journey"):
        assert "cited_events_match_a_recomputation" in cases[case_id]["failed_expectations"]


def test_a_swapped_reality_diff_blocks_promotion(tmp_path, monkeypatch):
    real = worlds.RefundDriver.diff
    monkeypatch.setattr(worlds.RefundDriver, "diff",
                        lambda self, fixture, baseline, fork: real(self, fixture, fork, baseline))
    cases = run_and_score(tmp_path, "flow.refund")
    assert "reality_diff_matches_the_stored_runs" in cases["flow.refund"]["failed_expectations"]


def test_a_mocked_trace_says_so(evaluated):
    records, _ = read_records(evaluated[0] / "telemetry" / "flow.refund.main.jsonl")
    assert all(r["attributes"]["transport"] == "mock" for r in records
               if r["name"] in ("model.attempt", "campaign.run"))


def test_the_gate_says_the_inference_was_mocked(bound, capsys):
    assert gate.main([str(bound), "--expected", str(DEFAULT_EXPECTED)]) == 0
    printed = capsys.readouterr().out
    assert '"inference": "mocked"' in printed and FIXTURE_LABEL in printed


def test_a_memory_failure_blocks_promotion(tmp_path, monkeypatch):
    """Probe 4: nothing is stored, which the negative checks alone never noticed."""
    monkeypatch.setattr(CampaignController, "_capture", lambda self, state, findings, ref: [])
    cases = run_and_score(tmp_path, "flow.refund")
    assert "verified_findings_were_stored" in cases["flow.refund"]["failed_expectations"]


def test_a_ledger_that_stops_counting_tools_blocks_promotion(tmp_path, monkeypatch):
    """Probe 9: reconcile every tool reservation to 0."""
    real = BudgetAccountant.reconcile

    def uncounted(self, reservation, *args, **kwargs):
        if reservation.resource == "tool_calls":
            return real(self, reservation, 0)
        return real(self, reservation, *args, **kwargs)

    monkeypatch.setattr(BudgetAccountant, "reconcile", uncounted)
    cases = run_and_score(tmp_path, "flow.refund")
    assert "tool_ledger_matches_the_stored_runs" in cases["flow.refund"]["failed_expectations"]


def test_a_trace_reduced_to_its_root_span_blocks(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)
    path = out / "telemetry" / "flow.refund.main.jsonl"
    roots = [line for line in path.read_text("utf-8").splitlines()
             if json.loads(line)["name"] == "campaign.run"]
    path.write_text("\n".join(roots) + "\n", "utf-8")
    assert any("flow.refund: failed expectation trace_valid_and_complete" in item
               for item in failures(out))


def test_coverage_that_under_reports_a_refund_fault_blocks(evaluated, tmp_path):
    out = copy_of(evaluated, tmp_path)

    def under_report(data):
        for item in data["campaigns"]["main"]["coverage"].values():
            item["faults_triggered"] = item["complete"] = False

    edit_evidence(out, "flow.refund", under_report)
    assert any("flow.refund: failed expectation coverage_matches_the_stored_runs" in item
               for item in failures(out))


# --- the runner ----------------------------------------------------------------------------

def test_a_used_out_directory_is_refused(tmp_path, capsys):
    (tmp_path / "stale.json").write_text("{}", "utf-8")
    with pytest.raises(SystemExit) as stopped:
        run_main(["--out", str(tmp_path), "--only", "flow.refund"])
    assert stopped.value.code == 2 and "already used" in capsys.readouterr().err


def test_only_with_no_match_blocks(tmp_path, capsys):
    assert run_all(tmp_path / "a", only="no-such-case") == ["--only 'no-such-case' matched no case"]
    assert run_main(["--out", str(tmp_path / "b"), "--only", "no-such-case"]) == 1
    assert "matched no case" in capsys.readouterr().out


def test_expectations_cant_be_written_from_part_of_the_suite(tmp_path):
    with pytest.raises(SystemExit):
        run_main(["--out", str(tmp_path / "c"), "--only", "flow.refund", "--write-expected",
                  "--expected", str(tmp_path / "expected.json")])
    assert not (tmp_path / "expected.json").exists()


# --- release evidence ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def bound(evaluated, tmp_path_factory):
    return bound_copy(evaluated, tmp_path_factory.mktemp("bound"))


def release(evidence, **changes):
    return build_record(**{"evidence": evidence, "commit": COMMIT, "repo": ROOT, **changes})


def test_a_clean_report_is_promotable_and_statuses_stay_separate(bound):
    record = release(bound)
    assert record["promotable"] and record["blockers"] == []
    assert record["runtime"]["status"] != record["live_capability"]["status"]
    assert record["live_capability"]["status"] == "not_started"
    assert record["customer_connected_use_allowed"] is False
    assert len(record["dependency"]["uv_lock_sha256"]) == 64
    assert record["rollback_target"]["commit"] == TARGETS["rollback_target"]["commit"]
    assert record["evaluation"]["report_sha256"] == hashlib.sha256(
        (bound / gate.REPORT_FILE).read_bytes()).hexdigest()


def test_a_failing_regression_blocks_the_release_record(bound, tmp_path):
    out = tmp_path / "copy"
    shutil.copytree(bound, out)
    edit_evidence(out, "flow.refund", lambda d: fork(d)["body"]["final_state"]["ledger"].pop())
    record = release(out)
    assert record["promotable"] is False and record["blockers"]


def forged(bound, tmp_path, change):
    out = tmp_path / "forged"
    shutil.copytree(bound, out)
    path = out / gate.REPORT_FILE
    report = json.loads(path.read_text("utf-8"))
    change(report)
    path.write_text(json.dumps(report), "utf-8")
    return out


@pytest.mark.parametrize("name, change", [
    ("status_failed", lambda r: r["cases"][0].update(status="failed")),
    ("fake_counts", lambda r: r["aggregate"].update(cases={"total": 9999, "passed": 9999})),
    ("fake_label", lambda r: r.update(fixture_label="muse-spark-1.3 live benchmark")),
    ("expectation_removed", lambda r: r["cases"][0].update(expectations={}))])
def test_a_report_that_differs_from_the_evidence_is_refused(bound, tmp_path, name, change):
    record = release(forged(bound, tmp_path, change))
    assert record["promotable"] is False
    assert "report.json doesn't match the evidence" in record["blockers"]


def test_a_report_with_no_evidence_behind_it_is_refused(bound, tmp_path):
    out = tmp_path / "bare"
    out.mkdir()
    shutil.copy(bound / gate.REPORT_FILE, out / gate.REPORT_FILE)
    assert release(out)["promotable"] is False


def test_a_missing_report_file_is_refused(bound, tmp_path):
    out = tmp_path / "copy"
    shutil.copytree(bound, out)
    (out / gate.REPORT_FILE).unlink()
    assert "no report.json in the evidence directory" in release(out)["blockers"]


@pytest.mark.parametrize("change, message", [
    (lambda r: r["cases"][0].update(status="failed"), "status failed"),
    (lambda r: r["cases"][0]["expectations"].update(campaign_repair_verified=False),
     "failed expectation campaign_repair_verified"),
    (lambda r: r["cases"][0].update(expectations={}), "no expectations were measured"),
    (lambda r: r["aggregate"].update(cases={"total": 9999, "passed": 9999}),
     "the aggregate doesn't match the cases"),
    (lambda r: r.update(fixture_label="live benchmark"), "isn't this suite's label")])
def test_the_gate_reads_more_than_failed_expectations(evaluated, change, message):
    report = json.loads(json.dumps(score_directory(evaluated[0])))
    change(report)
    assert any(message in item for item in gate.check(report, EXPECTED))


def test_a_commit_that_doesnt_exist_is_refused(bound):
    record = release(bound, commit="1" * 40)
    assert "the candidate commit doesn't exist" in record["blockers"]
    assert "the evidence didn't come from the candidate commit" in record["blockers"]


def test_evidence_from_another_commit_is_refused(evaluated, tmp_path):
    out = bound_copy(evaluated, tmp_path, commit="2" * 40)
    assert "the evidence didn't come from the candidate commit" in release(out)["blockers"]


def test_a_run_that_rewrote_its_expectations_cant_be_released(tmp_path, monkeypatch):
    monkeypatch.setattr("evals.run.run_all", lambda out, only=None: [])
    monkeypatch.setattr("evals.run.run_identity", lambda: {
        "commit": COMMIT, "dirty": False, "package_path": "x", "code_sha256": "0" * 64})
    run_main(["--out", str(tmp_path / "out"), "--write-expected",
              "--expected", str(tmp_path / "expected.json")])
    assert json.loads((tmp_path / "out" / gate.REPORT_FILE).read_text("utf-8"))["dirty"] is True


def test_a_tree_that_changes_during_the_run_is_dirty(tmp_path, monkeypatch):
    seen = iter([False, True])
    monkeypatch.setattr("evals.run.run_all", lambda out, only=None: [])
    monkeypatch.setattr("evals.run.run_identity", lambda: {
        "commit": COMMIT, "dirty": next(seen), "package_path": "x", "code_sha256": "0" * 64})
    run_main(["--out", str(tmp_path / "out"), "--expected", str(tmp_path / "expected.json")])
    assert json.loads((tmp_path / "out" / gate.REPORT_FILE).read_text("utf-8"))["dirty"] is True


def test_the_release_ignores_a_working_tree_that_blesses_a_regression(bound, tmp_path):
    """Probe: regress, rewrite expected.json locally, release. The commit's own file decides."""
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    objects = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute",
                              "--git-common-dir"], capture_output=True, text=True,
                             check=True).stdout.strip()
    # A checkout that shares ROOT's objects and holds anything in its working tree.
    alternates = clone / ".git" / "objects" / "info" / "alternates"
    alternates.write_bytes(f"{objects}/objects\n".encode())  # no CRLF: git reads the raw line
    (clone / "evals").mkdir()
    out = tmp_path / "regressed"
    shutil.copytree(bound, out)
    edit_evidence(out, "flow.refund", lambda d: d["observations"].update(regressed=True))
    seal(out)
    blessed = json.dumps(gate.expected_from(score_directory(out)), indent=1, sort_keys=True)
    (clone / "evals" / "expected.json").write_text(blessed, "utf-8")
    (clone / "uv.lock").write_text("changed locally", "utf-8")
    record = release(out, repo=clone)
    assert record["promotable"] is False
    assert any("flow.refund.observations.regressed" in item
               for item in record["blockers"]), record["blockers"]
    assert record["evaluation"]["expected_sha256"] == hashlib.sha256(
        subprocess.run(["git", "-C", str(ROOT), "show", f"{COMMIT}:evals/expected.json"],
                       capture_output=True, check=True).stdout.replace(b"\r\n", b"\n")).hexdigest()
    assert record["dependency"]["uv_lock_sha256"] == release(bound)["dependency"]["uv_lock_sha256"]


def test_mutant_code_on_the_path_cant_claim_the_clean_commit(tmp_path):
    """Probe: a mutated package is imported, and git still reports a clean commit."""
    mutant = tmp_path / "mutant"
    shutil.copytree(ROOT / "src", mutant / "src")
    for part in ("skills", "prompts", "migrations"):
        shutil.copytree(ROOT / part, mutant / part)
    path = mutant / "src" / "ghostlab" / "core.py"
    path.write_text(path.read_text("utf-8") + "\n# mutated\n", "utf-8")
    out = tmp_path / "out"
    done = subprocess.run(
        [sys.executable, "-m", "evals.run", "--out", str(out), "--only", "flow.refund",
         "--expected", str(tmp_path / "none.json")], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(mutant / "src")}, timeout=300, check=False)
    assert done.returncode == 1
    report = json.loads((out / gate.REPORT_FILE).read_text("utf-8"))
    assert report["package_path"].startswith(str(mutant.resolve()))
    assert report["code_sha256"] != code_sha256_at(ROOT, COMMIT)
    identity = json.loads((out / RUN_IDENTITY).read_text("utf-8"))
    (out / RUN_IDENTITY).write_text(json.dumps({**identity, "commit": COMMIT, "dirty": False}),
                                    "utf-8")
    (out / gate.REPORT_FILE).write_text(json.dumps(score_directory(out)), "utf-8")
    assert "the code that ran isn't the candidate's code" in release(out)["blockers"]


def test_restamping_evidence_onto_a_commit_with_other_code_is_refused(evaluated, tmp_path):
    history = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--max-count=40", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.split()
    other = next((sha for sha in history
                  if code_sha256_at(ROOT, sha) != code_sha256_at(ROOT, COMMIT)), None)
    if other is None:
        pytest.skip("no other code in this checkout's history")
    out = bound_copy(evaluated, tmp_path, commit=other)
    assert "the code that ran isn't the candidate's code" in release(out, commit=other)["blockers"]


def test_evidence_edited_after_the_run_breaks_the_seal(bound, tmp_path):
    out = tmp_path / "copy"
    shutil.copytree(bound, out)
    edit_evidence(out, "flow.refund", lambda d: d["observations"].update(edited=True))
    assert "run.json doesn't match the evidence files" in release(out)["blockers"]
    assert gate.main([str(out), "--expected", str(DEFAULT_EXPECTED)]) == 1


def test_evidence_from_a_dirty_tree_is_refused(evaluated, tmp_path):
    out = bound_copy(evaluated, tmp_path, dirty=True)
    assert "the evidence came from a tree with local changes" in release(out)["blockers"]


@pytest.mark.parametrize("change, message", [
    ({"rollback_target": None}, "no explicit rollback commit"),
    ({"rollback_target": {"commit": "a" * 40}}, "doesn't say what happens to the stores"),
    ({"schema": "other"}, "unknown targets schema")])
def test_targets_must_be_explicit(change, message):
    assert any(message in item for item in target_problems({**TARGETS, **change}))


def test_a_live_claim_without_a_report_proves_nothing_and_leaves_promotion_alone(bound):
    statuses = {**TARGETS["statuses"], "live_capability": {"status": "passed"}}
    record = release(bound, targets={**TARGETS, "statuses": statuses})
    assert record["live_evidence"]["validated"] is False
    assert record["customer_connected_use_allowed"] is False
    assert record["promotable"] is True


def test_the_candidate_cant_be_its_own_rollback(bound):
    record = release(bound, commit=TARGETS["rollback_target"]["commit"], targets=TARGETS)
    assert "the candidate equals its own rollback target" in record["blockers"]


# --- customer-connected use ----------------------------------------------------------------

PROVIDER, ENDPOINT, MODEL = "meta_responses", "https://api.meta.ai/v1", "muse-spark-1.3"


def probe_module():
    spec = importlib.util.spec_from_file_location("probe_provider",
                                                  ROOT / "scripts" / "probe_provider.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_report(tmp_path) -> dict:
    """What a live run of today's probe writes, built from a real dry run of it.

    Dry-run fields are replaced with the ones its live branch writes. No call is made.
    """
    path = tmp_path / "dry-run.json"
    assert probe_module().main(["--provider", PROVIDER, "--report", str(path)]) == 0
    report = json.loads(path.read_text("utf-8"))
    for name in ("planned", "note"):
        report.pop(name)
    return {**report, "live": True, "mode": "live", "transport": "network", "model": MODEL,
            "endpoint": ENDPOINT, "max_calls": 5, "calls_made": 5, "http_attempts": 5,
            "checks": [{"check": name, "status": "passed", "detail": "fixture"}
                       for name in sorted(REQUIRED_COMPATIBILITY_CHECKS)]}


def future_report(tmp_path) -> dict:
    """NOT something any probe writes today. The control the mutations below need.

    It adds what a future probe must prove: both output schemas and the AGENTS.md limits.
    """
    report = probe_report(tmp_path)
    return {**report, "structured_schemas": ["experiment_proposal", "world_experiment_proposal"],
            "probed_limits": list(REQUIRED_PROBES),
            "not_probed": {key: value for key, value in report["not_probed"].items()
                           if "timeout" not in key}}


def customer_target(root, report) -> dict:
    """A fully evidenced future target."""
    (root / "web").mkdir(exist_ok=True)
    (root / "uv.lock").write_text("lock", "utf-8")
    (root / "web" / "package-lock.json").write_text("{}", "utf-8")
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    gates = {}
    for gate_name in SECURITY_GATES:
        (evidence / f"{gate_name}.md").write_text("reviewed", "utf-8")
        gates[gate_name] = {"status": "passed", "evidence": f"evidence/{gate_name}.md"}
    (evidence / "report.json").write_text(json.dumps(report), "utf-8")
    return {**json.loads(json.dumps(TARGETS)), "security_gates": gates,
            "release_target": {"kind": "customer_connected"},
            "statuses": {**TARGETS["statuses"],
                         "runtime": {"status": "authenticated_service"},
                         "live_capability": {
                             "status": "passed", "provider": PROVIDER, "endpoint": ENDPOINT,
                             "model": MODEL, "max_model_calls": 5, "confirm_paid_calls": True,
                             "compatibility_report": "evidence/report.json"}}}


def rewrite_report(root, **changes):
    path = root / "evidence" / "report.json"
    path.write_text(json.dumps({**json.loads(path.read_text("utf-8")), **changes}), "utf-8")


def test_no_report_todays_probe_can_write_allows_customer_use(bound, tmp_path):
    """Unreachable today, and that's correct: the probe doesn't test the required limits."""
    (tmp_path / "root").mkdir()
    root = tmp_path / "root"
    targets = customer_target(root, probe_report(tmp_path))
    record = release(bound, targets=targets, root=root)
    assert record["customer_connected_use_allowed"] is False
    assert record["live_evidence"]["validated"] is False
    assert record["promotable"] is True


def test_the_future_shape_differs_from_the_probe_only_by_what_it_must_prove(tmp_path):
    written, future = probe_report(tmp_path), future_report(tmp_path)
    assert set(future) - set(written) == {"structured_schemas", "probed_limits"}
    assert "timeout_cancel_rate_limit" in written["not_probed"]


def test_customer_use_stays_false_with_every_requirement_met(bound, tmp_path):
    """Probe: all of this can be typed by hand, so meeting it proves nothing yet."""
    report = future_report(tmp_path)
    (tmp_path / "root").mkdir()
    record = release(bound, targets=customer_target(tmp_path / "root", report),
                     root=tmp_path / "root")
    assert record["customer_connected_use_allowed"] is False
    assert record["customer_connected_why_not"] == NO_VERIFIABLE_ORIGIN
    assert record["customer_connected_blockers"] == [NO_VERIFIABLE_ORIGIN]
    assert record["live_evidence"] == {"validated": True, "problems": []}
    assert record["promotable"] is True


def test_the_reviewers_reproduction_stays_false(bound):
    """Live status "passed", a report path that doesn't exist, a local runtime."""
    statuses = {**TARGETS["statuses"], "live_capability": {
        "status": "passed", "provider": PROVIDER, "endpoint": ENDPOINT, "model": MODEL,
        "compatibility_report": "does/not/exist.json"}}
    record = release(bound, targets={**TARGETS, "statuses": statuses})
    assert record["customer_connected_use_allowed"] is False
    assert record["live_evidence"]["validated"] is False
    assert record["promotable"] is True  # offline promotion isn't this decision


def test_a_local_target_stays_false_even_with_perfect_evidence(bound, tmp_path):
    report = future_report(tmp_path)
    (tmp_path / "root").mkdir()
    root = tmp_path / "root"
    targets = customer_target(root, report)
    targets["release_target"] = {"kind": "local_single_user"}
    record = release(bound, targets=targets, root=root)
    assert record["live_evidence"]["validated"] is True
    assert record["customer_connected_use_allowed"] is False
    assert record["customer_connected_blockers"] == [NO_VERIFIABLE_ORIGIN,
                                                     "this release target is local-only"]
    assert record["promotable"] is True


def _set(path, value):
    def change(targets, root):
        node = targets
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return change


def _drop_gate(name):
    return lambda targets, root: targets["security_gates"].pop(name)


def _report(**changes):
    return lambda targets, root: rewrite_report(root, **changes)


def _raw_report(content):
    def change(targets, root):
        (root / "evidence" / "report.json").write_text(content, "utf-8")
    return change


def _without_report_key(name):
    def change(targets, root):
        path = root / "evidence" / "report.json"
        report = json.loads(path.read_text("utf-8"))
        del report[name]
        path.write_text(json.dumps(report), "utf-8")
    return change


def _checks(*statuses):
    names = sorted(REQUIRED_COMPATIBILITY_CHECKS)
    return _report(checks=[{"check": name, "status": status}
                           for name, status in zip(names, statuses)])


def _empty_gate_file(targets, root):
    (root / "evidence" / f"{SECURITY_GATES[0]}.md").write_text("", "utf-8")


LIVE = ("statuses", "live_capability")
FIRST_GATE = ("security_gates", SECURITY_GATES[0])
MUTATIONS = {
    "local_kind": _set(("release_target", "kind"), "local_single_user"),
    "local_runtime": _set(("statuses", "runtime", "status"), "local_development_only"),
    "live_not_started": _set((*LIVE, "status"), "not_started"),
    "no_provider": _set((*LIVE, "provider"), None),
    "no_endpoint": _set((*LIVE, "endpoint"), None),
    "no_model": _set((*LIVE, "model"), None),
    "nonexistent_report": _set((*LIVE, "compatibility_report"), "evidence/missing.json"),
    "empty_report_path": _set((*LIVE, "compatibility_report"), ""),
    "report_path_is_a_directory": _set((*LIVE, "compatibility_report"), "evidence"),
    "report_outside_the_root": _set((*LIVE, "compatibility_report"), "../outside.json"),
    "paid_calls_unconfirmed": _set((*LIVE, "confirm_paid_calls"), False),
    "no_spending_cap": _set((*LIVE, "max_model_calls"), None),
    "cap_above_the_ceiling": _set((*LIVE, "max_model_calls"), 10**9),
    "report_not_json": _raw_report("not json"),
    "report_not_an_object": _raw_report("[]"),
    "report_is_mocked": _report(live=False, mode="mock", transport="mock"),
    "report_transport_mock": _report(transport="mock"),
    "report_for_another_model": _report(model="another-model"),
    "report_for_another_provider": _report(provider="baseten_chat_completions"),
    "report_for_another_endpoint": _report(endpoint="https://example.invalid/v1"),
    "report_with_no_checks": _report(checks=[]),
    "report_with_a_missing_check": _checks("passed"),
    "report_with_a_failed_check": _checks("failed", *["passed"] * 4),
    "report_with_a_skipped_check": _checks("skipped", *["passed"] * 4),
    "report_holds_a_secret": _report(note=f"key {SECRET}"),
    "report_covers_only_the_refund_schema": _report(structured_schemas=["experiment_proposal"]),
    "report_names_no_schemas": _without_report_key("structured_schemas"),
    "invented_provider": _set((*LIVE, "provider"), "not_a_real_provider"),
    "plain_http_endpoint": _set((*LIVE, "endpoint"), "http://127.0.0.1:9/v1"),
    "undocumented_endpoint": _set((*LIVE, "endpoint"), "https://example.invalid/v1"),
    "invented_model": _set((*LIVE, "model"), "invented-model-0"),
    "report_made_zero_calls": _report(calls_made=0, http_attempts=0),
    "report_has_no_call_counts": _without_report_key("calls_made"),
    "report_lists_timeouts_as_not_probed": _report(
        not_probed={"timeout_cancel_rate_limit": "not provoked"}),
    "report_lists_budgets_as_not_probed": _report(not_probed={"budget_enforcement": "no"}),
    "report_hides_a_limit_in_capitals": _report(not_probed={"TIMEOUT_Cancel": "no"}),
    "report_has_no_not_probed_list": _without_report_key("not_probed"),
    "report_omits_a_required_probe": _report(probed_limits=["timeout", "cancel", "rate_limit"]),
    "report_of_unknown_version": _report(report_version=2),
    "gate_evidence_empty": _empty_gate_file,
    "gate_marked_failed": _set((*FIRST_GATE, "status"), "failed"),
    "gate_evidence_missing": _set((*FIRST_GATE, "evidence"), "evidence/missing.md"),
    "gate_evidence_blank": _set((*FIRST_GATE, "evidence"), " "),
    **{f"no_{gate}": _drop_gate(gate) for gate in SECURITY_GATES},
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_any_missing_requirement_blocks_customer_use_and_nothing_else(bound, tmp_path, name):
    (tmp_path / "root").mkdir()
    root = tmp_path / "root"
    report = future_report(tmp_path)
    (tmp_path / "outside.json").write_text(json.dumps(report), "utf-8")
    targets = customer_target(root, report)
    MUTATIONS[name](targets, root)
    record = release(bound, targets=targets, root=root)
    assert record["customer_connected_use_allowed"] is False
    assert record["customer_connected_blockers"] != [NO_VERIFIABLE_ORIGIN]  # it was noticed
    assert record["promotable"] is True, record["blockers"]


def test_a_failed_offline_gate_also_blocks_customer_use(bound, tmp_path):
    out = forged(bound, tmp_path, lambda r: r.update(live_provider_contacted=True))
    (tmp_path / "root").mkdir()
    root = tmp_path / "root"
    record = release(out, targets=customer_target(root, future_report(tmp_path)), root=root)
    assert record["promotable"] is False
    assert record["customer_connected_use_allowed"] is False


def test_an_unknown_target_kind_blocks_promotion():
    assert any("unknown release target kind" in item for item in target_problems(
        {**TARGETS, "release_target": {"kind": "production"}}))
