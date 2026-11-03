"""P4 DEV defect suite tests: known defects detected per expectations.

Acceptance: every known dev defect is detected exactly as expected, with
survivors, false findings and unknown surprises reported (all empty here).
Standard library only (unittest style).
"""

import unittest

from ghostlab.verify import CATALOG, FAIL, assess, refund_completion
from ghostlab.verify.dev_defects import (DEV_CASES, WORLD_COMPLETION, WORLD_RULES,
                                         format_matrix, run_dev_suite)


class DevSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_dev_suite()

    def test_suite_has_sixteen_cases(self):
        self.assertEqual(len(DEV_CASES), 16)
        self.assertEqual(self.report.summary["total"], 16)

    def test_every_case_matches_its_expectations(self):
        mismatches = [r.case_id for r in self.report.results if not r.passed]
        self.assertEqual(mismatches, [])

    def test_no_survivors_no_false_findings_no_unknown_surprises(self):
        summary = self.report.summary
        self.assertEqual(summary["survivors"], ())
        self.assertEqual(summary["false_findings"], ())
        self.assertEqual(summary["unknown_surprises"], ())
        self.assertEqual(summary["passed"], summary["total"])

    def test_every_failure_cites_events_and_carries_values(self):
        for case in DEV_CASES:
            view = case.build()
            assessment = assess(view, WORLD_RULES[case.world])
            for verdict in assessment.verdicts:
                self.assertEqual(verdict.rule_version, CATALOG[verdict.rule].version)
                if verdict.outcome == FAIL:
                    self.assertTrue(verdict.evidence_event_ids,
                                    f"{case.case_id}/{verdict.rule} cites nothing")
                    self.assertIsNotNone(verdict.observed)
                    self.assertIsNotNone(verdict.limit)
                    self.assertIsNotNone(verdict.unit)

    def test_completion_is_reported_not_judged(self):
        by_id = {r.case_id: r for r in self.report.results}
        # DEV-R4a finished the refund task but failed safety.
        self.assertTrue(by_id["DEV-R4a"].completion["fully_refunded"])
        self.assertIn("ownership_refunds", by_id["DEV-R4a"].actual_failed)
        # DEV-J8 has no evidence: no completion, no safety claim either.
        self.assertIsNone(by_id["DEV-J8"].completion)
        self.assertEqual(by_id["DEV-J8"].actual_failed, frozenset())

    def test_matrix_reports_the_summary(self):
        matrix = format_matrix(self.report)
        self.assertIn("passed 16/16", matrix)
        self.assertIn("survivors=[]", matrix)
        self.assertIn("false_findings=[]", matrix)
        for case in DEV_CASES:
            self.assertIn(case.case_id, matrix)

    def test_completion_helpers_fail_closed(self):
        self.assertIsNone(refund_completion({}))
        self.assertIsNone(WORLD_COMPLETION["group-journey/1"]({}))


if __name__ == "__main__":
    unittest.main()
