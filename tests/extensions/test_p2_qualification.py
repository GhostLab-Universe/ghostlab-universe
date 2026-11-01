"""P2 qualification: the generic suite green on the reservation world.

Standard library only (unittest style). Runs
:func:`ghostlab.extensions.qualify.qualify_environment` against the
reservation adapter with expected tool definitions imported from the sample
OpenAPI document, then asserts the report passes, the missing-capability
report names snapshots honestly, and the suite itself stays live (tampered
inputs fail it) and isolated (one bad probe cannot abort it).
"""

import json
import unittest
from copy import deepcopy
from pathlib import Path

from ghostlab.adapters.reservation_env import (ENVIRONMENT_MANIFEST, RESERVATION_WORLD_VERSION,
                                               ReservationEnvironment)
from ghostlab.extensions.environment import Action, ToolSpec
from ghostlab.extensions.openapi_import import import_from_path
from ghostlab.extensions.qualify import (ProbeScript, QualificationReport, qualify_environment)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "reservation_openapi.json"
FIXTURE = "half_full"
CLEAN_SCENARIO = {"timeout_after_commit": False, "stale_snapshot": False,
                  "skip_capacity_check": False, "max_tool_calls": 20}
CHECK_NAMES = ("describe", "registration", "reset_correctness", "run_isolation",
               "schema_agreement", "unknown_tool_rejection", "unknown_arg_rejection",
               "cancellation", "evidence_availability", "replay_support",
               "missing_capabilities")


def script_actions():
    return [
        Action(tool="check_availability", arguments={}),
        Action(tool="reserve", arguments={"holder": "carol", "seats": 2}),
        Action(tool="get_status", arguments={"reservation_id": "R-0003"}),
        Action(tool="cancel", arguments={"reservation_id": "R-0003"}),
        Action(tool="get_status", arguments={"reservation_id": "R-0003"}),
        Action(tool="cancel", arguments={"reservation_id": "R-9999"}),
    ]


BASE_PROBE = ProbeScript(fixture=FIXTURE, scenario=dict(CLEAN_SCENARIO),
                         script=tuple(script_actions()))


def qualify(probe=BASE_PROBE, expected="sample"):
    tools = import_from_path(str(EXAMPLE), effect_map={"get_status": "read"}) if expected == "sample" else expected
    return qualify_environment(world_version=RESERVATION_WORLD_VERSION,
                               env_factory=ReservationEnvironment,
                               manifest=ENVIRONMENT_MANIFEST, probe=probe,
                               expected_tools=tools)


class QualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = qualify()

    def test_report_passes(self):
        self.assertIsInstance(self.report, QualificationReport)
        self.assertTrue(self.report.passed)
        self.assertIn("PASS", self.report.summary())
        self.assertEqual(self.report.world_version, RESERVATION_WORLD_VERSION)
        self.assertEqual(self.report.environment_id, "reservation-environment")

    def test_all_eleven_checks_pass(self):
        self.assertEqual(tuple(check.name for check in self.report.checks), CHECK_NAMES)
        for check in self.report.checks:
            self.assertTrue(check.passed, f"{check.name}: {check.details}")
            self.assertTrue(check.details, check.name)

    def test_missing_capabilities_name_snapshots(self):
        self.assertEqual(self.report.missing_capabilities, ("snapshot_restore",))
        missing = next(check for check in self.report.checks
                       if check.name == "missing_capabilities")
        self.assertIn("snapshot_restore", missing.details)

    def test_report_serializes_to_json(self):
        payload = json.dumps(self.report.to_dict())
        loaded = json.loads(payload)
        self.assertEqual(loaded["world_version"], RESERVATION_WORLD_VERSION)
        self.assertTrue(loaded["passed"])
        self.assertEqual(len(loaded["checks"]), 11)
        self.assertEqual(loaded["missing_capabilities"], ["snapshot_restore"])

    def test_agreement_without_expected_tools_skips(self):
        report = qualify(expected=None)
        self.assertTrue(report.passed)
        agreement = next(check for check in report.checks
                         if check.name == "schema_agreement")
        self.assertIn("skipped", agreement.details)


class SuiteLivenessTests(unittest.TestCase):
    def test_renamed_tools_fail_agreement(self):
        tampered = deepcopy(import_from_path(str(EXAMPLE)))
        tampered[1] = ToolSpec(name="WRONG", effect=tampered[1].effect,
                               parameters=tampered[1].parameters,
                               description=tampered[1].description)
        report = qualify(expected=tampered)
        self.assertFalse(report.passed)
        agreement = next(check for check in report.checks
                         if check.name == "schema_agreement")
        self.assertFalse(agreement.passed)

    def test_weakened_schemas_fail_agreement(self):
        tampered = deepcopy(import_from_path(str(EXAMPLE)))
        weakened = deepcopy(tampered[1].parameters)
        weakened["required"] = ["holder"]
        tampered[1] = ToolSpec(name=tampered[1].name, effect=tampered[1].effect,
                               parameters=weakened,
                               description=tampered[1].description)
        report = qualify(expected=tampered)
        self.assertFalse(report.passed)

    def test_bad_probes_fail_without_aborting(self):
        probe = ProbeScript(fixture="no-such-fixture", scenario=dict(CLEAN_SCENARIO),
                            script=tuple(script_actions()))
        report = qualify(probe=probe)
        self.assertFalse(report.passed)
        self.assertEqual(tuple(check.name for check in report.checks), CHECK_NAMES)
        reset = next(check for check in report.checks
                     if check.name == "reset_correctness")
        self.assertFalse(reset.passed)


class ProbeValidationTests(unittest.TestCase):
    def test_probe_scripts_are_validated(self):
        with self.assertRaises(ValueError):
            ProbeScript(fixture=FIXTURE, scenario=dict(CLEAN_SCENARIO), script=[])
        with self.assertRaises(ValueError):
            ProbeScript(fixture=FIXTURE, scenario=dict(CLEAN_SCENARIO),
                        script=[{"tool": "check_availability"}])
        with self.assertRaises(ValueError):
            ProbeScript(fixture="", scenario=dict(CLEAN_SCENARIO),
                        script=tuple(script_actions()))
        with self.assertRaises(ValueError):
            ProbeScript(fixture=FIXTURE, scenario=[], script=tuple(script_actions()))
        probe = ProbeScript(fixture=FIXTURE, scenario=dict(CLEAN_SCENARIO),
                            script=list(script_actions()))
        self.assertEqual(len(probe.script), 6)
        self.assertEqual(probe.task_params, {})

    def test_qualify_inputs_are_validated(self):
        expected = import_from_path(str(EXAMPLE))
        good = {"world_version": RESERVATION_WORLD_VERSION,
                "env_factory": ReservationEnvironment, "manifest": ENVIRONMENT_MANIFEST,
                "probe": BASE_PROBE, "expected_tools": expected}
        for key, bad in (("world_version", ""), ("env_factory", None),
                         ("manifest", "nope"), ("probe", "nope"),
                         ("expected_tools", []), ("expected_tools", [expected[0]] * 2),
                         ("expected_tools", ["nope"]), ("actor_id", "")):
            with self.assertRaises(ValueError, msg=(key, bad)):
                qualify_environment(**{**good, **{key: bad}})


if __name__ == "__main__":
    unittest.main()
