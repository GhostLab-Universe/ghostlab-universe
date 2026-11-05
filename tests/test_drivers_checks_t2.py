"""T2: domain-independent execution via drivers + code-only checks.

Proves the refund driver moved without behavior change (byte-identical
traces to ``ghostlab.core``), the booking driver executes through
``execute_run`` with its own evidence/fault/verify shape, the check registry
is code-only with param bounds, and resolution rejects unsupported checks.

Every test uses track-private temporary databases; no live registry file is
touched. Zero model calls, no secrets.
"""

import contextlib
import gc
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.equipment_booking import driver_booking  # noqa: E402
from examples.equipment_booking.env_adapter import (  # noqa: E402
    BOOKING_MODES, BOOKING_TOOLS)
from examples.equipment_booking.world import (  # noqa: E402
    EquipmentFixture, EquipmentScenario, EquipmentWorld)
from ghostlab.core import Fixture, RefundWorld, Scenario  # noqa: E402
from ghostlab.core import run as core_run
from ghostlab.core import verify as core_verify
from ghostlab.envs.adapters import (  # noqa: E402
    REFUND_MODES, REFUND_TOOLS, RefundEnvAdapter)
from ghostlab.envs.qualification import run_qualification  # noqa: E402
from ghostlab.envs.registry import EnvRegistry, utcnow  # noqa: E402
from ghostlab.experiments import checks, drivers, ops  # noqa: E402
from ghostlab.experiments import configs as configs_module  # noqa: E402
from ghostlab.experiments import drivers_refund  # noqa: E402
from ghostlab.experiments.checks import CheckError  # noqa: E402
from ghostlab.experiments.configs import (  # noqa: E402
    CONTROLLER_VERSION, EXPERIMENTS_SCHEMA_VERSION, ConfigError,
    FixtureSpec, LimitSpec, Policy, RepetitionSpec, RequestedConfig,
    canonical_digest, resolve_requested)
from ghostlab.experiments.controller import (  # noqa: E402
    drive_agent, evidence_complete, fault_fired)
from ghostlab.experiments.store import ExperimentStore  # noqa: E402

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
BOOKING_REF = ("examples.equipment_booking.env_adapter:BookingEnvAdapter")
BOOKING_FIXTURE = {"equipment_id": "synthetic-microscope-1",
                   "user_id": "synthetic-user-1",
                   "slot": "2026-11-03T09:00Z"}
REFUND_FIXTURE = {"order_id": "synthetic-order-1",
                  "customer_id": "synthetic-customer-1",
                  "paid_minor": 250000, "currency": "INR"}

REFUND_IDS = ("authorized_refunds", "total_refund_within_payment",
              "no_duplicate_full_refund", "unique_idempotency_keys")
BOOKING_IDS = ("authorized_bookings", "no_duplicate_booking",
               "unique_idempotency_keys")


class CustomRefundAdapter(RefundEnvAdapter):
    """Refund adapter describing itself under a custom (unregistered) id."""

    env_id = "refund-custom"
    version = "refund-custom/1"


def setUpModule():
    # Injected registration: driver_booking keeps relative-only imports,
    # so the importer registers its driver + checks exactly once.
    if drivers.get_driver(driver_booking.ENV_ID) is None:
        drivers.register_driver(driver_booking.ENV_ID, driver_booking)
    for spec in driver_booking.BOOKING_CHECK_SPECS:
        try:
            checks.get_check(spec["check_id"])
        except CheckError:
            checks.register_check(**spec)


@contextlib.contextmanager
def temp_dir():
    base = "/tmp" if os.path.isdir("/tmp") else None
    with tempfile.TemporaryDirectory(prefix="t2-", dir=base) as directory:
        yield directory
        gc.collect()


def make_registry(directory, *prefixes):
    return EnvRegistry(Path(directory) / "env.sqlite",
                       allowed_adapter_prefixes=prefixes)


def register_refund(registry, env_id="refund-env"):
    record = registry.register(
        env_id=env_id, env_version="refund-env/1",
        adapter_ref=REFUND_REF, display_name="Synthetic refund",
        declared_tools=deepcopy(REFUND_TOOLS),
        declared_modes=list(REFUND_MODES))
    run_qualification(registry, env_id)
    return record


