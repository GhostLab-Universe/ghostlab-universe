"""Fault-subset minimization for extension environments.

Delta-debugging over subsets of the recorded fault schedule with the same
promise as :mod:`ghostlab.diagnostics.minimize`: the minimized run must
reproduce the SAME meaningful failure witness (failed invariants plus their
cited evidence), never just the same invariant name. The world binding
supplies ``run``; the core owns only ordering, budgets and the verdict.
"""

import itertools
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ghostlab.diagnostics.minimize import Budget, SearchLimit

from .recorded import (canonical, check_verdicts, failure_witness, normalize_check,
                       witness_sha256)

EXT_MINIMIZE_SCHEMA = "ghostlab-minimization-ext/1"
EXT_REGRESSION_SCHEMA = "ghostlab-diagnostic-regression-ext/1"


@dataclass(frozen=True)
class RunEvidence:
    """What one fault-subset trial produced: status plus verdicts."""

    status: str
    checks: tuple | list
    tool_calls: int = 0

    def __post_init__(self):
        if self.status not in ("completed", "budget_exhausted"):
            raise ValueError("Invalid evidence status")
        if not isinstance(self.checks, (list, tuple)) or not self.checks:
            raise ValueError("Invalid evidence checks")
        for check in self.checks:
            normalize_check(check)
        object.__setattr__(self, "checks", tuple(self.checks))
        if type(self.tool_calls) is not int or self.tool_calls < 0:
            raise ValueError("Invalid evidence tool count")


@dataclass(frozen=True)
class MinimizeReport:
    """The minimization outcome: smallest reproducing subset plus trials."""

    schema: str
    invariant: str
    failure_witness: dict
    failure_witness_sha256: str
    original_faults: tuple
    selected_faults: tuple | None
    removed_faults: tuple
    trials: tuple
    runs_used: int
    max_runs: int
    deadline_ms: int
    cardinality_minimal: bool
    status: str
    regression: dict
    model_calls: int = 0

    def __post_init__(self):
        if self.schema != EXT_MINIMIZE_SCHEMA:
            raise ValueError("Invalid minimization schema")
        if type(self.invariant) is not str or not self.invariant:
            raise ValueError("Invalid minimization invariant")
        if (type(self.failure_witness) is not dict
                or set(self.failure_witness) != {"failed"}):
            raise ValueError("Invalid minimization witness")
        if (type(self.failure_witness_sha256) is not str
                or witness_sha256(self.failure_witness) != self.failure_witness_sha256):
            raise ValueError("Invalid minimization witness digest")
        for faults, label in ((self.original_faults, "original"),
                              (self.selected_faults or (), "selected"),
                              (self.removed_faults, "removed")):
            if (not isinstance(faults, (list, tuple))
                    or any(type(name) is not str or not name for name in faults)):
                raise ValueError(f"Invalid {label} faults")
        if self.selected_faults is not None and (
                set(self.selected_faults) - set(self.original_faults)
                or set(self.removed_faults) != set(self.original_faults)
                - set(self.selected_faults)):
            raise ValueError("Selected faults are not a subset of the original schedule")
        for trial in self.trials:
            if (type(trial) is not dict
                    or set(trial) != {"faults", "result", "run_status", "tool_calls"}
                    or trial["result"] not in ("reproduced", "inconclusive",
                                               "not_reproduced")
                    or type(trial["tool_calls"]) is not int):
                raise ValueError("Invalid minimization trial")
        if (type(self.runs_used) is not int or self.runs_used < 0
                or type(self.max_runs) is not int or self.max_runs < 1
                or type(self.deadline_ms) is not int or self.deadline_ms < 1):
            raise ValueError("Invalid minimization budget accounting")
        if type(self.cardinality_minimal) is not bool:
            raise ValueError("Invalid minimization minimality flag")
        if self.status not in ("reduced", "already_minimal", "baseline_failure",
                               "smallest_found", "run_budget_exhausted",
                               "deadline_exceeded"):
            raise ValueError("Invalid minimization status")
        if type(self.regression) is not dict:
            raise ValueError("Invalid minimization regression")
        if self.model_calls != 0:
            raise ValueError("Minimization makes no model call")
        object.__setattr__(self, "original_faults", tuple(self.original_faults))
        object.__setattr__(self, "removed_faults", tuple(self.removed_faults))
        object.__setattr__(self, "trials", tuple(self.trials))
        if self.selected_faults is not None:
            object.__setattr__(self, "selected_faults", tuple(self.selected_faults))

    def to_dict(self) -> dict:
        data = {
            "schema": self.schema, "invariant": self.invariant,
            "failure_witness": self.failure_witness,
            "failure_witness_sha256": self.failure_witness_sha256,
            "original_faults": list(self.original_faults),
            "selected_faults": (None if self.selected_faults is None
                                else list(self.selected_faults)),
            "removed_faults": list(self.removed_faults),
            "trials": [dict(trial) for trial in self.trials], "runs_used": self.runs_used,
            "max_runs": self.max_runs, "deadline_ms": self.deadline_ms,
            "cardinality_minimal": self.cardinality_minimal, "status": self.status,
            "regression": self.regression, "model_calls": self.model_calls,
        }
        return deepcopy(data)


