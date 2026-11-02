"""Track B focused tests: registry, gateway, qualify. Stdlib unittest."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from ghostlab.targets.gateway import (  # noqa: E402
    ControlledGateway,
    GatewayBudgetExceeded,
    GatewayCancelled,
    GatewayNotAllowed,
)
from ghostlab.targets.qualify import qualify_agent  # noqa: E402
from ghostlab.targets.registry import AgentManifest, TargetRegistry  # noqa: E402


class _FakeWorld:
    def __init__(self):
        self.state = {"reservations": [], "total_bookings": 0}
        self.calls: list[tuple[str, dict]] = []

    def snapshot(self):
        return {"reservations": list(self.state["reservations"]), "total_bookings": 0}

    def call(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        if tool == "read_only":
            return {"ok": True}
        if tool == "mutate":
            self.state["reservations"].append({"x": 1})
            return {"ok": True}
        raise ValueError(f"Unknown tool: {tool}")


def _manifest(name="demo-agent", **overrides):
    base = {
        "name": name,
        "version": "0.1.0",
        "config": {"retries": 1},
        "allowed_tools": ["read_only", "mutate"],
        "entry": "demo:entry",
        "description": "track b test agent",
    }
    base.update(overrides)
    return AgentManifest(
        name=base["name"],
        version=base["version"],
        config=dict(base["config"]),
        allowed_tools=tuple(base["allowed_tools"]),
        entry=base["entry"],
        description=base["description"],
    )


class RegistryGatewayTests(unittest.TestCase):
    def test_register_captures_version_and_config_snapshot(self):
        registry = TargetRegistry()
        config = {"retries": 1, "nested": {"a": 1}}
        record = registry.register(_manifest(config=config))
        config["retries"] = 99
        config["nested"]["a"] = 99
        stored = registry.get(record.name)
        assert stored is not None
        self.assertEqual(stored.version, "0.1.0")
        self.assertEqual(stored.config, {"retries": 1, "nested": {"a": 1}})
        stored.config["retries"] = 5
        self.assertEqual(registry.get(record.name).config["retries"], 1)

    def test_duplicate_registration_rejected(self):
        registry = TargetRegistry()
        registry.register(_manifest(name="dup"))
        with self.assertRaises(ValueError):
            registry.register(_manifest(name="dup"))

    def test_import_manifest_from_dict_and_file(self):
        with tempfile.TemporaryDirectory(prefix="ghostlab-trackb-") as tmp:
            db = Path(tmp) / "registry.db"
            registry = TargetRegistry(db_path=db)
            record = registry.import_manifest(
                {
                    "name": "imported-agent",
                    "version": "1.2.3",
                    "config": {"mode": "naive"},
                    "allowed_tools": ["read_only"],
                    "entry": "pkg:fn",
                }
            )
            self.assertEqual(record.source, "imported")
            manifest_path = Path(tmp) / "agent.json"
            manifest_path.write_text(
                '{"name": "file-agent", "version": "2.0.0",'
                ' "config": {}, "allowed_tools": ["read_only"]}',
                encoding="utf-8",
            )
            file_record = registry.import_manifest(manifest_path)
            self.assertEqual(file_record.source, "imported")
            # Persistence round-trip through the track-private temp DB.
            reopened = TargetRegistry(db_path=db)
            self.assertIsNotNone(reopened.get("imported-agent"))
            self.assertIsNotNone(reopened.get("file-agent"))

    def test_gateway_allowlist_budget_cancel_and_records(self):
        registry = TargetRegistry()
        registry.register(_manifest(allowed_tools=["read_only"]))
        gateway = ControlledGateway(registry, default_max_tool_calls=2)
        world = _FakeWorld()
        run_id = gateway.create_run("demo-agent")
        gateway.dispatch(run_id, world, "read_only", {})
        with self.assertRaises(GatewayNotAllowed):
            gateway.dispatch(run_id, world, "mutate", {})
        gateway.dispatch(run_id, world, "read_only", {})
        with self.assertRaises(GatewayBudgetExceeded):
            gateway.dispatch(run_id, world, "read_only", {})
        records = gateway.records_for_run(run_id)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].agent_version, "0.1.0")
        self.assertEqual(records[0].tool, "read_only")
        # Cancellation blocks further dispatch.
        run2 = gateway.create_run("demo-agent")
        gateway.cancel(run2)
        with self.assertRaises(GatewayCancelled):
            gateway.dispatch(run2, world, "read_only", {})
        self.assertEqual(len(gateway.action_records), 2)

    def test_gateway_error_outcomes_are_observable(self):
        registry = TargetRegistry()
        registry.register(_manifest(allowed_tools=["read_only", "boom"]))
        gateway = ControlledGateway(registry)

        class BoomWorld(_FakeWorld):
            def call(self, tool, arguments):
                if tool == "boom":
                    raise ValueError("synthetic world failure")
                return super().call(tool, arguments)

        run_id = gateway.create_run("demo-agent")
        with self.assertRaises(ValueError):
            gateway.dispatch(run_id, BoomWorld(), "boom", {})
        records = gateway.records_for_run(run_id)
        self.assertEqual(records[0].outcome["error"], "ValueError")

    def test_qualify_probe_must_be_non_mutating(self):
        registry = TargetRegistry()
        registry.register(_manifest())
        report = qualify_agent(
            registry,
            "demo-agent",
            world_factory=_FakeWorld,
            read_only_probe=("read_only", {}),
            expected_tools={"read_only", "mutate"},
        )
        self.assertTrue(report.passed, report)
        bad = qualify_agent(
            registry,
            "demo-agent",
            world_factory=_FakeWorld,
            read_only_probe=("mutate", {}),
            expected_tools={"read_only", "mutate"},
        )
        self.assertFalse(bad.passed)
        self.assertFalse(bad.checks["probe_non_mutating"])


if __name__ == "__main__":
    unittest.main()
