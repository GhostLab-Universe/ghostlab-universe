"""Enforced-execution outcomes for extension environments.

Generic over ``ghostlab-extensions/1``: no world logic, no model calls, no
network. :func:`assess_enforcement` reads one enforced execution record --
its mode/guarded flag plus its invariant checks -- and reports whether the
guard held. It never re-executes anything: enforcement is assessed from the
recorded evidence, like inspection, not by running the world again.

A lab record is refused, loudly: a run recorded with enforcement off
carries findings, not an enforcement outcome, and must never render as one.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ghostlab.replay.recorded import (check_verdicts, failure_witness,
                                      witness_sha256)

ENFORCEMENT_SCHEMA = "ghostlab-enforced-execution/1"
ENFORCED = "enforced"


def refuse_unless_enforced(*, mode: Any, guarded: Any) -> None:
    """Refuse enforcement outcomes for anything but an enforced record."""
    if type(mode) is not str or not mode:
        raise ValueError("Invalid execution mode")
    if type(guarded) is not bool:
        raise ValueError("Invalid guarded flag")
    if mode != ENFORCED or guarded is not True:
        raise ValueError("lab records carry no enforcement outcome; "
                         "enforcement was off for this run")


@dataclass(frozen=True)
class EnforcementResult:
    """Whether the guard held on one enforced execution record.

    ``held`` is true exactly when every recorded check passed: nothing
    got through the enforced boundary. A violated outcome names the
    failed checks and carries the same failure witness (and digest) as
    its replay/retest siblings, so the three outcomes stay comparable.
    """

    schema: str
    world_version: str
    mode: str
    guarded: bool
    held: bool
    checks: dict
    failed: tuple
    witness: dict
    witness_sha256: str
    model_calls: int = 0

    def __post_init__(self):
        if self.schema != ENFORCEMENT_SCHEMA:
            raise ValueError("Invalid enforcement schema")
        if type(self.world_version) is not str or not self.world_version:
            raise ValueError("Invalid enforcement world version")
        if self.mode != ENFORCED:
            raise ValueError("Invalid enforcement mode")
        if self.guarded is not True:
            raise ValueError("Invalid enforcement guarded flag")
        if type(self.held) is not bool:
            raise ValueError("Invalid enforcement verdict")
        if (type(self.checks) is not dict
                or any(type(name) is not str or type(hit) is not bool
                       for name, hit in self.checks.items())):
            raise ValueError("Invalid enforcement checks")
        if self.held != all(self.checks.values()):
            raise ValueError("Enforcement verdict does not match its checks")
        object.__setattr__(self, "failed", tuple(self.failed))
        if (any(type(name) is not str for name in self.failed)
                or set(self.failed) != {name for name, hit in self.checks.items()
                                        if hit is False}):
            raise ValueError("Invalid enforcement failures")
        if type(self.witness) is not dict or set(self.witness) != {"failed"}:
            raise ValueError("Invalid enforcement witness")
        if (type(self.witness_sha256) is not str
                or witness_sha256(self.witness) != self.witness_sha256):
            raise ValueError("Invalid enforcement witness digest")
        if self.model_calls != 0:
            raise ValueError("Enforcement assessment makes no model call")

    def to_dict(self) -> dict:
        data = {"schema": self.schema, "world_version": self.world_version,
                "mode": self.mode, "guarded": self.guarded, "held": self.held,
                "checks": dict(self.checks), "failed": list(self.failed),
                "witness": self.witness, "witness_sha256": self.witness_sha256,
                "model_calls": self.model_calls}
        return deepcopy(data)


def assess_enforcement(*, world_version: str, mode: str, guarded: bool,
                       checks: Any) -> EnforcementResult:
    """Assess one enforced execution record from its mode flag and checks.

    The mode comes first: lab records are refused before their checks are
    even read. Verdicts and the failure witness are then validated and
    digested exactly like the replay/retest siblings.
    """
    if type(world_version) is not str or not world_version:
        raise ValueError("Invalid world version")
    refuse_unless_enforced(mode=mode, guarded=guarded)
    verdicts = check_verdicts(checks)
    witness = failure_witness(checks)
    return EnforcementResult(
        schema=ENFORCEMENT_SCHEMA, world_version=world_version, mode=mode,
        guarded=guarded, held=all(verdicts.values()), checks=verdicts,
        failed=tuple(sorted(name for name, hit in verdicts.items() if hit is False)),
        witness=witness, witness_sha256=witness_sha256(witness))
