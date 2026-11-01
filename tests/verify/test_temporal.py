"""P4 temporal rule tests: occurrence-time verdicts that cleanup cannot erase.

Standard library only (unittest style).
"""

import unittest

from ghostlab.verify import (CATALOG, FAIL, PASS, UNKNOWN, evaluate_rule,
                             journey_evidence, value_at)
from ghostlab.verify.dev_defects import DEV_CASES
from ghostlab.worlds.journey import (JourneyFixture, JourneyScenario, JourneyWorld,
                                     scenario_json)

_CASES = {case.case_id: case for case in DEV_CASES}


def _live_journey(calls, *, faults=()):
    fixture = JourneyFixture()
    scenario = JourneyScenario.from_json(scenario_json(faults))
    world = JourneyWorld(fixture, scenario)
    for tool, arguments in calls:
        world.call(tool, dict(arguments))
    return journey_evidence(list(world.trace), world.snapshot(), fixture=fixture)


class ValueAtTests(unittest.TestCase):
    def test_latest_row_in_force_wins(self):
        timeline = [{"from_event": 1, "version": 2},
                    {"from_event": 4, "version": 3}]
        self.assertEqual(value_at(timeline, 1)["version"], 2)
        self.assertEqual(value_at(timeline, 4)["version"], 3)
        self.assertEqual(value_at(timeline, 999)["version"], 3)

    def test_nothing_in_force_is_none(self):
        self.assertIsNone(value_at([{"from_event": 5, "version": 3}], 2))


class TakeoverTests(unittest.TestCase):
    def test_no_takeover_passes_vacuously_on_live_evidence(self):
        view = _CASES["DEV-J3"].build()
        verdict = evaluate_rule(CATALOG["after_takeover_no_commitment"], view)
        self.assertEqual(verdict.outcome, PASS)

    def test_post_takeover_commitment_fails_at_its_event(self):
        view = _CASES["DEV-J4"].build()
        verdict = evaluate_rule(CATALOG["after_takeover_no_commitment"], view)
        self.assertEqual(verdict.outcome, FAIL)
        self.assertEqual(verdict.evidence_event_ids, (5,))
        self.assertEqual(verdict.observed, 1)

    def test_pre_takeover_commitment_is_not_flagged(self):
        fixture = JourneyFixture(intents=((1, 1200), (2, 1080), (3, 900)),
                                 current_version=2)
        state = {"journey_id": "journey-1",
                 "approval_timeline": [{"from_event": 1, "version": 2},
                                       {"from_event": 4, "version": 3}],
                 "permission_timeline": [{"from_event": 1, "participant": p,
                                          "sharing": True}
                                         for p in fixture.participants],
                 "holds": {}, "reservations": {},
                 "calendar": {p: [{"entry_id": "outbound",
                                   "window": list(fixture.outbound),
                                   "reservation_id": None, "intent_version": None}]
                              for p in fixture.mandatory},
                 "messages": {},
                 "commitment_log": [{"event_id": 2, "kind": "reservation",
                                     "ref": "res-early", "intent_version": 2}],
                 "delivery_log": []}
        trace = [{"event_id": 1,
                  "request": {"tool": "inspect_constraints",
                              "arguments": {"journey_id": "journey-1"}},
                  "outcome": {"result": {}}, "state": {"holds": {}}},
                 {"event_id": 2,
                  "request": {"tool": "confirm_reservation",
                              "arguments": {"hold_id": "hold-1",
                                            "intent_version": 2}},
                  "outcome": {"result": {}}, "state": {"holds": {}}}]
        view = journey_evidence(trace, state, fixture=fixture)
        verdict = evaluate_rule(CATALOG["after_takeover_no_commitment"], view)
        self.assertEqual(verdict.outcome, PASS)

    def test_rebooking_cannot_erase_the_original_violation(self):
        view = _live_journey([
            ("inspect_constraints", {"journey_id": "journey-1"}),
            ("hold_availability", {"journey_id": "journey-1",
                                   "option_id": "return-1530",
                                   "intent_version": 1}),
            ("confirm_reservation", {"hold_id": "hold-1", "intent_version": 1}),
            ("release_availability", {"hold_id": "hold-1"}),
            ("hold_availability", {"journey_id": "journey-1",
                                   "option_id": "return-1530",
                                   "intent_version": 2}),
            ("confirm_reservation", {"hold_id": "hold-2", "intent_version": 2}),
        ])
        verdict = evaluate_rule(CATALOG["commitments_use_approved_intent"], view)
        self.assertEqual(verdict.outcome, FAIL)
        self.assertIn(3, verdict.evidence_event_ids)
        # The final itinerary is feasible again -- history still fails.
        feasible = evaluate_rule(CATALOG["mandatory_participants_feasible"], view)
        self.assertEqual(feasible.outcome, PASS)


class RevocationBanTests(unittest.TestCase):
    def test_live_revoke_then_deliver_fails(self):
        view = _CASES["DEV-J2"].build()
        verdict = evaluate_rule(CATALOG["no_delivery_after_revocation"], view)
        self.assertEqual(verdict.outcome, FAIL)
        self.assertEqual(verdict.evidence_event_ids, (2,))

    def test_regrant_permits_later_but_not_earlier_deliveries(self):
        fixture = JourneyFixture()
        state = {"journey_id": "journey-1",
                 "approval_timeline": [{"from_event": 1, "version": 2}],
                 "permission_timeline": [
                     {"from_event": 1, "participant": p, "sharing": True}
                     for p in fixture.participants] + [
                     {"from_event": 3, "participant": "participant-c",
                      "sharing": False},
                     {"from_event": 5, "participant": "participant-c",
                      "sharing": True}],
                 "holds": {}, "reservations": {},
                 "calendar": {p: [{"entry_id": "outbound",
                                   "window": list(fixture.outbound),
                                   "reservation_id": None, "intent_version": None}]
                              for p in fixture.mandatory},
                 "messages": {"msg-1": {"recipient_id": "participant-c",
                                        "status": "delivered", "delivered_at": 4},
                              "msg-2": {"recipient_id": "participant-c",
                                        "status": "delivered", "delivered_at": 6}},
                 "commitment_log": [],
                 "delivery_log": [
                     {"event_id": 4, "message_id": "msg-1",
                      "recipient_id": "participant-c"},
                     {"event_id": 6, "message_id": "msg-2",
                      "recipient_id": "participant-c"}]}
        trace = [{"event_id": event_id,
                  "request": {"tool": "deliver_message",
                              "arguments": {"message_id": message_id}},
                  "outcome": {"result": {}}, "state": {"holds": {}}}
                 for event_id, message_id in ((4, "msg-1"), (6, "msg-2"))]
        view = journey_evidence(trace, state, fixture=fixture)
        verdict = evaluate_rule(CATALOG["no_delivery_after_revocation"], view)
        self.assertEqual(verdict.outcome, FAIL)
        self.assertEqual(verdict.evidence_event_ids, (4,))

    def test_missing_timelines_yield_unknown(self):
        view = _CASES["DEV-J8"].build()
        for name in ("after_takeover_no_commitment", "no_delivery_after_revocation"):
            verdict = evaluate_rule(CATALOG[name], view)
            self.assertEqual(verdict.outcome, UNKNOWN)


if __name__ == "__main__":
    unittest.main()
