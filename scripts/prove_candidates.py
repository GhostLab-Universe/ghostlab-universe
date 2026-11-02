"""T5 candidate-lifecycle proof: naive v1 -> fixed v2, end to end.

Registers the demo naive (v1) and fixed (v2) refund targets, proposes
a developer-supplied candidate v1 -> v2, runs it through
validation and multidimensional evaluation, promotes it through the
single-use digest-bound human approval flow, and then shows the
refusal of an out-of-allowlist change, an escape attempt, and
generated code. Exit 0 on full proof, 1 on any failed step, 2 on
usage errors. No model calls, no network, no credentials.
"""

import argparse
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.adapters import register_refund_adapters  # noqa: E402
from ghostlab.candidates.allowlist import (ChangeRefusal, code_identity_of,  # noqa: E402
                                           validate_change)
from ghostlab.candidates.demo_targets import (  # noqa: E402
    FIXED_ID, FIXED_VERSION, NAIVE_ID, NAIVE_VERSION, WORLD_VERSION,
    register_demo_targets)
from ghostlab.candidates.evaluate import run_candidate_evaluation  # noqa: E402
from ghostlab.candidates.service import (CandidateService,  # noqa: E402
                                         promotion_action)
from ghostlab.candidates.store import CandidateStore  # noqa: E402
from ghostlab.contracts import (ApprovalSubmission, Principal,  # noqa: E402
                                TrustedScope)
from ghostlab.extensions.registry import AdapterRegistry  # noqa: E402
from ghostlab.policy.approvals import (ApprovalService,  # noqa: E402
                                       ProjectApproverAllowlist)
from ghostlab.storage.approval_store import SqliteApprovalStore  # noqa: E402
from ghostlab.storage.migrations import migrate  # noqa: E402

PROJECT = "t5-proof"
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)

ORIGINAL = {"case_id": "timeout-after-commit",
            "fixture": {"order_id": "synthetic-order-1",
                        "customer_id": "synthetic-customer-1",
                        "paid_minor": 250_000, "currency": "INR"},
            "scenario": {"timeout_after_commit": True, "max_tool_calls": 20},
            "actor_id": "synthetic-customer-1",
            "task": {"task_id": "refund-1",
                     "params": {"order_id": "synthetic-order-1",
                                "customer_id": "synthetic-customer-1",
                                "amount_minor": 250_000}}}
REGRESSIONS = [{"case_id": "clean-refund",
                "fixture": dict(ORIGINAL["fixture"]),
                "scenario": {"timeout_after_commit": False,
                             "max_tool_calls": 20},
                "actor_id": "synthetic-customer-1",
                "task": {"task_id": "refund-2",
                         "params": dict(ORIGINAL["task"]["params"])}}]


def step(number, label):
    print(f"[prove-candidates] step {number}: {label}")


