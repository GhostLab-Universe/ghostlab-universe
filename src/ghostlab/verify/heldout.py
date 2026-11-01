"""Held-out evaluation boundary (P4): consume a published runner, never scenarios.

The other session owns the held-out scenarios. This module defines ONLY the
runner protocol and the result contract it publishes to, plus the
freeze-before-run protocol: the checkout must be committed and clean before
any held-out run, and the freeze record (commit + rules version) travels with
the request so results are attributable. When no runner is published,
evaluation is PENDING -- dev results are never substituted. Standard library
only; local git subprocess calls, no network.
"""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .rules import RULES_VERSION

HELDOUT_RUNNER_VERSION = "ghostlab-heldout-runner/1"
HELDOUT_RESULT_VERSION = "ghostlab-heldout-result/1"

_RESULT_KEYS = frozenset({"contract_version", "runner_version", "freeze_commit",
                          "scenarios"})
_SCENARIO_KEYS = frozenset({"scenario_id", "detected", "rules_fired"})
_GIT_PREFIX = ["git", "-c", "safe.directory=*"]


class NotCleanError(RuntimeError):
    """The checkout is not frozen: commit first, then evaluate held-out."""


class HeldoutContractError(ValueError):
    """A held-out result broke the published contract (or leaked content)."""


@dataclass(frozen=True)
class FreezeRecord:
    """A committed, clean checkout pinned for one held-out evaluation."""

    checkout: str
    commit: str
    rules_version: str

    def __post_init__(self):
        for value, label in ((self.checkout, "checkout"), (self.commit, "commit"),
                             (self.rules_version, "rules version")):
            if type(value) is not str or not value:
                raise ValueError(f"Invalid freeze {label}")

    def as_dict(self):
        return {"checkout": self.checkout, "commit": self.commit,
                "rules_version": self.rules_version}


@runtime_checkable
class HeldoutRunner(Protocol):
    """What the held-out owner publishes: run a frozen checkout, return results.

    Implementations live with the held-out owner and are consumed read-only
    here. ``run`` receives the freeze record as plain data and returns the
    result contract: opaque scenario ids plus detection booleans only -- never
    scenario content, traces or fixtures.
    """

    def run(self, frozen: Mapping[str, Any]) -> Mapping[str, Any]:
        """Evaluate the frozen checkout against hidden scenarios."""
        ...


def freeze(checkout="."):
    """Pin the checkout for held-out evaluation: committed and clean, or refuse."""
    if type(checkout) is not str or not checkout:
        raise ValueError("Invalid checkout")
    try:
        commit = subprocess.run(_GIT_PREFIX + ["rev-parse", "HEAD"], cwd=checkout,
                                capture_output=True, text=True, check=True,
                                timeout=30).stdout.strip()
        status = subprocess.run(_GIT_PREFIX + ["status", "--porcelain=v1"], cwd=checkout,
                                capture_output=True, text=True, check=True,
                                timeout=30).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise NotCleanError(f"cannot freeze {checkout!r}: {error}") from None
    if not commit:
        raise NotCleanError(f"cannot freeze {checkout!r}: no commit")
    if status:
        raise NotCleanError(f"cannot freeze {checkout!r}: uncommitted changes; "
                            "commit first")
    return FreezeRecord(checkout=checkout, commit=commit, rules_version=RULES_VERSION)


def _known_rules():
    from . import CATALOG  # local import: the package assembles the catalog
    return set(CATALOG)


def validate_result(result):
    """Validate a published held-out result against the contract.

    Accepts ONLY opaque scenario ids with detection booleans and known rule
    names. Any extra key -- scenario content of any kind -- is rejected, so a
    leaky runner fails loudly instead of widening this side's view.
    """
    if not isinstance(result, Mapping) or set(result) != _RESULT_KEYS:
        raise HeldoutContractError("result keys must be exactly "
                                   f"{sorted(_RESULT_KEYS)}")
    if result["contract_version"] != HELDOUT_RESULT_VERSION:
        raise HeldoutContractError("wrong held-out result contract version")
    if result["runner_version"] != HELDOUT_RUNNER_VERSION:
        raise HeldoutContractError("wrong held-out runner version")
    if type(result["freeze_commit"]) is not str or not result["freeze_commit"]:
        raise HeldoutContractError("invalid freeze commit")
    scenarios = result["scenarios"]
    if type(scenarios) is not list or len(scenarios) > 1000:
        raise HeldoutContractError("invalid scenario list")
    known = _known_rules()
    checked = []
    for entry in scenarios:
        if not isinstance(entry, Mapping) or set(entry) != _SCENARIO_KEYS:
            raise HeldoutContractError("scenario entries carry ONLY scenario_id, "
                                       "detected and rules_fired")
        scenario_id, detected, fired = (entry["scenario_id"], entry["detected"],
                                        entry["rules_fired"])
        if type(scenario_id) is not str or not scenario_id or len(scenario_id) > 200:
            raise HeldoutContractError("invalid scenario id")
        if type(detected) is not bool:
            raise HeldoutContractError("detected must be boolean")
        if (type(fired) is not list or any(type(r) is not str for r in fired)
                or any(r not in known for r in fired)):
            raise HeldoutContractError("rules_fired must list known rule names")
        checked.append({"scenario_id": scenario_id, "detected": detected,
                        "rules_fired": tuple(fired)})
    ids = [entry["scenario_id"] for entry in checked]
    if len(set(ids)) != len(ids):
        raise HeldoutContractError("duplicate scenario id")
    return {"contract_version": HELDOUT_RESULT_VERSION,
            "runner_version": HELDOUT_RUNNER_VERSION,
            "freeze_commit": result["freeze_commit"],
            "scenarios": tuple(checked),
            "summary": {"evaluated": len(checked),
                        "detected": sum(1 for e in checked if e["detected"])}}


def evaluate_heldout(*, runner=None, checkout="."):
    """Evaluate held-out scenarios, or report PENDING without substitution.

    ``runner`` is the other session's published runner (or None when nothing
    was published). A PENDING report carries no detection numbers at all, so
    dev results can never be mistaken for held-out evidence.
    """
    if runner is None:
        return {"status": "PENDING",
                "reason": "no held-out runner was published to this session",
                "rules_version": RULES_VERSION,
                "dev_results_substituted": False,
                "scenarios_evaluated": 0}
    if not isinstance(runner, HeldoutRunner):
        raise TypeError("held-out runner must satisfy the HeldoutRunner protocol")
    frozen = freeze(checkout)
    result = validate_result(runner.run(frozen.as_dict()))
    return {"status": "COMPLETE", "rules_version": RULES_VERSION,
            "freeze": frozen.as_dict(), "result": result}


__all__ = ["HELDOUT_RESULT_VERSION", "HELDOUT_RUNNER_VERSION", "FreezeRecord",
           "HeldoutContractError", "HeldoutRunner", "NotCleanError",
           "evaluate_heldout", "freeze", "validate_result"]
