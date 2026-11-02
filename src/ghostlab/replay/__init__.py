"""Reproduction for extension environments: inspect, replay, retest, enforce, minimize.

Read-only evidence inspection, recorded-action replay, fresh-agent retest,
enforced-execution assessment, fault-subset minimization with
failure-witness retention, and measured fidelity reports. Generic over
``ghostlab-extensions/1``: no world logic, no model calls, no network.
Pydantic lives only at the capsule-envelope boundary
(:mod:`ghostlab.replay.inspect`, :mod:`ghostlab.replay.export_v3`).
"""

from ghostlab.replay.enforced import (EnforcementResult, assess_enforcement,
                                      refuse_unless_enforced)
from ghostlab.replay.export_v3 import export_refund_run_v3, export_v3, write_capsule_file
from ghostlab.replay.fidelity import FidelityReport, assess_fidelity
from ghostlab.replay.inspect import (CapsuleInspection, ModeSupport, inspect_capsule,
                                     inspect_capsule_file)
from ghostlab.replay.minimize_ext import (MinimizeReport, RunEvidence, classify_evidence,
                                          minimize_faults)
from ghostlab.diagnostics.minimize import SearchLimit

from ghostlab.replay.recorded import (ReplayResult, RetestResult, allow_tools, canonical,
                                      check_verdicts, failure_witness, normalize_check,
                                      refuse_unless_recorded, replay_recorded, retest_fresh,
                                      witness_sha256)

__all__ = [
    "CapsuleInspection", "EnforcementResult", "FidelityReport", "MinimizeReport",
    "ModeSupport", "ReplayResult", "RetestResult", "RunEvidence", "allow_tools",
    "assess_enforcement", "assess_fidelity", "canonical", "check_verdicts",
    "classify_evidence", "export_refund_run_v3", "export_v3", "failure_witness",
    "inspect_capsule", "inspect_capsule_file", "minimize_faults", "normalize_check",
    "refuse_unless_enforced", "refuse_unless_recorded", "replay_recorded",
    "retest_fresh", "SearchLimit",
    "witness_sha256", "write_capsule_file",
]
