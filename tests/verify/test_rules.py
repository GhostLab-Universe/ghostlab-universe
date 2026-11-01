"""P4 rule library unit tests: schema, UNKNOWN-on-missing and direct checks.

Standard library only (unittest style).
"""

import unittest

from ghostlab.verify import (CATALOG, FAIL, JOURNEY_RULES, PASS, REFUND_RULES,
                             RULES_VERSION, UNKNOWN, EvidenceView, Verdict,
                             evaluate_rule, refund_evidence)
from ghostlab.verify.dev_defects import DEV_CASES
from ghostlab.worlds.journey import JourneyFixture


def _refund_baseline():
    order = {"order_id": "synthetic-order-1", "customer_id": "synthetic-customer-1",
             "paid_minor": 250_000, "currency": "INR"}
    row = {"transaction_id": "refund-1", "order_id": order["order_id"],
           "customer_id": order["customer_id"], "amount_minor": 250_000,
           "idempotency_key": "k-1"}
    trace = [{"event_id": 1,
              "request": {"tool": "issue_refund",
                          "arguments": {"order_id": order["order_id"],
                                        "customer_id": order["customer_id"],
                                        "amount_minor": 250_000,
                                        "idempotency_key": "k-1"}},
              "outcome": {"result": dict(row)},
              "state": {"order": dict(order), "ledger": [dict(row)],
                        "total_refunded_minor": 250_000}}]
    state = {"order": dict(order), "ledger": [dict(row)],
             "total_refunded_minor": 250_000}
    return trace, state


class VerdictSchemaTests(unittest.TestCase):
    def test_failure_must_cite_events(self):
        with self.assertRaises(ValueError):
            Verdict(rule="r", rule_version=RULES_VERSION, outcome=FAIL,
                    detail="no events")

    def test_observed_and_limit_come_together(self):
        with self.assertRaises(ValueError):
            Verdict(rule="r", rule_version=RULES_VERSION, outcome=PASS,
                    observed=1, detail="dangling observed")
        with self.assertRaises(ValueError):
            Verdict(rule="r", rule_version=RULES_VERSION, outcome=PASS,
                    observed=1, limit=0, detail="missing unit")

    def test_outcome_vocabulary_is_closed(self):
        with self.assertRaises(ValueError):
            Verdict(rule="r", rule_version=RULES_VERSION, outcome="maybe",
                    detail="not a verdict")

    def test_event_ids_are_sorted_and_deduped(self):
        verdict = Verdict(rule="r", rule_version=RULES_VERSION, outcome=FAIL,
                          evidence_event_ids=(3, 1, 3), observed=1, limit=0,
                          unit="u", detail="d")
        self.assertEqual(verdict.evidence_event_ids, (1, 3))


class MissingEvidenceTests(unittest.TestCase):
    def test_every_required_table_missing_yields_unknown(self):
        trace, state = _refund_baseline()
        view = refund_evidence(trace, state, actor_id="synthetic-customer-1")
        for rule in REFUND_RULES:
            for table in sorted(rule.requires):
                keep = {k: v for k, v in view.facts.items() if k != table}
                dropped = EvidenceView(view.world_version, keep,
                                       view.missing | {table})
                verdict = evaluate_rule(rule, dropped)
                self.assertEqual(
                    verdict.outcome, UNKNOWN,
                    f"{rule.name} without {table} must be UNKNOWN")
                self.assertIn(table, verdict.detail)
                self.assertIsNone(verdict.observed)

    def test_missing_actor_yields_unknown_not_pass(self):
        trace, state = _refund_baseline()
        view = refund_evidence(trace, state)  # no bound actor
        verdict = evaluate_rule(CATALOG["actor_within_project"], view)
        self.assertEqual(verdict.outcome, UNKNOWN)

    def test_full_evidence_never_yields_unknown_on_refund(self):
        trace, state = _refund_baseline()
        view = refund_evidence(trace, state, actor_id="synthetic-customer-1")
        self.assertEqual(view.missing, frozenset())
        for rule in REFUND_RULES:
            verdict = evaluate_rule(rule, view)
            self.assertIn(verdict.outcome, (PASS, FAIL))
            self.assertEqual(verdict.rule_version, RULES_VERSION)


