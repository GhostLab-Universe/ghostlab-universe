"""T4: external end-to-end proof for BOOKING through the shared pipeline.

Subprocess-runs ``examples/equipment_booking/e2e_proof.py`` (exit 0) and
directly asserts each stage artifact from its JSON summary plus the guard
files on disk, including an independent re-run of both exported guards.

Track-private temp dirs only; zero model calls, no secrets.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "equipment_booking" / "e2e_proof.py"
CONTROLLER = ROOT / "src" / "ghostlab" / "experiments" / "controller.py"


def run_child(args, timeout=600):
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"]
                               if env.get("PYTHONPATH") else "")
    return subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True,
                          text=True, timeout=timeout)


def run_guard(guard_path):
    return run_child([sys.executable, "-m", "pytest", str(guard_path), "-q",
                      "--no-header", "-p", "no:cacheprovider"], timeout=300)


class E2EProofT4Tests(unittest.TestCase):
    def test_proof_script_passes_and_every_stage_artifact_holds(self):
        with tempfile.TemporaryDirectory(prefix="t4-") as out:
            proc = run_child([sys.executable, str(SCRIPT), "--out", out])
            self.assertEqual(
                proc.returncode, 0,
                f"e2e_proof.py failed:\nSTDOUT:\n{proc.stdout}\n"
                f"STDERR:\n{proc.stderr}")
            summary = json.loads(
                (Path(out) / "summary.json").read_text(encoding="utf-8"))
            self.assertIs(summary["ok"], True)
            stages = summary["stages"]

            # No domain edits to the central controller.
            self.assertEqual(stages["controller_grep"]["book_equipment"], 0)
            self.assertEqual(
                stages["controller_grep"]["get_booking_status"], 0)

            # Register -> manifest -> qualify.
            self.assertEqual(stages["register"]["env_id"], "booking-env")
            self.assertIn("examples.equipment_booking.",
                          stages["register"]["allowlist"])
            self.assertTrue(stages["manifest"]["manifest_digest"])
            self.assertEqual(stages["qualify"]["status"],
                             "qualified-for-declared")
            self.assertTrue(stages["qualify"]["checks"])
            for check in stages["qualify"]["checks"]:
                self.assertIs(check["passed"], True, check["name"])

            # Target agents.
            self.assertEqual(stages["targets"]["agents"],
                             ["booking-corrected", "booking-naive"])

            # Resolve: unsupported rejected, two pinned booking records.
            self.assertIs(stages["resolve"]["unsupported_rejected"], True)
            self.assertNotEqual(stages["resolve"]["naive_resolved_id"],
                                stages["resolve"]["corrected_resolved_id"])

            # Naive run: duplicate violation with cited event/state evidence.
            naive = stages["naive_run"]
            self.assertEqual(naive["status"], "completed")
            self.assertEqual(naive["verdict"], "fail")
            self.assertEqual(naive["checks"],
                             {"authorized_bookings": True,
                              "no_duplicate_booking": False,
                              "unique_idempotency_keys": True})
            self.assertEqual(naive["rules_failed"], ["no_duplicate_booking"])
            self.assertEqual(naive["model_calls"], 0)
            self.assertIs(naive["fault_fired"], True)
            self.assertIs(type(naive["timeout_event_id"]), int)
            self.assertEqual(naive["total_bookings"], 2)
            self.assertEqual(naive["reservation_keys"],
                             ["booking-intent-1", "booking-retry-2"])

            # Minimize: violation preserved, stable, never grows.
            minimized = stages["minimize"]
            self.assertEqual(minimized["full_actions"], 3)
            self.assertLessEqual(minimized["required_actions"],
                                 minimized["full_actions"])
            self.assertIs(minimized["stable"], True)
            self.assertIs(minimized["required_violates"], True)

            # Corrected run: fresh pass under a distinct run id.
            fixed = stages["corrected_run"]
            self.assertEqual(fixed["verdict"], "pass")
            self.assertEqual(fixed["rules_failed"], [])
            self.assertEqual(fixed["model_calls"], 0)
            self.assertIs(fixed["fault_fired"], True)
            self.assertNotEqual(fixed["run_id"], naive["run_id"])

            # Guards: files exist, name their deps, recorded outcomes hold.
            guards = stages["guards"]
            naive_guard = Path(out) / guards["naive_guard"]
            fixed_guard = Path(out) / guards["corrected_guard"]
            for path in (naive_guard, fixed_guard):
                self.assertTrue(path.is_file(), str(path))
                text = path.read_text(encoding="utf-8")
                self.assertIn(guards["adapter_ref"], text)
                self.assertIn(guards["check_fn"], text)
            self.assertIn("BookingReplayAdapter", guards["adapter_ref"])
            self.assertIn("booking_prefix_violated", guards["check_fn"])
            self.assertEqual(guards["naive_pytest"]["returncode"], 1)
            self.assertIn("1 failed", guards["naive_pytest"]["tail"])
            self.assertEqual(guards["corrected_pytest"]["returncode"], 0)
            self.assertIn("1 passed", guards["corrected_pytest"]["tail"])

            # Independent re-run of both exported guards.
            rerun_naive = run_guard(naive_guard)
            self.assertEqual(rerun_naive.returncode, 1,
                             rerun_naive.stdout + rerun_naive.stderr)
            self.assertIn("1 failed", (rerun_naive.stdout or "")
                          + (rerun_naive.stderr or ""))
            rerun_fixed = run_guard(fixed_guard)
            self.assertEqual(rerun_fixed.returncode, 0,
                             rerun_fixed.stdout + rerun_fixed.stderr)
            self.assertIn("1 passed", (rerun_fixed.stdout or "")
                          + (rerun_fixed.stderr or ""))

    def test_controller_source_names_no_booking_tools(self):
        text = CONTROLLER.read_text(encoding="utf-8")
        self.assertNotIn("book_equipment", text)
        self.assertNotIn("get_booking_status", text)


if __name__ == "__main__":
    unittest.main()
