"""The evaluation suite and its gate: results come from artifacts, regressions block."""

import json
import shutil

import pytest

from evals import gate
from evals.cases import CASES
from evals.evidence import canonical_sha256, scan_stores
from evals.harness import SECRET, SYNTHETIC_SECRET
from evals.release import SECURITY_GATES, build_record, target_problems
from evals.run import DEFAULT_EXPECTED, run_all
from evals.score import score_directory
from ghostlab.core import run_agent
from ghostlab.orchestration import (REQUIRED_COMPATIBILITY_CHECKS, CampaignController,
                                    JourneyDriver, RefundDriver)
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


def test_a_live_claim_without_a_report_proves_nothing_and_leaves_promotion_alone(evaluated):
    statuses = {**TARGETS["statuses"], "live_capability": {"status": "passed"}}
    record = release(score_directory(evaluated[0]), targets={**TARGETS, "statuses": statuses})
    assert record["live_evidence"]["validated"] is False
    assert record["customer_connected_use_allowed"] is False
    assert record["promotable"] is True


def test_the_candidate_cant_be_its_own_rollback(evaluated):
    record = release(score_directory(evaluated[0]), commit=TARGETS["rollback_target"]["commit"])
    assert "the candidate equals its own rollback target" in record["blockers"]


# --- customer-connected use ----------------------------------------------------------------

PROVIDER, ENDPOINT, MODEL = "meta_responses", "https://api.meta.ai/v1", "fixture-live-model"


@pytest.fixture(scope="module")
def clean_report(evaluated):
    return score_directory(evaluated[0])


def valid_report() -> dict:
    """A labeled test fixture shaped like a passing live M01 report. No call was made."""
    return {"live": True, "mode": "live", "transport": "network", "provider": PROVIDER,
            "endpoint": ENDPOINT, "model": MODEL,
            "structured_schemas": ["experiment_proposal", "world_experiment_proposal"],
            "checks": [{"check": name, "status": "passed"}
                       for name in sorted(REQUIRED_COMPATIBILITY_CHECKS)]}


def customer_target(root) -> dict:
    """A fully evidenced future target: the positive control for every test below."""
    (root / "web").mkdir(exist_ok=True)
    (root / "uv.lock").write_text("lock", "utf-8")
    (root / "web" / "package-lock.json").write_text("{}", "utf-8")
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    gates = {}
    for gate in SECURITY_GATES:
        (evidence / f"{gate}.md").write_text("reviewed", "utf-8")
        gates[gate] = {"status": "passed", "evidence": f"evidence/{gate}.md"}
    (evidence / "report.json").write_text(json.dumps(valid_report()), "utf-8")
    return {**json.loads(json.dumps(TARGETS)), "security_gates": gates,
            "release_target": {"kind": "customer_connected"},
            "statuses": {**TARGETS["statuses"],
                         "runtime": {"status": "authenticated_service"},
                         "live_capability": {
                             "status": "passed", "provider": PROVIDER, "endpoint": ENDPOINT,
                             "model": MODEL, "max_model_calls": 5, "confirm_paid_calls": True,
                             "compatibility_report": "evidence/report.json"}}}


def live_of(targets):
    return targets["statuses"]["live_capability"]


def rewrite_report(root, **changes):
    (root / "evidence" / "report.json").write_text(
        json.dumps({**valid_report(), **changes}), "utf-8")


def test_a_fully_evidenced_customer_target_is_allowed(clean_report, tmp_path):
    record = release(clean_report, targets=customer_target(tmp_path), root=tmp_path)
    assert record["customer_connected_use_allowed"] is True
    assert record["customer_connected_blockers"] == []
    assert record["live_evidence"] == {"validated": True, "problems": []}


def test_the_reviewers_reproduction_stays_false(clean_report):
    """Live status "passed", a report path that doesn't exist, a local runtime."""
    statuses = {**TARGETS["statuses"], "live_capability": {
        "status": "passed", "provider": PROVIDER, "endpoint": ENDPOINT, "model": MODEL,
        "compatibility_report": "does/not/exist.json"}}
    record = release(clean_report, targets={**TARGETS, "statuses": statuses})
    assert record["customer_connected_use_allowed"] is False
    assert record["live_evidence"]["validated"] is False
    assert record["promotable"] is True  # offline promotion isn't this decision


def test_a_local_target_stays_false_even_with_perfect_evidence(clean_report, tmp_path):
    targets = customer_target(tmp_path)
    targets["release_target"] = {"kind": "local_single_user"}
    record = release(clean_report, targets=targets, root=tmp_path)
    assert record["live_evidence"]["validated"] is True
    assert record["customer_connected_use_allowed"] is False
    assert record["customer_connected_blockers"] == ["this release target is local-only"]
    assert record["promotable"] is True


def _set(path, value):
    def change(targets, root):
        node = targets
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return change


def _drop_gate(gate):
    return lambda targets, root: targets["security_gates"].pop(gate)


def _report(**changes):
    return lambda targets, root: rewrite_report(root, **changes)


def _raw_report(content):
    def change(targets, root):
        (root / "evidence" / "report.json").write_text(content, "utf-8")
    return change


def _checks(*statuses):
    names = sorted(REQUIRED_COMPATIBILITY_CHECKS)
    return _report(checks=[{"check": name, "status": status}
                           for name, status in zip(names, statuses)])


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
    "report_names_no_schemas": _raw_report(json.dumps(
        {key: value for key, value in valid_report().items() if key != "structured_schemas"})),
    "gate_marked_failed": _set((*FIRST_GATE, "status"), "failed"),
    "gate_evidence_missing": _set((*FIRST_GATE, "evidence"), "evidence/missing.md"),
    "gate_evidence_blank": _set((*FIRST_GATE, "evidence"), " "),
    **{f"no_{gate}": _drop_gate(gate) for gate in SECURITY_GATES},
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_any_missing_requirement_blocks_customer_use_and_nothing_else(clean_report, tmp_path,
                                                                      name):
    (tmp_path / "root").mkdir()
    root = tmp_path / "root"
    (tmp_path / "outside.json").write_text(json.dumps(valid_report()), "utf-8")
    targets = customer_target(root)
    MUTATIONS[name](targets, root)
    record = release(clean_report, targets=targets, root=root)
    assert record["customer_connected_use_allowed"] is False
    assert record["customer_connected_blockers"]
    assert record["promotable"] is True, record["blockers"]


def test_a_failed_offline_gate_also_blocks_customer_use(clean_report, tmp_path):
    bad = {**clean_report, "live_provider_contacted": True}
    record = release(bad, targets=customer_target(tmp_path), root=tmp_path)
    assert record["promotable"] is False
    assert record["customer_connected_use_allowed"] is False


def test_an_unknown_target_kind_blocks_promotion():
    assert any("unknown release target kind" in item for item in target_problems(
        {**TARGETS, "release_target": {"kind": "production"}}))