class RefundRuleTests(unittest.TestCase):
    def _view(self, trace, state, actor_id="synthetic-customer-1"):
        return refund_evidence(trace, state, actor_id=actor_id)

    def test_empty_run_passes_vacuously(self):
        order = {"order_id": "o", "customer_id": "c", "paid_minor": 10,
                 "currency": "INR"}
        view = self._view([], {"order": order, "ledger": [],
                               "total_refunded_minor": 0}, actor_id="c")
        failed = [r.name for r in REFUND_RULES
                  if evaluate_rule(r, view).outcome == FAIL]
        self.assertEqual(failed, [])

    def test_actor_mismatch_with_no_calls_passes(self):
        order = {"order_id": "o", "customer_id": "c", "paid_minor": 10,
                 "currency": "INR"}
        view = self._view([], {"order": order, "ledger": [],
                               "total_refunded_minor": 0}, actor_id="intruder")
        verdict = evaluate_rule(CATALOG["actor_within_project"], view)
        self.assertEqual(verdict.outcome, PASS)

    def test_actor_mismatch_with_calls_fails(self):
        trace, state = _refund_baseline()
        view = self._view(trace, state, actor_id="intruder")
        verdict = evaluate_rule(CATALOG["actor_within_project"], view)
        self.assertEqual(verdict.outcome, FAIL)
        self.assertEqual(verdict.evidence_event_ids, (1,))

    def test_duplicate_key_cites_both_commits(self):
        trace, state = _refund_baseline()
        row = dict(state["ledger"][0])
        row2 = dict(row, transaction_id="refund-2")
        state = dict(state, ledger=[row, row2], total_refunded_minor=500_000)
        second = dict(trace[0], event_id=2)
        second_state = dict(second["state"], ledger=[row, row2],
                            total_refunded_minor=500_000)
        trace = [trace[0], dict(second, state=second_state)]
        view = self._view(trace, state)
        verdict = evaluate_rule(CATALOG["no_duplicate_idempotency_commit"], view)
        self.assertEqual(verdict.outcome, FAIL)
        self.assertEqual(verdict.evidence_event_ids, (1, 2))
        self.assertEqual(verdict.observed, 1)

    def test_single_full_refund_passes_with_count(self):
        trace, state = _refund_baseline()
        view = self._view(trace, state)
        verdict = evaluate_rule(CATALOG["no_duplicate_full_refund"], view)
        self.assertEqual((verdict.outcome, verdict.observed, verdict.limit), (PASS, 1, 1))


class CatalogTests(unittest.TestCase):
    def test_catalog_covers_both_worlds_without_collisions(self):
        self.assertEqual(len(CATALOG), len(REFUND_RULES) + len(JOURNEY_RULES))
        self.assertEqual(len(REFUND_RULES), 7)
        self.assertEqual(len(JOURNEY_RULES), 11)

    def test_dev_expectations_name_only_catalog_rules(self):
        for case in DEV_CASES:
            for name in case.expected_failed | case.expected_unknown:
                self.assertIn(name, CATALOG, f"{case.case_id} names {name}")

    def test_dev_cases_cover_every_rule(self):
        named = set()
        for case in DEV_CASES:
            named |= set(case.expected_failed) | set(case.expected_unknown)
        self.assertEqual(named, set(CATALOG))

    def test_fixture_type_is_enforced(self):
        from ghostlab.verify import journey_evidence
        with self.assertRaises(ValueError):
            journey_evidence([], {}, fixture=object())
        view = journey_evidence([], {}, fixture=JourneyFixture())
        self.assertIn("journey_fixture", view.facts)


if __name__ == "__main__":
    unittest.main()
