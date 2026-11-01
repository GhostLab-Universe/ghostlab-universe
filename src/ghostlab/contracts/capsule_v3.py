"""Failure Capsule v3: v2 evidence plus optional external-environment provenance.

Contract set ``ghostlab-contracts/1.2`` is additive: v1 and v2 are unchanged
and keep working; refund v1 remains the authoritative format for refund-world/1.

A v3 payload is exactly the v2 payload plus one optional field,
provenance. None means plain v2 semantics: a self-contained recorded
capsule. A present block distinguishes recorded replay, fresh-agent retest,
external dependencies and truncation. See docs/capsule-v3.md.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .primitives import Sha256Hex, ShortText, StrictModel, _exact_type, require_unique
from .tools import ToolArg
from .world import AdapterVersion, CapsuleV2, WorldRunResult, WorldScenario
from .world_packs import PackVersion, WorldName, pack

CapsuleV3Version = Annotated[Literal[3], _exact_type(int)]
RegistryName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
"""P1 registry identifier shape: environment, adapter, fixture, dependency ids."""

ConfigValue = Annotated[str, StringConstraints(max_length=200)] | int | bool
"""Scalar world-config snapshot value (mirrors FaultParameter values)."""


def _require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


class CapsuleChecksumError(ValueError):
    """The capsule payload does not match its checksum; it is never converted."""


def capsule_digest(payload: dict) -> str:
    """The canonical digest every capsule version uses (same algorithm as v1 and v2)."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def fixture_digest(fixture: dict) -> str:
    """Canonical digest of a fixture snapshot for provenance.fixture_sha256."""
    return capsule_digest(dict(fixture))


class ExternalDependency(StrictModel):
    """One resource outside the capsule that the recorded run needed.

    Names plus pinned versions/digests only: no URIs, credentials or live
    handles. A capsule that names any is not reproducible from its own bytes.
    """

    name: RegistryName
    version: ShortText | None = None
    digest: Sha256Hex | None = None


class CapsuleProvenance(StrictModel):
    """Optional companion metadata for external environments.

    Environment, fixture and fault ids use the frozen P1 names; the contract
    checks their shape, while P5 replay resolves them against the adapter
    registry (fail closed on unknown ids). ``retest_adapter_*`` may name an
    adapter outside the world pack: a retest can come from an external agent.
    """

    replay_kind: Literal["recorded", "retest"] = "recorded"
    environment_id: RegistryName | None = None
    environment_version: AdapterVersion | None = None
    fixture_name: RegistryName | None = None
    fixture_sha256: Sha256Hex | None = None
    rule_versions: dict[WorldName, ShortText] = Field(default_factory=dict, max_length=16)
    faults_fired: list[WorldName] = Field(default_factory=list, max_length=8)
    config: dict[WorldName, ConfigValue] = Field(default_factory=dict, max_length=32)
    external_dependencies: list[ExternalDependency] = Field(default_factory=list,
                                                            max_length=16)
    truncated: bool = False
    retest_adapter_id: RegistryName | None = None
    retest_adapter_version: AdapterVersion | None = None

    @model_validator(mode="after")
    def _check(self):
        retest = self.replay_kind == "retest"
        _require((self.retest_adapter_id is None) is not retest,
                 "retest_adapter_id is required exactly for replay_kind='retest'")
        _require((self.retest_adapter_version is None) is not retest,
                 "retest_adapter_version is required exactly for replay_kind='retest'")
        require_unique([d.name for d in self.external_dependencies],
                       "external dependency names")
        return self


class CapsulePayloadV3(StrictModel):
    """v2 evidence plus optional external-environment provenance."""

    schema_version: CapsuleV3Version
    world_version: PackVersion
    fixture: dict[str, JsonValue]
    scenario: WorldScenario
    actor_id: ToolArg
    source: ShortText
    seed: int = Field(ge=0, le=2**53 - 1)
    recorded_model_outputs: list[JsonValue] = Field(max_length=1_000)
    run: WorldRunResult
    provenance: CapsuleProvenance | None = None

    @model_validator(mode="after")
    def _check(self):
        _require(self.scenario.world_version == self.world_version == self.run.world_version,
                 "scenario, run and capsule must name the same world")
        _require(self.run.actor_id == self.actor_id, "Recorded principal does not match capsule")
        _require(len(self.run.trace) <= self.scenario.max_tool_calls,
                 "Trace longer than the scenario's tool budget")
        # The v3 core is v2-shaped, so the frozen packs' v2 recorded-action
        # declaration governs it; implemented packs cannot declare 3 without a
        # new world version, and provenance-bearing capsules need P5 replay.
        _require(2 in pack(self.world_version).replay.capsule_schema_versions,
                 f"{self.world_version} does not replay recorded-action capsules")
        provenance = self.provenance
        if provenance is not None:
            if provenance.fixture_sha256 is not None:
                _require(fixture_digest(self.fixture) == provenance.fixture_sha256,
                         "provenance.fixture_sha256 does not match the capsule fixture")
            scheduled = {fault.fault_id for fault in self.scenario.faults}
            unknown = [f for f in provenance.faults_fired if f not in scheduled]
            _require(not unknown, f"faults_fired {unknown} were not scheduled by the scenario")
        return self


class CapsuleV3(StrictModel):
    """v3 envelope: same checksum rule as v2.

    The checksum detects accidental change; it does not establish authorship.
    """

    payload: CapsulePayloadV3
    sha256: Sha256Hex

    @model_validator(mode="after")
    def _check_checksum(self):
        payload = self.payload.model_dump(mode="json")
        _require(capsule_digest(payload) == self.sha256, "Capsule checksum mismatch")
        return self


def _verified_payload(capsule) -> dict:
    data = capsule.model_dump(mode="json")
    if capsule_digest(data["payload"]) != data["sha256"]:
        raise CapsuleChecksumError("Capsule checksum mismatch")
    return data["payload"]


def capsule_v2_to_v3(capsule: CapsuleV2 | dict) -> CapsuleV3:
    """Upgrade a checksum-verified v2 capsule; provenance stays absent (v2 semantics)."""
    payload = _verified_payload(CapsuleV2.model_validate(capsule))
    v3 = {**payload, "schema_version": 3, "provenance": None}
    return CapsuleV3.model_validate({"payload": v3, "sha256": capsule_digest(v3)})


def capsule_v3_to_v2(capsule: CapsuleV3 | dict) -> CapsuleV2:
    """Downgrade only a provenance-free v3 capsule.

    Anything recorded in provenance would be silently lost, so
    provenance-bearing capsules are refused with an explicit error.
    """
    payload = _verified_payload(CapsuleV3.model_validate(capsule))
    provenance = payload["provenance"]
    if provenance is not None:
        raise ValueError(
            "v3 provenance does not fit v2; refusing to drop it "
            f"(replay_kind={provenance['replay_kind']}, "
            f"external={len(provenance['external_dependencies'])}, "
            f"truncated={provenance['truncated']})")
    v2 = {key: value for key, value in payload.items() if key != "provenance"}
    v2["schema_version"] = 2
    return CapsuleV2.model_validate({"payload": v2, "sha256": capsule_digest(v2)})


__all__ = [
    "CapsuleChecksumError", "CapsulePayloadV3", "CapsuleProvenance", "CapsuleV3",
    "CapsuleV3Version", "ConfigValue", "ExternalDependency", "RegistryName",
    "capsule_digest", "capsule_v2_to_v3", "capsule_v3_to_v2", "fixture_digest",
]
