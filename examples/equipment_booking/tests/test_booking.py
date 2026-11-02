"""Track B equipment-booking tests: fault, duplicate, repair, walkthrough."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from examples.equipment_booking.agents import run_corrected, run_naive  # noqa: E402
from examples.equipment_booking.checks import (  # noqa: E402
    has_duplicate,
    verify_equipment,
)
from examples.equipment_booking.world import (  # noqa: E402
    BookingTimeout,
    EquipmentFixture,
    EquipmentScenario,
    EquipmentWorld,
    ToolError,
)
from ghostlab.targets.gateway import ControlledGateway  # noqa: E402
from ghostlab.targets.qualify import qualify_agent  # noqa: E402
from ghostlab.targets.registry import TargetRegistry  # noqa: E402

TOOLS = ["get_equipment", "get_booking_status", "book_equipment"]


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghostlab-trackb-"))
        self.registry = TargetRegistry(db_path=self.tmp / "registry.db")
        for name in ("booking-naive", "booking-corrected"):
            self.registry.register(
                {
                    "name": name,
                    "version": "0.1.0",
                    "config": {},
                    "allowed_tools": TOOLS,
                    "entry": f"examples.equipment_booking.agents:{name}",
                }
            )
        self.fixture = EquipmentFixture()
        self.gateway = ControlledGateway(self.registry)

    def _run(self, agent, scenario, entry):
        world = EquipmentWorld(self.fixture, scenario)
        run_id = self.gateway.create_run(agent)
        state = entry(self.gateway, run_id, world, self.fixture)
        return world, state

    def test_commit_then_timeout_and_reset(self):
        world = EquipmentWorld(self.fixture, EquipmentScenario(timeout_after_commit=True))
        request = {
            "equipment_id": self.fixture.equipment_id,
            "user_id": self.fixture.user_id,
            "slot": self.fixture.slot,
            "idempotency_key": "k1",
        }
        with self.assertRaises(BookingTimeout):
            world.call("book_equipment", request)
        self.assertEqual(world.snapshot()["total_bookings"], 1)
        world.reset()
        self.assertEqual(world.snapshot()["total_bookings"], 0)
        self.assertEqual(world.trace, [])

    def test_naive_retry_duplicates_corrected_does_not(self):
        scenario = EquipmentScenario(timeout_after_commit=True)
        _, naive_state = self._run("booking-naive", scenario, run_naive)
        self.assertEqual(naive_state["total_bookings"], 2)
        self.assertTrue(has_duplicate(self.fixture, naive_state))
        self.assertFalse(verify_equipment(self.fixture, naive_state)["no_duplicate_booking"])
        _, fixed_state = self._run("booking-corrected", scenario, run_corrected)
        self.assertEqual(fixed_state["total_bookings"], 1)
        self.assertFalse(has_duplicate(self.fixture, fixed_state))
        self.assertTrue(all(verify_equipment(self.fixture, fixed_state).values()))

    def test_unauthorized_and_unknown_tools_rejected(self):
        world = EquipmentWorld(self.fixture, EquipmentScenario(), actor_id="intruder")
        with self.assertRaises(ToolError):
            world.call(
                "book_equipment",
                {
                    "equipment_id": self.fixture.equipment_id,
                    "user_id": self.fixture.user_id,
                    "slot": self.fixture.slot,
                    "idempotency_key": "k1",
                },
            )
        self.assertEqual(world.snapshot()["total_bookings"], 0)
        world2 = EquipmentWorld(self.fixture, EquipmentScenario())
        with self.assertRaises(ToolError):
            world2.call("shell", {})

    def test_qualify_register_import_flow(self):
        manifest_path = self.tmp / "extra.json"
        manifest_path.write_text(
            '{"name": "extra-agent", "version": "0.2.0",'
            ' "config": {"a": 1}, "allowed_tools": ["get_equipment"]}',
            encoding="utf-8",
        )
        record = self.registry.import_manifest(manifest_path)
        self.assertEqual(record.source, "imported")
        self.assertEqual(record.version, "0.2.0")
        report = qualify_agent(
            self.registry,
            "booking-naive",
            world_factory=lambda: EquipmentWorld(self.fixture, EquipmentScenario()),
            read_only_probe=(
                "get_equipment",
                {"equipment_id": self.fixture.equipment_id, "user_id": self.fixture.user_id},
            ),
            expected_tools=set(TOOLS),
        )
        self.assertTrue(report.passed, report)

    def test_walkthrough_executable(self):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "examples/equipment_booking/walkthrough.py")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn('"duplicate_detected": true', proc.stdout)
        self.assertIn('"retest_bookings": 1', proc.stdout)


if __name__ == "__main__":
    unittest.main()
