"""P4 held-out boundary tests: freeze protocol, contracts and PENDING default.

Standard library only (unittest style).
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from ghostlab.verify import (HELDOUT_RESULT_VERSION, HELDOUT_RUNNER_VERSION,
                             HeldoutContractError, NotCleanError,
                             evaluate_heldout, freeze, validate_result)

_GIT = ["git", "-c", "safe.directory=*"]


def _git(*args, cwd):
    return subprocess.run(_GIT + list(args), cwd=cwd, capture_output=True,
                          text=True, check=True, timeout=60)


def _good_result(commit="abc123"):
    return {"contract_version": HELDOUT_RESULT_VERSION,
            "runner_version": HELDOUT_RUNNER_VERSION,
            "freeze_commit": commit,
            "scenarios": [
                {"scenario_id": "heldout-1", "detected": True,
                 "rules_fired": ["total_within_payment"]},
                {"scenario_id": "heldout-2", "detected": False,
                 "rules_fired": []},
            ]}


class StubRunner:
    def __init__(self, result):
        self.result = result
        self.seen = None

    def run(self, frozen):
        self.seen = dict(frozen)
        return self.result


class PendingTests(unittest.TestCase):
    def test_no_runner_is_pending_without_substitution(self):
        report = evaluate_heldout()
        self.assertEqual(report["status"], "PENDING")
        self.assertFalse(report["dev_results_substituted"])
        self.assertEqual(report["scenarios_evaluated"], 0)
        self.assertNotIn("summary", report)
        self.assertNotIn("scenarios", report)

    def test_non_runner_is_rejected(self):
        with self.assertRaises(TypeError):
            evaluate_heldout(runner=object())


class FreezeTests(unittest.TestCase):
    def _repo(self, path):
        _git("init", cwd=path)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty",
             "-m", "init", cwd=path)

    def test_clean_checkout_freezes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            frozen = freeze(tmp)
            self.assertEqual(len(frozen.commit), 40)
            self.assertEqual(frozen.checkout, tmp)
            self.assertEqual(frozen.rules_version, "ghostlab-rules/1")

    def test_dirty_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            Path(tmp, "scratch.txt").write_text("uncommitted", encoding="utf-8")
            with self.assertRaises(NotCleanError):
                freeze(tmp)

    def test_non_repo_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(NotCleanError):
                freeze(tmp)


class ContractTests(unittest.TestCase):
    def test_good_result_validates_with_summary(self):
        checked = validate_result(_good_result())
        self.assertEqual(checked["summary"], {"evaluated": 2, "detected": 1})
        self.assertEqual(checked["scenarios"][0]["rules_fired"],
                         ("total_within_payment",))

    def test_scenario_content_leak_is_rejected(self):
        leaky = _good_result()
        leaky["scenarios"][0]["actions"] = ["issue_refund"]
        with self.assertRaises(HeldoutContractError):
            validate_result(leaky)

    def test_top_level_content_leak_is_rejected(self):
        leaky = _good_result()
        leaky["fixtures"] = {}
        with self.assertRaises(HeldoutContractError):
            validate_result(leaky)

    def test_unknown_rule_is_rejected(self):
        bad = _good_result()
        bad["scenarios"][0]["rules_fired"] = ["not-a-rule"]
        with self.assertRaises(HeldoutContractError):
            validate_result(bad)

    def test_wrong_versions_are_rejected(self):
        bad = _good_result()
        bad["contract_version"] = "something-else/1"
        with self.assertRaises(HeldoutContractError):
            validate_result(bad)
        bad = _good_result()
        bad["runner_version"] = "something-else/1"
        with self.assertRaises(HeldoutContractError):
            validate_result(bad)

    def test_duplicate_ids_and_bad_shapes_rejected(self):
        bad = _good_result()
        bad["scenarios"][1]["scenario_id"] = "heldout-1"
        with self.assertRaises(HeldoutContractError):
            validate_result(bad)
        bad = _good_result()
        bad["scenarios"][1]["detected"] = "yes"
        with self.assertRaises(HeldoutContractError):
            validate_result(bad)


class CompletePathTests(unittest.TestCase):
    def test_stub_runner_completes_on_a_frozen_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            _git("init", cwd=tmp)
            _git("-c", "user.email=t@t", "-c", "user.name=t", "commit",
                 "--allow-empty", "-m", "init", cwd=tmp)
            commit = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
            runner = StubRunner(_good_result(commit=commit))
            report = evaluate_heldout(runner=runner, checkout=tmp)
            self.assertEqual(report["status"], "COMPLETE")
            self.assertEqual(report["freeze"]["commit"], commit)
            self.assertEqual(report["result"]["summary"]["evaluated"], 2)
            self.assertEqual(runner.seen["commit"], commit)


if __name__ == "__main__":
    unittest.main()