def register_booking(registry, env_id="booking-env"):
    record = registry.register(
        env_id=env_id, env_version="booking-env/1",
        adapter_ref=BOOKING_REF, display_name="Synthetic booking",
        declared_tools=deepcopy(BOOKING_TOOLS),
        declared_modes=list(BOOKING_MODES))
    report = run_qualification(registry, env_id,
                               fixture=dict(BOOKING_FIXTURE))
    assert report["status"] == "qualified-for-declared", report
    return record


def valid_policy(**overrides):
    policy = {"policy_version": "policy/1", "ceiling_tool_calls": 20,
              "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
              "ceiling_wall_clock_ms": 60000,
              "allowed_agents": ["vulnerable", "repaired"],
              "allowed_hooks": ["timeout_after_commit"]}
    policy.update(overrides)
    return policy


def valid_requested(**overrides):
    obj = {"config_version": 1, "name": "t2 refund probe",
           "env_id": "refund-env", "agent": "vulnerable",
           "fixture": dict(REFUND_FIXTURE), "faults": [],
           "rules": list(REFUND_IDS),
           "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                      "max_wall_clock_ms": 60000},
           "repetitions": {"count": 1}}
    obj.update(overrides)
    return obj


def craft_booking_resolved(store, registration, agent, resolved_id,
                           project="proj-t2"):
    """Hand-crafted booking resolved record (bypasses refund-shaped
    requested validation, which is out of scope for the T2 minimal edit)."""
    body = {
        "schema_version": EXPERIMENTS_SCHEMA_VERSION,
        "resolved_config_id": resolved_id, "project_id": project,
        "requested_config_id": "req-t2-booking",
        "requested_digest": "0" * 64, "resolved_at": utcnow(),
        "resolved_by": "t2-test",
        "pins": {"env_id": registration["env_id"],
                 "env_version": registration["env_version"],
                 "adapter_ref": registration["adapter_ref"],
                 "code_digest": registration["code_digest"],
                 "manifest_digest": None,
                 "world_version": "equipment-booking-world/1",
                 "adapter_version": "booking-adapters/1",
                 "controller_version": CONTROLLER_VERSION,
                 "discovered_tools": deepcopy(BOOKING_TOOLS),
                 "discovered_modes": list(BOOKING_MODES)},
        "effective": {
            "agent": agent, "fixture": dict(BOOKING_FIXTURE),
            "faults": [{"hook": "timeout_after_commit", "params": {},
                        "required": True}],
            "rules": list(BOOKING_IDS),
            "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                       "max_wall_clock_ms": 60000},
            "repetitions": {"count": 1}, "seeds": [0]},
        "provenance": {"policy_version": "policy/1",
                       "requested_by": "t2-test",
                       "requested_digest": "0" * 64,
                       "resolved_by": "t2-test",
                       "discovery_match": True},
    }
    digest = canonical_digest({k: v for k, v in body.items()
                               if k != "digest"})
    body["digest"] = digest
    return store.save_resolved(project_id=project,
                               requested_id="req-t2-booking", body=body,
                               digest=digest, created_at=body["resolved_at"])


def direct_requested(env_id, rules, agent="vulnerable"):
    """RequestedConfig built directly to exercise resolve-time rejection
    (creation-time validation would already reject unknown rules)."""
    return RequestedConfig(
        name="t2 direct", env_id=env_id, agent=agent,
        fixture=FixtureSpec(values=(
            ("currency", "INR"),
            ("customer_id", "synthetic-customer-1"),
            ("order_id", "synthetic-order-1"),
            ("paid_minor", 250000))),
        faults=tuple(), rules=tuple(rules),
        limits=LimitSpec(20, 200, 60000), repetitions=RepetitionSpec(1))


class DriverRegistryTests(unittest.TestCase):
    def test_driver_surface_and_lookup(self):
        refund = drivers.get_driver("refund-env")
        self.assertIs(refund, drivers_refund)
        booking = drivers.get_driver("booking-env")
        self.assertIs(booking, driver_booking)
        self.assertIsNone(drivers.get_driver("no-such-env"))
        for driver in (refund, booking):
            for name in ("drive", "evidence_complete", "fault_fired",
                         "verify_checks", "declared_checks", "agents"):
                self.assertTrue(callable(getattr(driver, name)), name)
        self.assertEqual(refund.declared_checks(), list(REFUND_IDS))
        self.assertEqual(booking.declared_checks(), list(BOOKING_IDS))
        self.assertEqual(refund.agents(), ["vulnerable", "repaired", "llm-naive",
                                          "llm-hardened", "llm-naive-noread",
                                          "llm-hardened-noread", "llm-naive-wonder",
                                          "llm-naive-wonder-noread", "status-ignored",
                                          "retry-nokey", "split-key-per-attempt"])
        self.assertEqual(booking.agents(), ["vulnerable", "repaired",
                                           "status-ignored", "double-submit"])

    def test_driver_registration_rejects_bad_surface(self):
        with self.assertRaises(ValueError):
            drivers.register_driver("t2-bad-surface", object())
        with self.assertRaises(ValueError):
            drivers.register_driver("Not Valid!", drivers_refund)
        with self.assertRaisesRegex(ValueError, "already registered"):
            drivers.register_driver("refund-env", drivers_refund)


