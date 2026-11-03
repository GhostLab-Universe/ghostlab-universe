"""Held-out harness tests: self-test E2E, separation, namespaces, freeze, strictness.

Standard library only (unittest style). No model calls, no network. Every
scenario-shaped file used here is either the shipped SYNTHETIC fixture or a
clearly-labeled synthetic probe built in a temp dir -- never hidden content.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import ghostlab.harness
from ghostlab.explore.harness import ScriptedTarget
from ghostlab.extensions.target import TargetInfo
from ghostlab.harness import (HELDOUT_CONFIG_VERSION, HELDOUT_MANIFEST_VERSION,
                              HELDOUT_NAMESPACE, HELDOUT_SPEC_VERSION,
                              SYNTHETIC_NAMESPACE, FreezeMismatchError,
                              HeldoutManifestError, HeldoutResultError,
                              HeldoutSpecError, NamespaceError, dump_result_json,
                              frozen_handshake, run_heldout, run_selftest,
                              to_legacy_result, validate_result,
                              verify_handshake)
from ghostlab.verify import (NotCleanError, RULES_VERSION,
                             validate_result as legacy_validate_result)

_GIT = ["git", "-c", "safe.directory=*"]
_FIXTURE_DIR = Path(ghostlab.harness.__file__).parent / "selftest_fixture"
_FREEZE = {"commit": "synthetic-test-commit", "rules_version": RULES_VERSION}


def _git(*args, cwd):
    return subprocess.run(_GIT + list(args), cwd=cwd, capture_output=True,
                          text=True, check=True, timeout=60)


def _init_repo(path):
    _git("init", cwd=path)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty",
         "-m", "init", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _config(namespace, **overrides):
    config = {"config_version": HELDOUT_CONFIG_VERSION, "namespace": namespace,
              "seed": 11, "max_attempts": 100, "action_cap": 20}
    config.update(overrides)
    return config


def _probe_spec(*, script=None, world="refund", extra=None):
    """A synthetic held-out-shaped spec; CANARY marks must never leak."""
    spec = {"spec_version": HELDOUT_SPEC_VERSION, "world": world,
            "fixture": {"order_id": "probe-order-CANARY",
                        "customer_id": "probe-customer-CANARY",
                        "paid_minor": 50000, "currency": "INR"},
            "scenario": {"timeout_after_commit": False, "max_tool_calls": 20},
            "actor_id": "probe-actor-CANARY"}
    if script is not None:
        spec["candidate_script"] = script
    if extra:
        spec.update(extra)
    return spec


_PROBE_SCRIPT = [{"tool": "get_order",
                  "arguments": {"order_id": "probe-order-CANARY",
                                "customer_id": "probe-customer-CANARY"}}]


def _write_set(root, namespace, specs):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cases = []
    for case_id, spec in specs:
        name = f"{case_id}.json"
        (root / name).write_text(json.dumps(spec), encoding="utf-8")
        cases.append({"case_id": case_id, "spec": name})
    manifest = {"manifest_version": HELDOUT_MANIFEST_VERSION,
                "namespace": namespace, "cases": cases}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(root)


def _immediate_candidate(case_id, handle):
    del case_id, handle
    return ScriptedTarget([], adapter_id="immediate-probe")


class _RecordingTarget:
    def __init__(self, record):
        self._record = record

    def info(self):
        return TargetInfo(adapter_id="separation-probe",
                          adapter_version="probe/1", model=None, provider=None,
                          endpoint=None)

    def start(self, task, observation):
        del observation
        self._record["task"] = {"task_id": task.task_id,
                                "params": dict(task.params)}

    def propose(self, observation):
        del observation
        return None

    def notify_result(self, action, outcome):
        del action, outcome

    def close(self, reason):
        self._record["closed"] = reason


class _SnoopingFactory:
    """Grabs everything the harness hands the candidate, then looks for leaks."""

    def __init__(self):
        self.calls = []

    def __call__(self, case_id, handle):
        seen = {"case_id": case_id, "handle_type": type(handle).__name__,
                "handle_repr": repr(handle)}
        attrs = sorted(a for a in dir(handle) if not a.startswith("_"))
        seen["handle_attrs"] = attrs
        for attr in attrs:
            value = getattr(handle, attr)
            seen[f"handle.{attr}"] = value() if callable(value) else value
        record = {}
        self.calls.append({"seen": seen, "target_record": record})
        return _RecordingTarget(record)


class SelfTestTests(unittest.TestCase):
    def test_bundled_fixture_verdicts(self):
        result = run_selftest(freeze_proof=dict(_FREEZE))
        by_id = {case["case_id"]: case for case in result["cases"]}
        self.assertEqual(by_id["SYNTH-PASS-1"]["verdict"], "pass")
        self.assertEqual(by_id["SYNTH-VIOLATION-1"]["verdict"], "violation")
        self.assertEqual(by_id["SYNTH-VIOLATION-1"]["rules_fired"],
                         ("no_duplicate_full_refund", "total_within_payment"))
        self.assertEqual(by_id["SYNTH-UNKNOWN-1"]["verdict"], "unknown")
        self.assertEqual(by_id["SYNTH-UNKNOWN-1"]["unknown_rules"],
                         ("actor_within_project",))
        self.assertEqual(result["summary"],
                         {"evaluated": 3, "pass": 1, "violation": 1,
                          "unknown": 1})
        self.assertEqual(result["inference_usage"],
                         {"model_calls": 0, "cases_with_model": 0})
        for case in result["cases"]:
            self.assertEqual(case["inference_usage"]["model_calls"], 0)
            self.assertTrue(case["inference_usage"]["deterministic"])
        validate_result(json.loads(dump_result_json(result)))

    def test_selftest_is_deterministic(self):
        first = dump_result_json(run_selftest(freeze_proof=dict(_FREEZE)))
        second = dump_result_json(run_selftest(freeze_proof=dict(_FREEZE)))
        self.assertEqual(first, second)


class SeparationTests(unittest.TestCase):
    def test_candidate_receives_only_case_id_and_opaque_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(Path(tmp) / "scenarios", HELDOUT_NAMESPACE,
                                  [("probe-1", _probe_spec())])
            repo = Path(tmp) / "repo"
            repo.mkdir()
            commit = _init_repo(str(repo))
            snoop = _SnoopingFactory()
            result = run_heldout(
                scenario_dir=scenario,
                handshake=frozen_handshake(_config(HELDOUT_NAMESPACE)),
                freeze_commit=commit, candidate_factory=snoop,
                checkout=str(repo))
        validate_result(json.loads(dump_result_json(result)))
        self.assertEqual(result["summary"]["evaluated"], 1)
        self.assertEqual(len(snoop.calls), 1)
        seen = snoop.calls[0]["seen"]
        record = snoop.calls[0]["target_record"]
        blob = json.dumps({"seen": seen, "task": record.get("task")},
                          default=str)
        self.assertIn("probe-1", blob)
        self.assertNotIn("CANARY", blob)
        self.assertNotIn("timeout_after_commit", blob)
        self.assertEqual(seen["handle_attrs"], ["case_id", "token"])
        self.assertEqual(seen["handle.case_id"], "probe-1")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", seen["handle.token"]))
        self.assertEqual(record["task"],
                         {"task_id": "probe-1", "params": {"case_id": "probe-1"}})


class NamespaceTests(unittest.TestCase):
    def _repo(self, tmp):
        repo = Path(tmp) / "repo"
        repo.mkdir()
        return str(repo), _init_repo(str(repo))

    def test_run_heldout_refuses_synthetic_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout, commit = self._repo(tmp)
            with self.assertRaises(NamespaceError):
                run_heldout(
                    scenario_dir=str(_FIXTURE_DIR),
                    handshake=frozen_handshake(_config(HELDOUT_NAMESPACE)),
                    freeze_commit=commit,
                    candidate_factory=_immediate_candidate, checkout=checkout)

    def test_run_selftest_refuses_heldout_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(Path(tmp) / "scenarios", HELDOUT_NAMESPACE,
                                  [("probe-1", _probe_spec())])
            with self.assertRaises(NamespaceError):
                run_selftest(
                    fixture_dir=scenario, freeze_proof=dict(_FREEZE),
                    handshake=frozen_handshake(_config(SYNTHETIC_NAMESPACE)))

    def test_frozen_config_cannot_cross_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(
                Path(tmp) / "scenarios", SYNTHETIC_NAMESPACE,
                [("SYNTH-PROBE-1", _probe_spec(script=list(_PROBE_SCRIPT)))])
            with self.assertRaises(NamespaceError):
                run_selftest(
                    fixture_dir=scenario, freeze_proof=dict(_FREEZE),
                    handshake=frozen_handshake(_config(HELDOUT_NAMESPACE)))

    def test_tampered_handshake_refuses(self):
        handshake = frozen_handshake(_config(HELDOUT_NAMESPACE))
        handshake["config"]["seed"] += 1
        with self.assertRaises(FreezeMismatchError):
            verify_handshake(handshake)

    def test_handshake_snapshots_config(self):
        config = _config(HELDOUT_NAMESPACE)
        handshake = frozen_handshake(config)
        config["seed"] = 999999
        verify_handshake(handshake)  # the frozen copy is unaffected


class FreezeTests(unittest.TestCase):
    def _scenario(self, tmp):
        return _write_set(Path(tmp) / "scenarios", HELDOUT_NAMESPACE,
                          [("probe-1", _probe_spec())])

    def test_dirty_checkout_is_refused_before_scenarios_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = self._scenario(tmp)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_repo(str(repo))
            Path(repo, "scratch.txt").write_text("uncommitted", encoding="utf-8")
            with self.assertRaises(NotCleanError):
                run_heldout(
                    scenario_dir=scenario,
                    handshake=frozen_handshake(_config(HELDOUT_NAMESPACE)),
                    freeze_commit="0" * 40,
                    candidate_factory=_immediate_candidate,
                    checkout=str(repo))

    def test_moved_commit_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = self._scenario(tmp)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_repo(str(repo))
            with self.assertRaises(FreezeMismatchError):
                run_heldout(
                    scenario_dir=scenario,
                    handshake=frozen_handshake(_config(HELDOUT_NAMESPACE)),
                    freeze_commit="0" * 40,
                    candidate_factory=_immediate_candidate,
                    checkout=str(repo))


class StrictnessTests(unittest.TestCase):
    def _good_result(self):
        return json.loads(
            dump_result_json(run_selftest(freeze_proof=dict(_FREEZE))))

    def test_result_rejects_unknown_fields(self):
        bad = self._good_result()
        bad["fixtures"] = {}
        with self.assertRaises(HeldoutResultError):
            validate_result(bad)
        bad = self._good_result()
        bad["cases"][0]["trace"] = []
        with self.assertRaises(HeldoutResultError):
            validate_result(bad)

    def test_result_rejects_bad_verdicts_rules_and_digests(self):
        bad = self._good_result()
        bad["cases"][0]["verdict"] = "fail"
        with self.assertRaises(HeldoutResultError):
            validate_result(bad)
        bad = self._good_result()
        bad["cases"][0]["rules_fired"] = ["not-a-rule"]
        with self.assertRaises(HeldoutResultError):
            validate_result(bad)
        bad = self._good_result()
        bad["config_digest"] = "0" * 64
        with self.assertRaises(HeldoutResultError):
            validate_result(bad)

    def test_manifest_rejects_duplicates_and_mis_namespaced_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(Path(tmp) / "a", HELDOUT_NAMESPACE,
                                  [("probe-1", _probe_spec()),
                                   ("probe-1", _probe_spec())])
            with self.assertRaises(HeldoutManifestError):
                run_selftest(fixture_dir=scenario, freeze_proof=dict(_FREEZE),
                             handshake=frozen_handshake(
                                 _config(HELDOUT_NAMESPACE)))
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(Path(tmp) / "b", HELDOUT_NAMESPACE,
                                  [("SYNTH-1", _probe_spec())])
            with self.assertRaises(HeldoutManifestError):
                run_selftest(fixture_dir=scenario, freeze_proof=dict(_FREEZE),
                             handshake=frozen_handshake(
                                 _config(HELDOUT_NAMESPACE)))

    def test_heldout_spec_rejects_embedded_candidate_and_bad_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(
                Path(tmp) / "c", HELDOUT_NAMESPACE,
                [("probe-1", _probe_spec(script=list(_PROBE_SCRIPT)))])
            with self.assertRaises(HeldoutSpecError):
                run_selftest(fixture_dir=scenario, freeze_proof=dict(_FREEZE),
                             handshake=frozen_handshake(
                                 _config(HELDOUT_NAMESPACE)))
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(Path(tmp) / "d", HELDOUT_NAMESPACE,
                                  [("probe-1", _probe_spec(world="journey"))])
            with self.assertRaises(HeldoutSpecError):
                run_selftest(fixture_dir=scenario, freeze_proof=dict(_FREEZE),
                             handshake=frozen_handshake(
                                 _config(HELDOUT_NAMESPACE)))

    def test_case_count_beyond_frozen_bound_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = _write_set(
                Path(tmp) / "e", SYNTHETIC_NAMESPACE,
                [(f"SYNTH-PROBE-{n}",
                  _probe_spec(script=list(_PROBE_SCRIPT)))
                 for n in (1, 2)])
            with self.assertRaises(HeldoutManifestError):
                run_selftest(
                    fixture_dir=scenario, freeze_proof=dict(_FREEZE),
                    handshake=frozen_handshake(
                        _config(SYNTHETIC_NAMESPACE, max_attempts=1)))


class LegacyProjectionTests(unittest.TestCase):
    def test_projection_validates_against_consumer_contract(self):
        result = run_selftest(freeze_proof=dict(_FREEZE))
        checked = legacy_validate_result(to_legacy_result(result))
        self.assertEqual(checked["summary"], {"evaluated": 3, "detected": 1})
        self.assertEqual(checked["freeze_commit"], "synthetic-test-commit")
        by_id = {entry["scenario_id"]: entry
                 for entry in checked["scenarios"]}
        self.assertTrue(by_id["SYNTH-VIOLATION-1"]["detected"])
        self.assertFalse(by_id["SYNTH-PASS-1"]["detected"])
        self.assertFalse(by_id["SYNTH-UNKNOWN-1"]["detected"])


class FixturePurityTests(unittest.TestCase):
    def test_shipped_fixture_is_synthetic_only(self):
        names = sorted(path.name for path in _FIXTURE_DIR.iterdir())
        self.assertEqual(names, ["README-SYNTHETIC.txt", "SYNTH-PASS-1.json",
                                 "SYNTH-UNKNOWN-1.json", "SYNTH-VIOLATION-1.json",
                                 "manifest.json", "selftest_config.json"])
        manifest = json.loads((_FIXTURE_DIR / "manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(manifest["namespace"], SYNTHETIC_NAMESPACE)
        for case in manifest["cases"]:
            self.assertTrue(case["case_id"].startswith("SYNTH-"))


if __name__ == "__main__":
    unittest.main()
