"""Analyst structured outputs (``ghostlab-contracts/1.1``). Model-facing and verdict-free.

The deterministic verifier's verdicts are final. These outputs explain evidence
and suggest repairs for human review; nothing here can change a verdict, carry
identity or authority, or contain executable code.
"""

from typing import Literal

from pydantic import Field, model_validator

from .common import Ref
from .primitives import Identifier, SchemaVersion, ShortText, StrictModel, Text, require_unique
from .world_packs import AdapterId, PackVersion, WorldName, pack

CITABLE = {"finding", "event", "run", "artifact"}


def _check_citations(refs):
    bad = sorted({ref.kind for ref in refs} - CITABLE)
    if bad:
        raise ValueError(f"citations must be findings, events, runs or artifacts, not {bad}")


class EvidenceClaim(StrictModel):
    statement: Text
    invariant: WorldName | None
    cited: list[Ref] = Field(min_length=1, max_length=16)


class EvidenceInterpretation(StrictModel):
    """``interpret-evidence`` output. ``hypothesis_assessment`` is commentary, not a verdict."""

    schema_version: SchemaVersion
    world_version: PackVersion
    run_id: Identifier
    summary: Text
    claims: list[EvidenceClaim] = Field(min_length=1, max_length=16)
    hypothesis_assessment: Literal["supported", "not_supported", "inconclusive"]
    open_questions: list[ShortText] = Field(max_length=8)

    @model_validator(mode="after")
    def _check(self):
        names = pack(self.world_version).invariant_names
        for claim in self.claims:
            if claim.invariant is not None and claim.invariant not in names:
                raise ValueError(f"{claim.invariant!r} is not an invariant of {self.world_version}")
            _check_citations(claim.cited)
        return self


class RepairSuggestion(StrictModel):
    """``suggest-idempotency-repair`` output: a reviewable proposal, never executable code."""

    schema_version: SchemaVersion
    world_version: PackVersion
    title: ShortText
    target_invariants: list[WorldName] = Field(min_length=1, max_length=8)
    change_kind: Literal["use_checked_in_adapter", "describe_change_for_review"]
    adapter_id: AdapterId | None
    description: Text
    rationale: Text
    risks: list[ShortText] = Field(min_length=1, max_length=8)
    cited: list[Ref] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _check(self):
        world = pack(self.world_version)
        require_unique(self.target_invariants, "target_invariants")
        unknown = set(self.target_invariants) - world.invariant_names
        if unknown:
            raise ValueError(f"{sorted(unknown)} are not invariants of {self.world_version}")
        if self.change_kind == "use_checked_in_adapter":
            adapter = world.adapter(self.adapter_id) if self.adapter_id else None
            if adapter is None or adapter.kind != "repaired":
                raise ValueError("use_checked_in_adapter must name a repaired adapter of the pack")
        elif self.adapter_id is not None:
            raise ValueError("describe_change_for_review names no adapter")
        _check_citations(self.cited)
        return self