class RefundCheckTests(unittest.TestCase):
    def test_all_four_evaluate_against_core(self):
        fixture = Fixture()
        faulted = core_run(fixture, Scenario(timeout_after_commit=True),
                           "vulnerable")
        repaired = core_run(fixture, Scenario(timeout_after_commit=True),
                            "repaired")
        for check_id in REFUND_IDS:
            with self.subTest(check=check_id):
                bad = checks.evaluate(
                    check_id, snapshot=faulted["final_state"],
                    trace=faulted["trace"], params={}, fixture=fixture)
                good = checks.evaluate(
                    check_id, snapshot=repaired["final_state"],
                    trace=repaired["trace"], params={}, fixture=fixture)
                expected_bad = core_verify(
                    fixture, faulted["final_state"])[check_id]
                self.assertEqual(bad["verdict"],
                                 "pass" if expected_bad else "violation")
                self.assertEqual(good["verdict"], "pass")
                self.assertIs(type(bad["detail"]), str)
                self.assertIs(type(bad["evidence_refs"]), list)
        self.assertEqual(faulted["checks"]["total_refund_within_payment"],
                         False)
        self.assertEqual(faulted["checks"]["no_duplicate_full_refund"],
                         False)

    def test_unknown_on_missing_evidence_never_passes(self):
        for check_id in REFUND_IDS:
            with self.subTest(check=check_id):
                result = checks.evaluate(check_id, snapshot=None,
                                         trace=None, params={},
                                         fixture=Fixture())
                self.assertEqual(result["verdict"], "unknown")
                self.assertNotEqual(result["verdict"], "pass")
                self.assertEqual(result["evidence_refs"], [])

    def test_unknown_check_id_rejected(self):
        with self.assertRaises(CheckError):
            checks.evaluate("no_such_check", snapshot={}, trace=[],
                            params={}, fixture=Fixture())

    def test_check_versions_pinned_in_registry(self):
        for check_id in REFUND_IDS:
            self.assertEqual(checks.get_check(check_id)["version"], "1")


