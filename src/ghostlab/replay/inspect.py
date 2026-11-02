"""Checksum-first capsule inspection: evidence without execution.

:func:`inspect_capsule` validates one parsed capsule envelope (v1, v2 or
v3) and reports which reproduction modes it supports: read-only evidence
inspection, recorded-action replay, or fresh-agent retest. Inspection never
executes tools, calls models, or touches the network: it only validates the
envelope and reads its checksum-verified metadata.

Every mode decision is explicit. A capsule that cannot replay says why
(external dependencies, truncation, or a retest kind). Unknown worlds are a
replay-time failure, never an inspection failure: inspection is
registry-free.
"""

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ghostlab import capsule as refund_capsule
from ghostlab.contracts.capsule_v3 import CapsuleV3, capsule_digest
from ghostlab.contracts.world import CapsuleV2

RECORDED = "recorded"
RETEST = "retest"


@dataclass(frozen=True)
class ModeSupport:
    """Whether one reproduction mode fits this capsule, and why."""

    supported: bool
    detail: str

    def __post_init__(self):
        if type(self.supported) is not bool:
            raise ValueError("Invalid mode support")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("Invalid mode detail")

    def to_dict(self) -> dict:
        return {"supported": self.supported, "detail": self.detail}


@dataclass(frozen=True)
class CapsuleInspection:
    """Validated envelope facts plus explicit reproduction-mode support."""

    schema_version: int
    world_version: str
    source: str
    actor_id: str
    adapter_id: str
    adapter_version: str
    status: str
    tool_calls: int
    failed_invariants: tuple
    replay_kind: str
    truncated: bool
    external_dependencies: tuple
    faults_fired: tuple
    rule_versions: dict
    fixture_name: str | None
    evidence_inspection: ModeSupport
    recorded_replay: ModeSupport
    fresh_retest: ModeSupport

    def __post_init__(self):
        if self.schema_version not in (1, 2, 3):
            raise ValueError("Invalid inspected schema version")
        for value, label in ((self.world_version, "world version"),
                             (self.source, "source"), (self.actor_id, "actor id"),
                             (self.adapter_id, "adapter id"),
                             (self.adapter_version, "adapter version")):
            if type(value) is not str or not value:
                raise ValueError(f"Invalid inspected {label}")
        if self.status not in ("completed", "budget_exhausted"):
            raise ValueError("Invalid inspected run status")
        if type(self.tool_calls) is not int or not 0 <= self.tool_calls <= 1000:
            raise ValueError("Invalid inspected tool count")
        for names, label in ((self.failed_invariants, "failed invariants"),
                             (self.faults_fired, "faults fired")):
            if (not isinstance(names, (list, tuple))
                    or any(type(name) is not str for name in names)):
                raise ValueError(f"Invalid inspected {label}")
        if self.replay_kind not in (RECORDED, RETEST):
            raise ValueError("Invalid inspected replay kind")
        if type(self.truncated) is not bool:
            raise ValueError("Invalid inspected truncation flag")
        for dep in self.external_dependencies:
            if (not isinstance(dep, dict) or set(dep) != {"name", "version", "digest"}
                    or type(dep["name"]) is not str or not dep["name"]):
                raise ValueError("Invalid inspected external dependency")
        if (type(self.rule_versions) is not dict
                or any(type(name) is not str or type(version) is not str
                       for name, version in self.rule_versions.items())):
            raise ValueError("Invalid inspected rule versions")
        if self.fixture_name is not None and type(self.fixture_name) is not str:
            raise ValueError("Invalid inspected fixture name")
        for mode in (self.evidence_inspection, self.recorded_replay, self.fresh_retest):
            if not isinstance(mode, ModeSupport):
                raise ValueError("Invalid inspected mode support")
        object.__setattr__(self, "failed_invariants", tuple(self.failed_invariants))
        object.__setattr__(self, "external_dependencies", tuple(self.external_dependencies))
        object.__setattr__(self, "faults_fired", tuple(self.faults_fired))

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version, "world_version": self.world_version,
            "source": self.source, "actor_id": self.actor_id, "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version, "status": self.status,
            "tool_calls": self.tool_calls, "failed_invariants": list(self.failed_invariants),
            "replay_kind": self.replay_kind, "truncated": self.truncated,
            "external_dependencies": [dict(dep) for dep in self.external_dependencies],
            "faults_fired": list(self.faults_fired), "rule_versions": dict(self.rule_versions),
            "fixture_name": self.fixture_name,
            "evidence_inspection": self.evidence_inspection.to_dict(),
            "recorded_replay": self.recorded_replay.to_dict(),
            "fresh_retest": self.fresh_retest.to_dict(),
        }


def _checked_envelope(data: Any) -> int:
    """The schema version after the checksum passes. Checksum comes first."""
    if type(data) is not dict or set(data) != {"payload", "sha256"}:
        raise ValueError("Invalid capsule envelope")
    payload, sha = data["payload"], data["sha256"]
    if type(payload) is not dict or type(sha) is not str:
        raise ValueError("Invalid capsule envelope")
    if capsule_digest(payload) != sha:
        raise ValueError("Capsule checksum mismatch")
    version = payload.get("schema_version")
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError("Unsupported capsule schema")
    return version