def endpoint(registry, target_id):
    manifest = registry.target_manifest(WORLD_VERSION, target_id)
    return {"target_id": target_id, "target_version": manifest.version,
            "code_identity": code_identity_of(manifest),
            "config_digest": None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None,
                        help="candidate store path (default: temp file)")
    args = parser.parse_args(argv)

    failures = []

    def check(label, condition, detail=""):
        print(f"  {'PASS' if condition else 'FAIL'} {label} {detail}".rstrip())
        if not condition:
            failures.append(label)

    step(1, "register demo naive v1 + fixed v2 targets")
    registry = AdapterRegistry()
    register_refund_adapters(registry)
    register_demo_targets(registry)
    naive = registry.target_manifest(WORLD_VERSION, NAIVE_ID)
    fixed = registry.target_manifest(WORLD_VERSION, FIXED_ID)
    check("naive registered", naive.version == NAIVE_VERSION,
          f"({NAIVE_ID}@{naive.version})")
    check("fixed registered", fixed.version == FIXED_VERSION,
          f"({FIXED_ID}@{fixed.version})")

    scratch = Path(tempfile.mkdtemp())
    db_path = args.db or str(scratch / "candidates.db")
    store = CandidateStore(db_path)
    approvals_db = scratch / 'approvals.db'
    migrate(approvals_db)
    approvals = ApprovalService(
        SqliteApprovalStore(approvals_db),
        approvers=ProjectApproverAllowlist({PROJECT: ["reviewer-1"]}))
    service = CandidateService(store, registry, WORLD_VERSION,
                               approvals=approvals)
    scope = TrustedScope.model_validate({
        "project_id": PROJECT,
        "principal": {"principal_id": "reviewer-1", "kind": "human"},
        "role": "analyst", "delegated_by": None, "allowed_tools": [],
        "policy_version": "policy-1", "issued_by": "controller"})
    parent = endpoint(registry, NAIVE_ID)
    proposed = endpoint(registry, FIXED_ID)
    diff = [{"kind": "target", "path": "target",
             "before": {"target_id": NAIVE_ID,
                        "target_version": NAIVE_VERSION},
             "after": {"target_id": FIXED_ID,
                       "target_version": FIXED_VERSION}}]

    step(2, "propose + validate candidate v1 -> v2")
    candidate = service.propose(
        scope=scope, mode="developer_supplied", parent=parent,
        proposed=proposed, diff=diff, evidence_refs=[],
        now_iso=NOW.isoformat())
    check("proposed", candidate["status"] == "proposed",
          f"({candidate['candidate_id']})")
    validated = service.validate(
        scope=scope, candidate_id=candidate["candidate_id"],
        now_iso=NOW.isoformat())
    check("validated", validated["status"] == "validated")

    step(3, "evaluate parent vs candidate (multidimensional)")
    evaluation = run_candidate_evaluation(
        registry=registry, world_version=WORLD_VERSION, project_id=PROJECT,
        candidate_id=candidate["candidate_id"], parent_id=NAIVE_ID,
        proposed_id=FIXED_ID, original=ORIGINAL, regressions=REGRESSIONS,
        cap=20, evaluated_by="reviewer-1", now_iso=NOW.isoformat())
    service.record_evaluation(scope=scope, evaluation=evaluation)
    parent_failed = [c["name"] for c in evaluation["original"]["parent"]["checks"]
                     if not c["passed"]]
    cand_failed = [c["name"]
                   for c in evaluation["original"]["candidate"]["checks"]
                   if not c["passed"]]
    check("parent fails original", bool(parent_failed),
          f"(failed={parent_failed})")
    check("candidate fixes original", not cand_failed)
    check("no aggregate verdict", "passed" not in evaluation)
    check("regressions recorded", len(evaluation["regressions"]) == 1)
    check("usage recorded",
          evaluation["usage"]["candidate"]["tool_calls"] > 0)

    step(4, "promote through human approval (single use)")
    promotion = service.request_promotion(
        scope=scope, candidate_id=candidate["candidate_id"],
        evaluation_id=evaluation["evaluation_id"], approval_id="appr-1",
        campaign_id="camp-1", task_id="task-1",
        summary="promote demo fix v1 -> v2",
        material_risks=["demo promotion; no production effect"],
        now=NOW, ttl=timedelta(hours=1))
    check("promotion pending", promotion["status"] == "pending")
    action = promotion_action(
        promotion_id=promotion["promotion_id"],
        candidate_id=candidate["candidate_id"], project_id=PROJECT,
        parent=parent, proposed=proposed,
        evaluation_digest=evaluation["digest"])
    approvals.decide(
        session_principal=Principal.model_validate(
            {"principal_id": "reviewer-1", "kind": "human"}),
        project_id=PROJECT,
        submission=ApprovalSubmission.model_validate({
            "schema_version": 1, "approval_id": "appr-1",
            "action_digest": promotion["action_digest"],
            "decision": "approve", "reason": "fix verified in proof"}),
        decision_id="dec-1", now=NOW + timedelta(minutes=1))
    done = service.complete_promotion(
        scope=scope, candidate_id=candidate["candidate_id"],
        promotion_id=promotion["promotion_id"], action=action,
        now=NOW + timedelta(minutes=2),
        decided_at=NOW.isoformat())
    check("promoted", done["status"] == "completed")
    check("decided_by from session",
          done["decided_by"] == "reviewer-1")
    check("candidate promoted",
          store.get_candidate(PROJECT, candidate["candidate_id"])["status"]
          == "promoted")

    step(5, "refuse out-of-allowlist change")
    bad_diff = diff + [{"kind": "field", "path": "run.evil_knob",
                        "before": 1, "after": 2}]
    try:
        validate_change(mode="developer_supplied", parent=parent,
                        proposed=proposed, diff=bad_diff, registry=registry,
                        world_version=WORLD_VERSION)
        check("out-of-allowlist refused", False)
    except ChangeRefusal as refusal:
        check("out-of-allowlist refused",
              refusal.code == "not_allowlisted", f"({refusal.code})")

    step(6, "refuse escape attempt + generated code")
    escape_diff = diff + [{"kind": "field", "path": "agent.endpoint",
                           "before": "a", "after": "b"}]
    try:
        validate_change(mode="developer_supplied", parent=parent,
                        proposed=proposed, diff=escape_diff,
                        registry=registry, world_version=WORLD_VERSION)
        check("escape refused", False)
    except ChangeRefusal as refusal:
        check("escape refused", refusal.code == "escape_attempt",
              f"({refusal.code})")
    code_diff = diff + [{"kind": "code", "path": "agent.patch",
                         "before": "", "after": "x"}]
    try:
        validate_change(mode="developer_supplied", parent=parent,
                        proposed=proposed, diff=code_diff, registry=registry,
                        world_version=WORLD_VERSION)
        check("generated code refused", False)
    except ChangeRefusal as refusal:
        check("generated code refused",
              refusal.code == "generated_code_refused",
              f"({refusal.code})")

    print(f"[prove-candidates] {6 - len(failures)}/6 refusal/lifecycle "
          f"groups clean; failures={failures or 'none'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