class BookingCheckTests(unittest.TestCase):
    def test_drive_sequences_match_agents_py(self):
        from examples.equipment_booking.world import BookingTimeout
        calls = []
        books = []

        def invoke(tool, arguments):
            calls.append((tool, dict(arguments)))
            if tool == "get_equipment":
                return dict(arguments)
            if tool == "book_equipment":
                books.append(dict(arguments))
                if len(books) == 1:
                    raise BookingTimeout("committed; timed out")
                return {"ok": True}
            if tool == "get_booking_status":
                return {"status": "committed", "booking": {}}
            raise AssertionError(tool)

        fx = dict(BOOKING_FIXTURE)
        driver_booking.drive("vulnerable", invoke, fx)
        self.assertEqual([c[0] for c in calls],
                         ["get_equipment", "book_equipment",
                          "book_equipment"])
        self.assertEqual(calls[1][1]["idempotency_key"],
                         "booking-intent-1")
        self.assertEqual(calls[2][1]["idempotency_key"], "booking-retry-2")
        self.assertEqual(calls[1][1]["slot"], fx["slot"])

        calls.clear()
        books.clear()
        driver_booking.drive("repaired", invoke, fx)
        # Timeout, then status shows committed: no rebook.
        self.assertEqual([c[0] for c in calls],
                         ["get_equipment", "book_equipment",
                          "get_booking_status"])
        self.assertEqual(calls[2][1]["idempotency_key"],
                         "booking-intent-1")

    def test_all_three_evaluate_with_duplicate_violation(self):
        from examples.equipment_booking.world import BookingTimeout
        fixture = EquipmentFixture()
        fx = {"equipment_id": fixture.equipment_id,
              "user_id": fixture.user_id, "slot": fixture.slot}

        def run_world(agent):
            world = EquipmentWorld(
                fixture, EquipmentScenario(timeout_after_commit=True))
            driver_booking.drive(agent, world.call, dict(fx))
            return world

        naive = run_world("vulnerable")
        self.assertEqual(len(naive.snapshot()["reservations"]), 2)
        corrected = run_world("repaired")
        self.assertEqual(len(corrected.snapshot()["reservations"]), 1)

        results = {c: checks.evaluate(
            c, snapshot=naive.snapshot(), trace=naive.trace, params={},
            fixture=fixture) for c in BOOKING_IDS}
        self.assertEqual(results["authorized_bookings"]["verdict"], "pass")
        self.assertEqual(results["no_duplicate_booking"]["verdict"],
                         "violation")
        self.assertEqual(results["unique_idempotency_keys"]["verdict"],
                         "pass")
        for check_id in BOOKING_IDS:
            good = checks.evaluate(
                check_id, snapshot=corrected.snapshot(),
                trace=corrected.trace, params={}, fixture=fixture)
            self.assertEqual(good["verdict"], "pass", check_id)
            unknown = checks.evaluate(check_id, snapshot=None,
                                      trace=None, params={},
                                      fixture=fixture)
            self.assertEqual(unknown["verdict"], "unknown", check_id)
        self.assertTrue(issubclass(BookingTimeout, Exception))

    def test_booking_fault_and_evidence_shape(self):
        fixture = EquipmentFixture()
        fx = {"equipment_id": fixture.equipment_id,
              "user_id": fixture.user_id, "slot": fixture.slot}
        world = EquipmentWorld(
            fixture, EquipmentScenario(timeout_after_commit=True))
        driver_booking.drive("vulnerable", world.call, dict(fx))
        evidence = {"snapshot": world.snapshot(),
                    "trace": list(world.trace)}
        self.assertTrue(driver_booking.evidence_complete(evidence, 3))
        self.assertFalse(driver_booking.evidence_complete(evidence, 2))
        self.assertTrue(driver_booking.fault_fired(
            "timeout_after_commit", evidence["trace"]))
        self.assertFalse(driver_booking.fault_fired("nope",
                                                    evidence["trace"]))
        # Refund-shaped evidence is incomplete for booking and vice versa.
        refund_world = RefundWorld(
            Fixture(), Scenario(timeout_after_commit=True))
        drivers_refund.drive("vulnerable", refund_world.call,
                             dict(REFUND_FIXTURE))
        refund_evidence = {"snapshot": refund_world.snapshot(),
                           "trace": list(refund_world.trace)}
        self.assertFalse(driver_booking.evidence_complete(
            refund_evidence, len(refund_world.trace)))
        self.assertFalse(drivers_refund.evidence_complete(
            evidence, len(world.trace)))


class BookingExecuteTests(unittest.TestCase):
    def test_vulnerable_duplicates_and_fails_with_fired_timeout(self):
        with temp_dir() as directory:
            registry = make_registry(
                directory, "ghostlab.", "examples.equipment_booking.")
            record = register_booking(registry)
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            resolved = craft_booking_resolved(store, record, "vulnerable",
                                              "res-t2-booking-vuln")
            run = ops.start_run(store, registry, "proj-t2",
                                resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["tool_calls"], 3)
            self.assertEqual(body["checks"],
                             {"authorized_bookings": True,
                              "no_duplicate_booking": False,
                              "unique_idempotency_keys": True})
            self.assertTrue(all(type(v) is bool
                                for v in body["checks"].values()))
            self.assertEqual(body["rules_failed"],
                             ["no_duplicate_booking"])
            self.assertEqual(len(body["faults"]), 1)
            self.assertTrue(body["faults"][0]["required"])
            self.assertIs(body["faults"][0]["fired"], True)
            self.assertEqual(body["verdict"], "fail")
            self.assertEqual(body["model_calls"], 0)
            snapshot = body["evidence"]["snapshot"]
            self.assertEqual(snapshot["total_bookings"], 2)
            self.assertNotIn("checks_meta", body)

    def test_corrected_passes(self):
        with temp_dir() as directory:
            registry = make_registry(
                directory, "ghostlab.", "examples.equipment_booking.")
            record = register_booking(registry)
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            resolved = craft_booking_resolved(store, record, "repaired",
                                              "res-t2-booking-fixed")
            run = ops.start_run(store, registry, "proj-t2",
                                resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["tool_calls"], 3)
            self.assertEqual(body["checks"],
                             {"authorized_bookings": True,
                              "no_duplicate_booking": True,
                              "unique_idempotency_keys": True})
            self.assertEqual(body["rules_failed"], [])
            self.assertIs(body["faults"][0]["fired"], True)
            self.assertEqual(body["verdict"], "pass")

    def test_unknown_env_driver_refused(self):
        with temp_dir() as directory:
            registry = make_registry(
                directory, "ghostlab.", "examples.equipment_booking.")
            record = register_booking(registry, env_id="booking-nodriver")
            # Booking driver is registered for booking-env only; an
            # unregistered non-refund env has no driver to dispatch to.
            self.assertIsNone(drivers.get_driver("booking-nodriver"))
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            resolved = craft_booking_resolved(store, record, "vulnerable",
                                              "res-t2-booking-nodriver")
            with self.assertRaisesRegex(ConfigError, "no driver"):
                ops.start_run(store, registry, "proj-t2",
                              resolved["resolved_id"])


