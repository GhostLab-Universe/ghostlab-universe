"""Executable walkthrough: register->import->qualify->run->duplicate->retest."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from examples.equipment_booking.agents import run_corrected, run_naive  # noqa: E402
from examples.equipment_booking.checks import (  # noqa: E402
    duplicate_count,
    has_duplicate,
    verify_equipment,
)
from examples.equipment_booking.world import (  # noqa: E402
    EquipmentFixture,
    EquipmentScenario,
    EquipmentWorld,
)
from ghostlab.targets.gateway import ControlledGateway  # noqa: E402
from ghostlab.targets.qualify import qualify_agent  # noqa: E402
from ghostlab.targets.registry import TargetRegistry  # noqa: E402

TOOLS = ["get_equipment", "get_booking_status", "book_equipment"]


def trackb_tempdir(prefix="ghostlab-trackb-") -> Path:
    """Prefer /tmp when writable (task asks /tmp); else platform temp dir."""
    try:
        candidate = Path("/tmp")
        if candidate.is_dir():
            probe = candidate / f"{prefix}probe"
            probe.mkdir(exist_ok=True)
            probe.rmdir()
            return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    except OSError:
        pass
    return Path(tempfile.mkdtemp(prefix=prefix))


def main() -> int:
    tmp = trackb_tempdir()
    db_path = tmp / "trackb-registry.db"
    registry = TargetRegistry(db_path=db_path)
    fixture = EquipmentFixture()

    # 1. register: developer-owned agents with version+config capture.
    naive = registry.register(
        {
            "name": "booking-naive",
            "version": "0.1.0",
            "config": {"retry": "fresh-key", "slot": fixture.slot},
            "allowed_tools": TOOLS,
            "entry": "examples.equipment_booking.agents:run_naive",
            "description": "Vulnerable fresh-key retry demo",
        }
    )
    corrected = registry.register(
        {
            "name": "booking-corrected",
            "version": "0.1.0",
            "config": {"retry": "stable-key", "slot": fixture.slot},
            "allowed_tools": TOOLS,
            "entry": "examples.equipment_booking.agents:run_corrected",
            "description": "Corrected stable-key retry demo",
        }
    )

    # 2. import: JSON manifest round-trip for a third agent name.
    manifest = registry.export_manifest("booking-corrected")
    manifest["name"] = "booking-corrected-imported"
    manifest_path = tmp / "booking-corrected.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    imported = registry.import_manifest(manifest_path)

    # 3. qualify: non-mutating probe through the gateway.
    def world_factory():
        return EquipmentWorld(fixture, EquipmentScenario())

    probe = (("get_equipment", {"equipment_id": fixture.equipment_id, "user_id": fixture.user_id}),)
    qualify = {
        name: qualify_agent(
            registry,
            name,
            world_factory=world_factory,
            read_only_probe=probe[0],
            expected_tools=set(TOOLS),
        )
        for name in ("booking-naive", "booking-corrected", "booking-corrected-imported")
    }

    # 4. run: baseline + faulted naive + retest corrected, each fresh + gated.
    gateway = ControlledGateway(registry)
    baseline_world = EquipmentWorld(fixture, EquipmentScenario())
    baseline_run = gateway.create_run("booking-naive")
    baseline_state = run_naive(gateway, baseline_run, baseline_world, fixture)

    faulted_world = EquipmentWorld(fixture, EquipmentScenario(timeout_after_commit=True))
    faulted_run = gateway.create_run("booking-naive")
    faulted_state = run_naive(gateway, faulted_run, faulted_world, fixture)

    # 5. duplicate: independent check over controller-owned state.
    faulted_checks = verify_equipment(fixture, faulted_state)
    duplicated = has_duplicate(fixture, faulted_state)

    # 6. retest: corrected stable-key agent under the identical fault.
    retest_world = EquipmentWorld(fixture, EquipmentScenario(timeout_after_commit=True))
    retest_run = gateway.create_run("booking-corrected")
    retest_state = run_corrected(gateway, retest_run, retest_world, fixture)
    retest_checks = verify_equipment(fixture, retest_state)

    # Resettable-state proof: reset() returns the faulted world to zero.
    faulted_world.reset()
    reset_ok = faulted_world.snapshot()["total_bookings"] == 0 and faulted_world.trace == []

    summary = {
        "tmpdir": str(tmp),
        "db": str(db_path),
        "registered": [naive.name, corrected.name],
        "imported": imported.name,
        "import_source": imported.source,
        "qualified": {name: report.passed for name, report in qualify.items()},
        "baseline_bookings": baseline_state["total_bookings"],
        "baseline_checks": verify_equipment(fixture, baseline_state),
        "faulted_bookings": faulted_state["total_bookings"],
        "faulted_checks": faulted_checks,
        "duplicate_count": duplicate_count(fixture, faulted_state),
        "duplicate_detected": duplicated,
        "retest_bookings": retest_state["total_bookings"],
        "retest_checks": retest_checks,
        "reset_ok": reset_ok,
        "gateway_records": len(gateway.action_records),
        "model_calls": 0,
    }
    print(json.dumps(summary, indent=2))

    ok = (
        all(summary["qualified"].values())
        and summary["baseline_bookings"] == 1
        and all(summary["baseline_checks"].values())
        and summary["faulted_bookings"] == 2
        and summary["duplicate_detected"]
        and not summary["faulted_checks"]["no_duplicate_booking"]
        and summary["retest_bookings"] == 1
        and all(summary["retest_checks"].values())
        and reset_ok
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