def classify_evidence(evidence: RunEvidence, invariant: str,
                      expected_witness: dict) -> str:
    """Reproduced only for the same failed invariant AND the same witness.

    Mirrors diagnostics minimization: an exhausted trial that still shows
    the same failure counts as reproduced; only non-matching exhausted
    trials are inconclusive.
    """
    if not isinstance(evidence, RunEvidence):
        raise TypeError("classify_evidence evidence must be RunEvidence")
    if type(invariant) is not str or not invariant:
        raise ValueError("Invalid invariant")
    verdicts = check_verdicts(evidence.checks)
    if invariant not in verdicts:
        raise ValueError(f"Unknown invariant {invariant!r}")
    reproduces = (verdicts[invariant] is False
                  and canonical(failure_witness(evidence.checks))
                  == canonical(expected_witness))
    if reproduces:
        return "reproduced"
    return "inconclusive" if evidence.status != "completed" else "not_reproduced"


def minimize_faults(*, run: Callable[[tuple], RunEvidence], faults: Any, invariant: str,
                    max_runs: int = 16, deadline_ms: int = 5000,
                    clock: Callable[[], float] = time.monotonic) -> MinimizeReport:
    """Smallest fault subset reproducing the baseline failure witness.

    Minimality means smallest fault *count* among subsets of this exact
    schedule. It says nothing about other fixtures, timings, adapters or
    production systems. Exhausted negative trials cannot establish absence.
    If the budget dies before the baseline completes, SearchLimit propagates
    (no witness exists yet, so no report can be built); mid-search exhaustion
    returns a report instead.
    """
    if not callable(run):
        raise TypeError("minimize_faults run must be callable")
    if (not isinstance(faults, (list, tuple)) or not 1 <= len(faults) <= 8
            or any(type(name) is not str or not name for name in faults)
            or len(set(faults)) != len(faults)):
        raise ValueError("faults holds 1 to 8 unique fault ids")
    if type(invariant) is not str or not invariant:
        raise ValueError("Invalid invariant")
    faults = tuple(faults)
    budget = Budget(max_runs, deadline_ms, clock)
    trials: list = []
    unknown_sizes: list = []
    baseline = budget.call(lambda: run(faults))
    if not isinstance(baseline, RunEvidence):
        raise TypeError("run must return RunEvidence")
    witness = failure_witness(baseline.checks)
    if classify_evidence(baseline, invariant, witness) != "reproduced":
        raise ValueError("selected failure cannot be reproduced")
    best, smaller = list(faults), False
    try:
        found = False
        for size in range(len(faults)):
            for subset in itertools.combinations(faults, size):
                trial = budget.call(lambda subset=subset: run(tuple(subset)))
                if not isinstance(trial, RunEvidence):
                    raise TypeError("run must return RunEvidence")
                result = classify_evidence(trial, invariant, witness)
                trials.append({"faults": list(subset), "result": result,
                               "run_status": trial.status, "tool_calls": trial.tool_calls})
                if result == "inconclusive":
                    unknown_sizes.append(size)
                if result == "reproduced":
                    best, found, smaller = list(subset), True, True
                    break
            if found:
                break
    except SearchLimit as limit:
        return MinimizeReport(
            schema=EXT_MINIMIZE_SCHEMA, invariant=invariant, failure_witness=witness,
            failure_witness_sha256=witness_sha256(witness), original_faults=faults,
            selected_faults=None, removed_faults=(), trials=tuple(trials),
            runs_used=budget.used, max_runs=max_runs, deadline_ms=deadline_ms,
            cardinality_minimal=False, status=str(limit),
            regression={"schema": EXT_REGRESSION_SCHEMA, "invariant": invariant,
                        "failure_witness": witness,
                        "failure_witness_sha256": witness_sha256(witness),
                        "faults": None, "note": "search did not finish"})
    inconclusive = any(size < len(best) for size in unknown_sizes)
    selected = tuple(best)
    return MinimizeReport(
        schema=EXT_MINIMIZE_SCHEMA, invariant=invariant, failure_witness=witness,
        failure_witness_sha256=witness_sha256(witness), original_faults=faults,
        selected_faults=selected,
        removed_faults=tuple(name for name in faults if name not in selected),
        trials=tuple(trials), runs_used=budget.used, max_runs=max_runs,
        deadline_ms=deadline_ms, cardinality_minimal=not inconclusive,
        status=("smallest_found" if inconclusive else "baseline_failure" if not selected
                else "reduced" if smaller else "already_minimal"),
        regression={"schema": EXT_REGRESSION_SCHEMA, "invariant": invariant,
                    "failure_witness": witness,
                    "failure_witness_sha256": witness_sha256(witness),
                    "faults": list(selected)})
