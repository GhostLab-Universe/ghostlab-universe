"""Batch corpus evaluation tests: one target across many pinned cases.

Standard library only (unittest style).
"""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

from ghostlab.replay.corpus_eval import (
    CORPUS_EVAL_SCHEMA, NOT_APPLICABLE, PASS, TESTED, UNKNOWN, UNTESTED,
    VIOLATION, default_registry, evaluate_corpus, format_summary, gate,
    load_corpus, parse_target)
from ghostlab.replay.recorded import canonical
from ghostlab.verify import RULES_VERSION, TEMPORAL_RULES_VERSION

ROOT = Path(__file__).resolve().parents[2]
EXT = str(ROOT / "examples" / "regressions" / "ext")

BLIND = {"kind": "script", "id": "blind",
         "actions": [{"tool": "reserve",
                      "arguments": {"holder": "demo", "seats": 2}}]}
CAREFUL = {"kind": "script", "id": "careful",
           "actions": [{"tool": "check_availability", "arguments": {}},
                       {"tool": "reserve",
                        "arguments": {"holder": "demo", "seats": 1}}]}
REFUSAL = {"kind": "script", "id": "refusal", "actions": []}
STALE_PROBE = {"kind": "script", "id": "stale-probe",
               "actions": [{"tool": "check_availability", "arguments": {}},
                           {"tool": "check_availability", "arguments": {}},
                           {"tool": "reserve",
                            "arguments": {"holder": "demo", "seats": 1}}]}


