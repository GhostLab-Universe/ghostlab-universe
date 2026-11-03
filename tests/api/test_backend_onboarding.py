"""Fresh-root builtin seeding: Prepare run must find refund+reservation (Blocker B)."""

from ghostlab.extensions.operator_registry import STATUS_NOT_QUALIFIED, OperatorRegistry

import pytest

REFUND = "refund-world/1"
RESERVATION = "reservation-world/1"


def refund_config_body():
    return {"config_version": 1, "name": "refund-onboarding",
            "world_version": REFUND,
            "target": {"kind": "script",
                       "actions": [{"tool": "get_order",
                                    "arguments": {"order_id": "synthetic-order-1",
                                                  "customer_id": "synthetic-customer-1"}}]},
            "fixture": {"name": "synthetic-inr-2500"}, "faults": [],
            "rules": ["authorized_refunds"],
            "limits": {"max_tool_calls": 10, "max_total_tool_calls": 20,
                       "max_wall_clock_ms": 5000},
            "repetitions": {"count": 1}}


def _registry_for(api):
    root = api.lab.paths.graph_db.parent
    return OperatorRegistry(root / "operator_registry.sqlite")


def test_fresh_root_seeded_with_builtins(stack):
    api = stack()
    operators = _registry_for(api)
    worlds = {env["world_version"] for env in operators.list_envs()}
    assert REFUND in worlds, worlds
    assert RESERVATION in worlds, worlds
    for world, ref in ((REFUND, "ghostlab.adapters.refund_env:RefundEnvironment"),
                       (RESERVATION,
                        "ghostlab.adapters.reservation_env:ReservationEnvironment")):
        record = operators.get(world)
        assert record["adapter_ref"] == ref, record
        assert record["registered_by"] == "builtin", record
        assert record["status"] == STATUS_NOT_QUALIFIED, record
    # Declared tools come from each adapter's describe().
    refund_tools = {t["name"] for t in operators.get(REFUND)["declared_tools"]}
    assert refund_tools == {"get_order", "issue_refund", "get_refund_status"}, refund_tools
    reservation_tools = {
        t["name"] for t in operators.get(RESERVATION)["declared_tools"]}
    assert reservation_tools == {
        "check_availability", "reserve", "cancel", "get_status"}, reservation_tools


def test_unseeded_readonly_registry_fails_with_named_error(stack, tmp_path):
    import os

    from ghostlab.api.app import seed_builtin_registrations

    path = tmp_path / "operator_registry.sqlite"
    seed_builtin_registrations(OperatorRegistry(path))  # creates the file
    import sqlite3

    connection = sqlite3.connect(str(path))
    connection.execute("DELETE FROM registrations")
    connection.commit()
    connection.close()
    os.chmod(path, 0o444)
    try:
        operators = OperatorRegistry(path)
        with pytest.raises(RuntimeError, match="not writable"):
            seed_builtin_registrations(operators)
    finally:
        os.chmod(path, 0o644)


def test_second_startup_idempotent(stack):
    api = stack()
    operators = _registry_for(api)
    before = operators.list_envs()
    from ghostlab.api.app import seed_builtin_registrations

    seed_builtin_registrations(operators)
    seed_builtin_registrations(operators)
    assert operators.list_envs() == before


def test_refund_experiment_resolves_on_fresh_root(stack):
    api = stack()
    created = api.post("/v1/extensions/exp-configs",
                       json={"config": refund_config_body()})
    assert created.status_code == 200, created.text
    config_id = created.json()["config_id"]
    resolved = api.post(f"/v1/extensions/exp-configs/{config_id}/resolve", json={})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved"]["pins"]["world_version"] == REFUND