def _failed(checks: Any) -> tuple:
    if not isinstance(checks, Mapping):
        raise ValueError("Invalid recorded checks")
    for name, passed in checks.items():
        if type(name) is not str or type(passed) is not bool:
            raise ValueError("Invalid recorded checks")
    return tuple(sorted(name for name, passed in checks.items() if passed is False))


def _modes(*, replay_kind: str, truncated: bool, external: tuple) -> dict:
    if replay_kind == RETEST:
        replay = ModeSupport(False, "retest capsules are new experiments; "
                                    "rerun the target instead of recorded replay")
    elif truncated:
        replay = ModeSupport(False, "truncated capsules carry incomplete evidence; "
                                    "a replay would compare against a cut-short trace")
    elif external:
        names = ", ".join(sorted(dep["name"] for dep in external))
        replay = ModeSupport(False, f"capsule needs resources outside itself: {names}")
    else:
        replay = ModeSupport(True, "replay recorded actions against a reset environment")
    return {
        "evidence_inspection": ModeSupport(True, "read-only; validates the envelope only"),
        "recorded_replay": replay,
        "fresh_retest": ModeSupport(True, "a new experiment under the same scenario; "
                                          "never a replay-equivalence claim"),
    }


def _inspect_v1(data: dict) -> CapsuleInspection:
    # v1 readers require exact field sets, so reuse the v1 reader verbatim.
    with tempfile.TemporaryDirectory(prefix="ghostlab-inspect-") as directory:
        path = Path(directory) / "capsule.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        payload = refund_capsule.load_capsule(path)
    run = payload["run"]
    return CapsuleInspection(
        schema_version=1, world_version=payload["world_version"], source=payload["source"],
        actor_id=payload["actor_id"], adapter_id=run["adapter"],
        adapter_version=run["adapter_version"], status=run["status"],
        tool_calls=len(run["trace"]), failed_invariants=_failed(run["checks"]),
        replay_kind=RECORDED, truncated=False, external_dependencies=(), faults_fired=(),
        rule_versions={}, fixture_name=None, **_modes(replay_kind=RECORDED,
                                                      truncated=False, external=()))


def _inspect_v2(data: dict) -> CapsuleInspection:
    capsule = CapsuleV2.model_validate(data)
    payload, run = capsule.payload, capsule.payload.run
    return CapsuleInspection(
        schema_version=2, world_version=payload.world_version, source=payload.source,
        actor_id=payload.actor_id, adapter_id=run.adapter_id,
        adapter_version=run.adapter_version, status=run.status,
        tool_calls=len(run.trace), failed_invariants=_failed(run.checks),
        replay_kind=RECORDED, truncated=False, external_dependencies=(), faults_fired=(),
        rule_versions={}, fixture_name=None, **_modes(replay_kind=RECORDED,
                                                      truncated=False, external=()))


def _inspect_v3(data: dict) -> CapsuleInspection:
    capsule = CapsuleV3.model_validate(data)
    payload, run = capsule.payload, capsule.payload.run
    provenance = payload.provenance
    replay_kind = provenance.replay_kind if provenance is not None else RECORDED
    truncated = provenance.truncated if provenance is not None else False
    external = (tuple({"name": dep.name, "version": dep.version, "digest": dep.digest}
                      for dep in provenance.external_dependencies)
                if provenance is not None else ())
    faults = tuple(provenance.faults_fired) if provenance is not None else ()
    rules = dict(provenance.rule_versions) if provenance is not None else {}
    fixture = provenance.fixture_name if provenance is not None else None
    return CapsuleInspection(
        schema_version=3, world_version=payload.world_version, source=payload.source,
        actor_id=payload.actor_id, adapter_id=run.adapter_id,
        adapter_version=run.adapter_version, status=run.status,
        tool_calls=len(run.trace), failed_invariants=_failed(run.checks),
        replay_kind=replay_kind, truncated=truncated, external_dependencies=external,
        faults_fired=faults, rule_versions=rules, fixture_name=fixture,
        **_modes(replay_kind=replay_kind, truncated=truncated, external=external))


def inspect_capsule(data: Any) -> CapsuleInspection:
    """Validate one parsed envelope and report its reproduction modes."""
    version = _checked_envelope(data)
    if version == 1:
        return _inspect_v1(data)
    if version == 2:
        return _inspect_v2(data)
    return _inspect_v3(data)


def inspect_capsule_file(path: str | Path) -> CapsuleInspection:
    """Validate one envelope file (size-capped) and report its modes."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("Capsule file does not exist")
    if path.stat().st_size > refund_capsule.MAX_CAPSULE_BYTES:
        raise ValueError("Capsule too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("Invalid capsule JSON") from error
    return inspect_capsule(data)