def _eval_cli(argv):
    path = ROOT / "scripts" / "eval_corpus.py"
    spec = importlib.util.spec_from_file_location("eval_corpus_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, buffer.getvalue()


class TargetParsingTests(unittest.TestCase):
    def test_registered_target(self):
        target = parse_target({"kind": "registered", "id": "repaired"})
        self.assertEqual((target["kind"], target["id"], target["actions"]),
                         ("registered", "repaired", None))

    def test_script_target_defaults_its_id(self):
        target = parse_target({"kind": "script", "actions": []})
        self.assertEqual(target["id"], "corpus-script")
        self.assertEqual(target["actions"], ())

    def test_bad_specs_rejected(self):
        for spec in (None, {}, {"kind": "nope"}, {"kind": "registered"},
                     {"kind": "registered", "id": ""},
                     {"kind": "registered", "id": "x", "extra": 1},
                     {"kind": "script"}, {"kind": "script", "actions": {}},
                     {"kind": "script", "actions": [{"tool": "x"}]},
                     {"kind": "script", "actions": [{"arguments": {}}]},
                     {"kind": "script", "actions": [{"tool": "", "arguments": {}}]},
                     {"kind": "script", "actions": [{"tool": "x",
                                                     "arguments": []}]},
                     {"kind": "script", "id": "", "actions": []}):
            with self.assertRaises(ValueError, msg=spec):
                parse_target(spec)


class LoadCorpusTests(unittest.TestCase):
    def test_loads_the_ext_corpus(self):
        entries = load_corpus([EXT])
        self.assertEqual([e["case"]["case_id"] for e in entries],
                         ["reservation-blind-double-book",
                          "reservation-careful-safe"])

    def test_missing_and_empty_paths_rejected(self):
        with self.assertRaises(ValueError):
            load_corpus([str(ROOT / "examples" / "no-such-dir")])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_corpus([tmp])
        with self.assertRaises(ValueError):
            load_corpus([])

    def test_malformed_files_rejected_exactly_like_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_json = Path(tmp) / "bad.json"
            bad_json.write_text("{nope", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_corpus([str(bad_json)])
            wrong_keys = Path(tmp) / "keys.json"
            wrong_keys.write_text(json.dumps({"case_id": "x"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_corpus([str(wrong_keys)])


class VerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus([EXT])

    def test_two_targets_give_different_per_case_verdicts(self):
        blind = evaluate_corpus(corpus=self.corpus,
                                target=parse_target(BLIND))
        careful = evaluate_corpus(corpus=self.corpus,
                                  target=parse_target(CAREFUL))
        blind_verdicts = [c["verdict"] for c in blind["cases"]]
        careful_verdicts = [c["verdict"] for c in careful["cases"]]
        self.assertEqual(blind_verdicts, [VIOLATION, VIOLATION])
        self.assertEqual(careful_verdicts, [PASS, PASS])
        self.assertNotEqual(blind_verdicts, careful_verdicts)

    def test_violations_name_their_checks_and_witness(self):
        report = evaluate_corpus(corpus=self.corpus, target=parse_target(BLIND))
        for record in report["cases"]:
            self.assertEqual(record["failed_checks"], ["capacity_respected"])
            self.assertIn("capacity_respected", record["detail"])
            self.assertEqual(len(record["witness_sha256"]), 64)
        self.assertEqual(report["summary"]["violations"],
                         ["reservation-blind-double-book",
                          "reservation-careful-safe"])

    def test_registered_targets_differ_on_dev_refund_scenarios(self):
        vulnerable = evaluate_corpus(
            target=parse_target({"kind": "registered", "id": "vulnerable"}),
            include_dev=True, only=["DEV-R1"])
        repaired = evaluate_corpus(
            target=parse_target({"kind": "registered", "id": "repaired"}),
            include_dev=True, only=["DEV-R1"])
        self.assertEqual(vulnerable["cases"][0]["verdict"], VIOLATION)
        self.assertIn("no_duplicate_full_refund",
                      vulnerable["cases"][0]["failed_rules"])
        self.assertEqual(repaired["cases"][0]["verdict"], PASS)

    def test_refusal_only_fails_completion_not_safety(self):
        report = evaluate_corpus(corpus=self.corpus,
                                 target=parse_target(REFUSAL))
        for record in report["cases"]:
            self.assertEqual(record["verdict"], PASS)
            self.assertFalse(record["completed"])
            self.assertEqual(record["usage"], {"tool_calls": 0})
        self.assertEqual(len(report["summary"]["completion_failures"]), 2)
        ok, _ = gate(report)
        self.assertTrue(ok)  # completion is reported, never judged

    def test_denied_run_is_unknown_with_failed_completion(self):
        report = evaluate_corpus(corpus=self.corpus, target=parse_target(BLIND),
                                 allow=["check_availability"])
        for record in report["cases"]:
            self.assertEqual(record["verdict"], UNKNOWN)
            self.assertEqual(record["coverage"], UNTESTED)
            self.assertTrue(record["denied"])
            self.assertIn("denied", record["detail"])
            self.assertFalse(record["completed"])

    def test_budget_cut_fails_completion(self):
        report = evaluate_corpus(corpus=self.corpus,
                                 target=parse_target(CAREFUL), cap=1)
        for record in report["cases"]:
            self.assertEqual(record["status"], "budget_exhausted")
            self.assertFalse(record["completed"])


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus([EXT])

    def test_unfired_required_fault_is_untested_never_pass(self):
        report = evaluate_corpus(
            corpus=self.corpus, target=parse_target(CAREFUL),
            required_faults=["skip_capacity_check"])
        for record in report["cases"]:
            self.assertEqual(record["coverage"], UNTESTED)
            self.assertEqual(record["verdict"], UNKNOWN)
            self.assertIn("skip_capacity_check", record["detail"])
            self.assertNotIn("skip_capacity_check",
                             record["pins"]["faults_fired"])
        ok, failed = gate(report)
        self.assertFalse(ok)
        self.assertEqual(len(failed["unknowns"]), 2)

    def test_fired_required_fault_keeps_the_case_tested(self):
        # The stale fault fires on the second availability read, once a
        # cached snapshot exists to go stale.
        report = evaluate_corpus(
            corpus=self.corpus, target=parse_target(STALE_PROBE),
            required_faults=["timeout_after_commit", "stale_snapshot"])
        for record in report["cases"]:
            self.assertEqual(record["coverage"], TESTED)
            self.assertEqual(record["verdict"], PASS)
            self.assertIn("timeout_after_commit", record["pins"]["faults_fired"])
            self.assertIn("stale_snapshot", record["pins"]["faults_fired"])

    def test_violation_with_unfired_fault_stays_a_violation(self):
        report = evaluate_corpus(
            corpus=self.corpus, target=parse_target(BLIND),
            required_faults=["stale_snapshot"])
        for record in report["cases"]:
            self.assertEqual(record["coverage"], UNTESTED)
            self.assertEqual(record["verdict"], VIOLATION)

    def test_unknown_required_fault_is_a_per_case_unknown(self):
        report = evaluate_corpus(
            corpus=self.corpus, target=parse_target(CAREFUL),
            required_faults=["no-such-fault"])
        for record in report["cases"]:
            self.assertEqual(record["verdict"], UNKNOWN)
            self.assertIn("no-such-fault", record["detail"])

    def test_dev_table_pins_the_timeout_fault(self):
        report = evaluate_corpus(
            target=parse_target({"kind": "registered", "id": "repaired"}),
            include_dev=True, only=["DEV-R1"])
        record = report["cases"][0]
        self.assertEqual(record["pins"]["required_faults"],
                         ["timeout_after_commit"])
        self.assertEqual(record["coverage"], TESTED)


class NotApplicableTests(unittest.TestCase):
    def test_unregistered_target_for_world_is_declined(self):
        corpus = load_corpus([EXT])
        report = evaluate_corpus(
            corpus=corpus,
            target=parse_target({"kind": "registered", "id": "vulnerable"}))
        for record in report["cases"]:
            self.assertEqual(record["verdict"], NOT_APPLICABLE)
            self.assertEqual(record["coverage"], NOT_APPLICABLE)
            self.assertIsNone(record["completed"])
            self.assertIn("not registered", record["detail"])
        ok, _ = gate(report)
        self.assertTrue(ok)  # declined cases are listed, never gate-failing

    def test_unknown_world_is_declined(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = load_corpus([EXT])[0]["case"]
            foreign = deepcopy(entry)
            foreign["case_id"] = "foreign-case"
            foreign["world_version"] = "nope-world/9"
            path = Path(tmp) / "foreign.json"
            path.write_text(json.dumps(foreign), encoding="utf-8")
            report = evaluate_corpus(corpus=load_corpus([tmp]),
                                     target=parse_target(CAREFUL))
        record = report["cases"][0]
        self.assertEqual(record["verdict"], NOT_APPLICABLE)
        self.assertIn("no adapter package", record["detail"])

    def test_dev_entry_with_missing_world_is_declined(self):
        from ghostlab.adapters.reservation_env import (
            register_reservation_adapters)
        from ghostlab.extensions.registry import AdapterRegistry
        registry = AdapterRegistry()
        register_reservation_adapters(registry)
        self.assertNotIn("refund-world/1", registry.worlds())
        report = evaluate_corpus(
            registry=registry, target=parse_target(CAREFUL),
            include_dev=True, only=["DEV-R1"])
        self.assertEqual(len(report["cases"]), 1)
        record = report["cases"][0]
        self.assertEqual(record["case_id"], "DEV-R1")
        self.assertEqual(record["verdict"], NOT_APPLICABLE)
        self.assertIn("no adapter package", record["detail"])

    def test_dev_modes_are_all_accounted_for(self):
        report = evaluate_corpus(
            target=parse_target({"kind": "registered", "id": "repaired"}),
            include_dev=True)
        self.assertEqual(report["summary"]["total"], 16)
        declined = {c["case_id"]: c["detail"] for c in report["cases"]
                    if c["verdict"] == NOT_APPLICABLE}
        self.assertEqual(len(declined), 12)
        for case_id in ("DEV-R4a", "DEV-R4b", "DEV-R5", "DEV-R6",
                        "DEV-J4", "DEV-J5", "DEV-J6", "DEV-J7", "DEV-J8"):
            self.assertIn("synthetic", declined[case_id])
        for case_id in ("DEV-J1", "DEV-J2", "DEV-J3"):
            self.assertIn("no extension adapter", declined[case_id])

    def test_case_filter_restricts_both_sources(self):
        corpus = load_corpus([EXT])
        report = evaluate_corpus(corpus=corpus, target=parse_target(CAREFUL),
                                 include_dev=True,
                                 only=["reservation-careful-safe", "DEV-R3"])
        self.assertEqual(sorted(c["case_id"] for c in report["cases"]),
                         ["DEV-R3", "reservation-careful-safe"])
        self.assertEqual(report["case_filter"],
                         ["DEV-R3", "reservation-careful-safe"])


class PinsUsageAndSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus([EXT])
        cls.report = evaluate_corpus(corpus=cls.corpus,
                                     target=parse_target(CAREFUL),
                                     only=["reservation-careful-safe"])
        cls.dev_report = evaluate_corpus(
            target=parse_target({"kind": "registered", "id": "repaired"}),
            include_dev=True, only=["DEV-R1"])

    def test_pins_stamp_env_fixture_rules_and_faults(self):
        report = self.report
        self.assertEqual(report["schema"], CORPUS_EVAL_SCHEMA)
        self.assertEqual(report["pins"]["rules_version"], RULES_VERSION)
        self.assertEqual(report["pins"]["temporal_rules_version"],
                         TEMPORAL_RULES_VERSION)
        self.assertIn("refund-world/1", report["pins"]["worlds"])
        self.assertIn("reservation-world/1", report["pins"]["worlds"])
        ext = report["cases"][0]["pins"]
        self.assertEqual(ext["environment_version"], "reservation-environment/1")
        self.assertEqual(len(ext["fixture_sha256"]), 64)
        self.assertTrue(ext["scenario"]["timeout_after_commit"])
        self.assertEqual(ext["required_faults"], [])
        dev = self.dev_report["cases"][0]["pins"]
        self.assertEqual(dev["environment_version"], "refund-environment/1")
        self.assertEqual(len(dev["fixture_sha256"]), 64)
        self.assertEqual(dev["scenario"],
                         {"timeout_after_commit": True, "max_tool_calls": 20})
        self.assertEqual(dev["required_faults"], ["timeout_after_commit"])
        self.assertEqual(dev["faults_fired"], ["timeout_after_commit"])

    def test_usage_accounts_tool_calls_and_zero_models(self):
        report = self.report
        total = sum(c["usage"]["tool_calls"] for c in report["cases"])
        self.assertEqual(report["usage"],
                         {"cases": 1, "tool_calls": total, "model_calls": 0})
        self.assertGreater(total, 0)

    def test_summary_lists_every_dimension_explicitly(self):
        report = self.report
        summary = report["summary"]
        self.assertEqual(summary["by_verdict"],
                         {"pass": 1, "violation": 0, "unknown": 0,
                          "not-applicable": 0})
        self.assertEqual(summary["by_world"]["reservation-world/1"]["pass"], 1)
        self.assertEqual(summary["by_coverage"]["tested"], 1)
        for key in ("violations", "unknowns", "not_applicable", "untested",
                    "completion_failures", "completion_not_evaluated"):
            self.assertEqual(summary[key], [], key)

    def test_report_is_deterministic_and_json_stable(self):
        first = evaluate_corpus(corpus=self.corpus,
                                target=parse_target(CAREFUL))
        second = evaluate_corpus(corpus=self.corpus,
                                 target=parse_target(CAREFUL))
        self.assertEqual(canonical(first), canonical(second))
        json.dumps(first, sort_keys=True)

    def test_failed_batch_never_hides_its_regressions(self):
        report = evaluate_corpus(corpus=self.corpus,
                                 target=parse_target(BLIND))
        summary = report["summary"]
        self.assertEqual(summary["by_verdict"]["violation"], 2)
        self.assertEqual(summary["violations"],
                         ["reservation-blind-double-book",
                          "reservation-careful-safe"])
        text = format_summary(report)
        self.assertIn("violation", text)
        self.assertIn("reservation-blind-double-book", text)
        self.assertIn("model_calls=0", text)

    def test_harness_touches_no_network_or_model_surface(self):
        for path in (ROOT / "src" / "ghostlab" / "replay" / "corpus_eval.py",
                     ROOT / "scripts" / "eval_corpus.py"):
            source = path.read_text(encoding="utf-8")
            for token in ("socket", "urllib", "http.client", "requests",
                          "httpx", "ssl", "subprocess", "openai", "anthropic"):
                self.assertNotIn(token, source, f"{path.name}::{token}")


class RobustnessTests(unittest.TestCase):
    def test_one_bad_case_never_aborts_the_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = load_corpus([EXT])[0]["case"]
            broken = deepcopy(entry)
            broken["case_id"] = "broken-fixture"
            broken["fixture"] = {"fixture": "near_full"}  # missing keys
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            report = evaluate_corpus(corpus=load_corpus([tmp, EXT]),
                                     target=parse_target(CAREFUL))
        by_id = {c["case_id"]: c for c in report["cases"]}
        self.assertEqual(by_id["broken-fixture"]["verdict"], UNKNOWN)
        self.assertIn("retest error", by_id["broken-fixture"]["detail"])
        self.assertEqual(by_id["reservation-careful-safe"]["verdict"], PASS)

    def test_options_are_validated(self):
        corpus = load_corpus([EXT])
        target = parse_target(CAREFUL)
        for kwargs in ({"required_faults": [""]}, {"required_faults": "x"},
                       {"cap": 0}, {"cap": 1001}, {"allow": []},
                       {"allow": [""]}, {"only": [""]}, {"only": "DEV-R1"}):
            with self.assertRaises(ValueError, msg=kwargs):
                evaluate_corpus(corpus=corpus, target=target, **kwargs)
        with self.assertRaises(ValueError):
            evaluate_corpus(corpus=corpus, target={"kind": "nope"})
        with self.assertRaises(ValueError):
            evaluate_corpus(corpus=[{"nope": 1}], target=target)

    def test_default_registry_holds_both_worlds(self):
        registry = default_registry()
        self.assertIn("refund-world/1", registry.worlds())
        self.assertIn("reservation-world/1", registry.worlds())


class CliTests(unittest.TestCase):
    def test_cli_reports_gate_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            blind = Path(tmp) / "blind.json"
            blind.write_text(json.dumps(BLIND["actions"]), encoding="utf-8")
            code, out = _eval_cli(["--script", str(blind), EXT])
            self.assertEqual(code, 1)
            self.assertIn("gate FAILED", out)
            careful = Path(tmp) / "careful.json"
            careful.write_text(json.dumps(CAREFUL["actions"]), encoding="utf-8")
            report_path = Path(tmp) / "report.json"
            code, out = _eval_cli(["--script", str(careful), EXT,
                                   "--out", str(report_path)])
            self.assertEqual(code, 0)
            self.assertIn("gate passed", out)
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["summary"]["by_verdict"]["pass"], 2)

    def test_cli_usage_errors_exit_two(self):
        code, out = _eval_cli(["--script", str(ROOT / "nope.json"), EXT])
        self.assertEqual(code, 2)
        code, out = _eval_cli(["--target-id", "repaired"])
        self.assertEqual(code, 2)
        self.assertIn("no case files", out)

    def test_cli_case_filter_matching_nothing_exits_two(self):
        code, out = _eval_cli(["--target-id", "repaired",
                                "--case", "NOPE-9", "--include-dev"])
        self.assertEqual(code, 2)
        self.assertIn("no cases matched", out)


if __name__ == "__main__":
    unittest.main()