class RefundParityTests(unittest.TestCase):
    def test_traces_byte_identical_to_core(self):
        cases = [("vulnerable", True), ("repaired", True),
                 ("vulnerable", False), ("repaired", False)]
        with temp_dir() as directory:
            registry = make_registry(directory, "ghostlab.")
            register_refund(registry)
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            for agent, faulted in cases:
                with self.subTest(agent=agent, faulted=faulted):
                    faults = [{"hook": "timeout_after_commit",
                               "params": {}, "required": True}] \
                        if faulted else []
                    created = ops.create_requested_config(
                        store, "proj-t2",
                        valid_requested(agent=agent, faults=faults),
                        valid_policy(), "t2-operator")
                    resolved = ops.resolve_config(
                        store, registry, "proj-t2", created["config_id"],
                        valid_policy(), "t2-operator")
                    run = ops.start_run(store, registry, "proj-t2",
                                        resolved["resolved_id"],
                                        attempt=cases.index((agent,
                                                             faulted)))
                    expected = core_run(
                        Fixture(**REFUND_FIXTURE),
                        Scenario(timeout_after_commit=faulted), agent)
                    blob = json.dumps(run["run"]["evidence"]["trace"],
                                      sort_keys=True)
                    want = json.dumps(expected["trace"], sort_keys=True)
                    self.assertEqual(blob, want)
                    self.assertEqual(run["run"]["evidence"]["snapshot"],
                                     expected["final_state"])
                    self.assertEqual(run["run"]["checks"],
                                     expected["checks"])

    def test_legacy_delegates_preserved(self):
        fixture = Fixture()
        world = RefundWorld(
            fixture, Scenario(timeout_after_commit=True))
        drive_agent(agent="vulnerable", invoke=world.call,
                    identity={"order_id": fixture.order_id,
                              "customer_id": fixture.customer_id},
                    paid_minor=fixture.paid_minor)
        self.assertEqual(len(world.ledger), 2)
        with self.assertRaises(ConfigError):
            drive_agent(agent="oracle", invoke=world.call,
                        identity={"order_id": "o", "customer_id": "c"},
                        paid_minor=1)
        self.assertTrue(fault_fired("timeout_after_commit", world.trace))
        self.assertFalse(fault_fired("nope", world.trace))
        evidence = {"snapshot": world.snapshot(),
                    "trace": list(world.trace)}
        self.assertTrue(evidence_complete(evidence, len(world.trace)))
        self.assertIsInstance(drivers_refund.drive, type(drive_agent))


