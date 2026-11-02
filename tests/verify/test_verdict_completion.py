"""Verdict completion tests: temporal stamping and the not-applicable outcome.

Standard library only (unittest style).
"""

import unittest

from ghostlab.verify import (CATALOG, FAIL, NOT_APPLICABLE, PASS, REFUND_RULES,
                             RULES_VERSION, TEMPORAL_RULES, TEMPORAL_RULES_VERSION,
                             UNKNOWN, Assessment, Rule, Verdict, assess, evaluate_rule,
                             refund_evidence, verdict_summary)
from ghostlab.verify.dev_defects import DEV_CASES, WORLD_RULES

_CASES = {case.case_id: case for case in DEV_CASES}


def _declining_rule():
    def check(facts):
        del facts
        return (NOT_APPLICABLE, (), None, None, None,
                "refunds say nothing about seat maps")
    return Rule("seat_map_only", frozenset({"ledger"}), check, "Declines.")


def _refund_view():
    return _CASES["DEV-R3"].build()


class TemporalStampingTests(unittest.TestCase):
    def test_temporal_rules_carry_the_temporal_version(self):
        for rule in TEMPORAL_RULES:
            self.assertEqual(rule.version, TEMPORAL_RULES_VERSION)

    def test_core_rules_carry_the_core_version(self):
        for rule in REFUND_RULES:
            self.assertEqual(rule.version, RULES_VERSION)

    def test_rule_version_defaults_to_core(self):
        rule = Rule("r", frozenset({"ledger"}), lambda facts: (PASS, (), 0, 0, "u", "d"))
        self.assertEqual(rule.version, RULES_VERSION)
        with self.assertRaises(ValueError):
            Rule("r", frozenset({"ledger"}), lambda facts: (PASS, (), 0, 0, "u", "d"),
                 version="")

    def test_temporal_verdicts_stamped_on_pass_fail_and_unknown(self):
        clean = _CASES["DEV-J3"].build()
        verdict = evaluate_rule(CATALOG["after_takeover_no_commitment"], clean)
        self.assertEqual((verdict.outcome, verdict.rule_version),
                         (PASS, TEMPORAL_RULES_VERSION))
        broken = _CASES["DEV-J4"].build()
        verdict = evaluate_rule(CATALOG["after_takeover_no_commitment"], broken)
        self.assertEqual((verdict.outcome, verdict.rule_version),
                         (FAIL, TEMPORAL_RULES_VERSION))
        empty = _CASES["DEV-J8"].build()
        verdict = evaluate_rule(CATALOG["after_takeover_no_commitment"], empty)
        self.assertEqual((verdict.outcome, verdict.rule_version),
                         (UNKNOWN, TEMPORAL_RULES_VERSION))

    def test_rule_error_unknown_keeps_the_rule_family_stamp(self):
        def broken(facts):
            del facts
            raise KeyError("boom")
        rule = Rule("temporal_broken", frozenset({"ledger"}), broken,
                    version=TEMPORAL_RULES_VERSION)
        assessment = assess(_refund_view(), [rule])
        self.assertEqual(assessment.unknown_rules, ("temporal_broken",))
        self.assertEqual(assessment.verdicts[0].rule_version, TEMPORAL_RULES_VERSION)

    def test_assessment_mixes_family_stamps_per_verdict(self):
        view = _CASES["DEV-J4"].build()
        assessment = assess(view, [CATALOG["commitments_use_approved_intent"],
                                   CATALOG["after_takeover_no_commitment"]])
        stamped = {v.rule: v.rule_version for v in assessment.verdicts}
        self.assertEqual(stamped, {"commitments_use_approved_intent": RULES_VERSION,
                                  "after_takeover_no_commitment": TEMPORAL_RULES_VERSION})


class NotApplicableTests(unittest.TestCase):
    def test_vocabulary_accepts_not_applicable_only(self):
        verdict = Verdict(rule="r", rule_version=RULES_VERSION,
                          outcome=NOT_APPLICABLE, detail="outside scope")
        self.assertEqual(verdict.outcome, "not-applicable")
        with self.assertRaises(ValueError):
            Verdict(rule="r", rule_version=RULES_VERSION, outcome="n/a",
                    detail="not a verdict")

    def test_declining_rule_reports_without_moving_safety(self):
        assessment = assess(_refund_view(), [*REFUND_RULES, _declining_rule()])
        self.assertEqual(assessment.not_applicable_rules, ("seat_map_only",))
        self.assertEqual(assessment.failed_rules, ())
        self.assertEqual(assessment.unknown_rules, ())
        self.assertTrue(assessment.safety_held)
        self.assertEqual(verdict_summary(assessment)["seat_map_only"], NOT_APPLICABLE)

    def test_declined_rule_never_counts_as_passed(self):
        assessment = assess(_refund_view(), [_declining_rule()])
        passed = [v.rule for v in assessment.verdicts if v.outcome == PASS]
        self.assertEqual(passed, [])
        self.assertEqual(assessment.not_applicable_rules, ("seat_map_only",))

    def test_missing_facts_stay_unknown_never_not_applicable(self):
        view = _refund_view()
        keep = {k: v for k, v in view.facts.items() if k != "ledger"}
        from ghostlab.verify import EvidenceView
        dropped = EvidenceView(view.world_version, keep, view.missing | {"ledger"})
        verdict = evaluate_rule(_declining_rule(), dropped)
        self.assertEqual(verdict.outcome, UNKNOWN)

    def test_assessment_summary_must_agree_on_declined_rules(self):
        assessment = assess(_refund_view(), [_declining_rule()])
        with self.assertRaises(ValueError):
            Assessment(world_version=assessment.world_version,
                       rules_version=assessment.rules_version,
                       verdicts=assessment.verdicts, completion=None,
                       safety_held=True, failed_rules=(), unknown_rules=(),
                       not_applicable_rules=())  # drops the declined rule

    def test_assessment_defaults_to_no_declined_rules(self):
        view = _refund_view()
        assessment = assess(view, REFUND_RULES)
        rebuilt = Assessment(world_version=assessment.world_version,
                             rules_version=assessment.rules_version,
                             verdicts=assessment.verdicts, completion=None,
                             safety_held=assessment.safety_held,
                             failed_rules=assessment.failed_rules,
                             unknown_rules=assessment.unknown_rules)
        self.assertEqual(rebuilt.not_applicable_rules, ())

    def test_no_existing_check_returns_not_applicable(self):
        # Old verdicts keep their meaning: no reviewed check declines.
        for case in DEV_CASES:
            assessment = assess(case.build(), WORLD_RULES[case.world])
            self.assertEqual(assessment.not_applicable_rules, (), case.case_id)


if __name__ == "__main__":
    unittest.main()
