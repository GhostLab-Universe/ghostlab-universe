"""Shared M04 test helpers built from the published M00 examples (temporary stores only)."""

import json
from pathlib import Path

from ghostlab.contracts import (ApprovalDecision, ApprovalRequest, BudgetReservation,
                                MemoryRecord, TrustedScope)
from ghostlab.storage.database import open_database

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "contracts"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def scope(project_id: str = "project-local") -> TrustedScope:
    return TrustedScope.model_validate({**example("trusted_scope.explorer"),
                                        "project_id": project_id})


def memory(record_id: str = "memory-1", project_id: str = "project-local", **changes):
    return MemoryRecord.model_validate({**example("memory_record.verified_finding"),
                                        "record_id": record_id, "project_id": project_id,
                                        **changes})


def approval(approval_id: str = "approval-1", project_id: str = "project-local", **changes):
    data = example("approval_request.apply_repair")
    data["scope"] = {**data["scope"], "project_id": project_id}
    return ApprovalRequest.model_validate({**data, "approval_id": approval_id, **changes})


def decision(approval_id: str = "approval-1", **changes) -> ApprovalDecision:
    return ApprovalDecision.model_validate({**example("approval_decision.approved"),
                                            "approval_id": approval_id, **changes})


def reservation(reservation_id: str, amount: int = 1, **changes) -> BudgetReservation:
    return BudgetReservation.model_validate({**example("budget_reservation.reserved"),
                                             "reservation_id": reservation_id,
                                             "amount": amount, **changes})


def query_scope_free_memory_query(**changes):
    from ghostlab.contracts import MemoryQuery
    return MemoryQuery.model_validate({
        "schema_version": 1, "project_id": "project-local", "campaign_id": None,
        "world_version": None, "kinds": ["finding", "preference", "episodic_event"],
        "statuses": ["verified", "unverified", "contradicted", "superseded"],
        "as_of": "2026-10-29T12:00:00Z", "limit": 200, **changes})


def database(tmp_path: Path) -> Path:
    return open_database(tmp_path / "ghostlab.sqlite")
