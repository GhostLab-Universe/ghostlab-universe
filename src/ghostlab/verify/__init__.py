"""Deterministic verifier (P4): reusable rules over recorded evidence.

Verdicts come from explicit rules plus authoritative trace/state evidence;
target or model claims are never inputs. Missing evidence yields UNKNOWN,
never pass. Completion is reported alongside safety, never folded into it.
Standard library only: no inference, no network, no services.
"""

from .assess import (Assessment, assess, journey_completion, refund_completion,
                     verdict_summary)
from .evidence import (JOURNEY_WORLD, REFUND_WORLD, EvidenceView, journey_evidence,
                       refund_evidence)
from .heldout import (HELDOUT_RESULT_VERSION, HELDOUT_RUNNER_VERSION, FreezeRecord,
                      HeldoutContractError, HeldoutRunner, NotCleanError,
                      evaluate_heldout, freeze, validate_result)
from .rules import (FAIL, PASS, REFUND_RULES, RULES_VERSION, UNKNOWN, JOURNEY_CORE_RULES,
                    Rule, Verdict, evaluate_rule)
from .temporal import TEMPORAL_RULES, TEMPORAL_RULES_VERSION, approval_takeovers, value_at

JOURNEY_RULES = JOURNEY_CORE_RULES + TEMPORAL_RULES

CATALOG = {rule.name: rule for rule in REFUND_RULES + JOURNEY_RULES}
assert len(CATALOG) == len(REFUND_RULES) + len(JOURNEY_RULES), "duplicate rule name"

__all__ = [
    "Assessment", "CATALOG", "FAIL", "HELDOUT_RESULT_VERSION", "HELDOUT_RUNNER_VERSION",
    "JOURNEY_CORE_RULES", "JOURNEY_RULES", "JOURNEY_WORLD", "PASS", "REFUND_RULES",
    "REFUND_WORLD", "RULES_VERSION", "TEMPORAL_RULES", "TEMPORAL_RULES_VERSION",
    "UNKNOWN", "EvidenceView", "FreezeRecord", "HeldoutContractError", "HeldoutRunner",
    "NotCleanError", "Rule", "Verdict", "approval_takeovers", "assess", "evaluate_heldout",
    "evaluate_rule", "freeze", "journey_completion", "journey_evidence",
    "refund_completion", "refund_evidence", "validate_result", "value_at",
    "verdict_summary",
]