class ResolveRejectionTests(unittest.TestCase):
    def test_unknown_rule_id_rejected_at_resolve(self):
        with temp_dir() as directory:
            registry = make_registry(directory, "ghostlab.")
            register_refund(registry)
            requested = direct_requested("refund-env", ("no_such_rule",))
            with self.assertRaisesRegex(ConfigError, "unsupported check"):
                resolve_requested(
                    requested, project_id="proj-t2",
                    requested_id="req-t2-direct",
                    requested_digest="d" * 64, requested_by="t2-test",
                    policy=Policy.from_obj(valid_policy()),
                    registry=registry)

    def test_refund_rule_on_booking_env_rejected(self):
        with temp_dir() as directory:
            registry = make_registry(
                directory, "ghostlab.", "examples.equipment_booking.")
            register_booking(registry)
            requested = direct_requested("booking-env",
                                         ("authorized_refunds",))
            with self.assertRaisesRegex(ConfigError, "unsupported check"):
                resolve_requested(
                    requested, project_id="proj-t2",
                    requested_id="req-t2-direct",
                    requested_digest="d" * 64, requested_by="t2-test",
                    policy=Policy.from_obj(valid_policy()),
                    registry=registry)

    def test_unregistered_env_skips_check_rejection(self):
        # Refund-family adapters under custom env ids (e.g. sibling
        # defect batteries) resolve without a registered driver.
        with temp_dir() as directory:
            registry = make_registry(directory, "ghostlab.", __name__)
            registry.register(
                env_id="refund-custom", env_version="refund-custom/1",
                adapter_ref=f"{__name__}:CustomRefundAdapter",
                display_name="Synthetic refund",
                declared_tools=deepcopy(REFUND_TOOLS),
                declared_modes=list(REFUND_MODES))
            requested = direct_requested("refund-custom", REFUND_IDS)
            resolved = resolve_requested(
                requested, project_id="proj-t2",
                requested_id="req-t2-direct",
                requested_digest="d" * 64, requested_by="t2-test",
                policy=Policy.from_obj(valid_policy()), registry=registry)
            self.assertEqual(resolved.pins.env_id, "refund-custom")


class CheckParamTests(unittest.TestCase):
    def test_unknown_param_rejected_on_real_check(self):
        snapshot = core_run(Fixture(), Scenario(), "vulnerable")[
            "final_state"]
        with self.assertRaises(CheckError):
            checks.evaluate("authorized_refunds", snapshot=snapshot,
                            trace=[], params={"bogus": 1},
                            fixture=Fixture())

    def test_out_of_bounds_and_wrong_type_rejected(self):
        try:
            checks.get_check("t2_probe_check")
        except CheckError:
            checks.register_check(
                check_id="t2_probe_check", version="1",
                description="T2 probe with a bounded int param",
                params_spec={"threshold": (int, 1, 10)},
                required_evidence=["snapshot"], applies_to="refund-env",
                temporal="stateless",
                interpret=lambda **kwargs: {
                    "verdict": "pass", "detail": "ok",
                    "evidence_refs": []})
        good = {"snapshot": {"probe": True}, "trace": [],
                "params": {"threshold": 5}, "fixture": Fixture()}
        self.assertEqual(checks.evaluate("t2_probe_check", **good)[
            "verdict"], "pass")
        for bad in ({"threshold": 11}, {"threshold": 0},
                    {"threshold": "high"}, {"threshold": True}, {}):
            with self.subTest(params=bad):
                with self.assertRaises(CheckError):
                    checks.evaluate("t2_probe_check", snapshot={"p": 1},
                                    trace=[], params=bad,
                                    fixture=Fixture())

    def test_checks_never_redefined_and_code_only(self):
        with self.assertRaisesRegex(CheckError, "already registered"):
            checks.register_check(
                check_id="authorized_refunds", version="2",
                description="redefine attempt", params_spec={},
                required_evidence=["snapshot"], applies_to="refund-env",
                temporal="stateless",
                interpret=lambda **kwargs: {
                    "verdict": "pass", "detail": "x",
                    "evidence_refs": []})
        with self.assertRaisesRegex(CheckError, "code callable"):
            checks.register_check(
                check_id="t2_client_shaped", version="1",
                description="client payload, not code", params_spec={},
                required_evidence=["snapshot"], applies_to="refund-env",
                temporal="stateless", interpret={"not": "code"})
        # Keyword-only: a positional client-shaped dict cannot register.
        with self.assertRaises(TypeError):
            checks.register_check({"check_id": "t2_client_shaped"})


class NoClientRegistrationSurfaceTests(unittest.TestCase):
    def test_ops_and_configs_expose_no_check_registration(self):
        self.assertFalse(hasattr(ops, "register_check"))
        self.assertNotIn("register_check", ops.__all__)
        self.assertFalse(hasattr(configs_module, "register_check"))
        self.assertFalse(hasattr(ops, "register_driver"))

    def test_requested_configs_cannot_carry_checks(self):
        obj = valid_requested(checks={"register": ["t2_client_shaped"]})
        with temp_dir() as directory:
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            with self.assertRaisesRegex(ConfigError, "exactly the v1"):
                ops.create_requested_config(store, "proj-t2", obj,
                                            valid_policy(), "t2-operator")


if __name__ == "__main__":
    unittest.main()
