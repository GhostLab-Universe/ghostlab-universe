"""Summaries of completed work built from structured evidence only.

The text is a fixed template over verifier findings. No model output and no
reasoning trace is stored or reconstructed; every claim cites a finding id.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ghostlab.contracts import ContextItem, InvariantFinding, Provenance, Ref


@dataclass(frozen=True)
class SummaryResult:
    item: ContextItem
    evidence_refs: tuple[Ref, ...]


def summarize_findings(findings: Sequence[InvariantFinding], *, item_id: str, run_ref: Ref,
                       observed_at: str) -> SummaryResult:
    if not findings:
        raise ValueError("Nothing to summarize")
    if run_ref.kind != "run" or any(f.run_id != run_ref.id for f in findings):
        raise ValueError("All findings must belong to the referenced run")
    ordered = sorted(findings, key=lambda f: (f.verdict != "failed", f.invariant, f.finding_id))
    parts = []
    for finding in ordered:
        detail = f"{finding.invariant} {finding.verdict}"
        if finding.evidence_event_ids:
            detail += " at events " + ", ".join(map(str, finding.evidence_event_ids))
        if getattr(finding, "observed_minor", None) is not None \
                and finding.limit_minor is not None:
            detail += (f" (observed {finding.observed_minor} vs limit "
                       f"{finding.limit_minor} minor units)")
        elif getattr(finding, "observed_value", None) is not None \
                and getattr(finding, "limit_value", None) is not None:
            detail += (f" (observed {finding.observed_value} vs limit "
                       f"{finding.limit_value} {finding.unit})")
        parts.append(f"{detail} [{finding.finding_id}]")
    failed = sum(f.verdict == "failed" for f in findings)
    content = (f"Run {run_ref.id}: {len(findings)} verifier findings, {failed} failed. "
               + "; ".join(parts) + ".")
    item = ContextItem(
        item_id=item_id, kind="summary", content=content, artifact=None,
        provenance=Provenance(source_kind="verifier", source_ref=run_ref,
                              observed_at=observed_at, trust="verified"))
    refs = tuple(Ref(kind="finding", id=f.finding_id, sha256=None) for f in ordered)
    return SummaryResult(item, refs)
