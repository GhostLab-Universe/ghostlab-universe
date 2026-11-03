"""T1: defective-adapter battery — the qualifier catches real defects.

Each defective adapter below is a subclass of the reviewed
``RefundEnvAdapter`` defined in THIS test module and executed through the
REAL ``run_qualification`` runner against a track-private temporary database
(``tempfile`` under the system temp dir). No mocks, no source edits, zero
model calls.

Frozen contracts: ``docs/modules/testing-pipeline.md``. Qualification checks
(``src/ghostlab/envs/qualification.py``): reset, isolation, schemas,
evidence, fault-hooks, cancellation, reproduction-modes.

Layering note (case e): the ``fault-hooks`` qualification check covers
set/clear hygiene plus unknown-hook rejection ONLY — it never proves a hook
fires. Activation is proven per run: a required fault that never fired fails
the run verdict as untested (``controller.evaluate_verdict``). Case (e)
executes that proof via the public ``ops`` path.

Limitations (recorded, not fixed here):

- Qualification is bounded evidence that the seven checks passed on the
  probed fixture, not a proof the environment is safe or correct.
- Interleaved-operation isolation (concurrent overlapping invocations) is
  unsupported for synchronous adapters and is not probed; the isolation
  check covers sequential cross-instance separation only.
"""

import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ghostlab.envs.adapters import (REFUND_MODES, REFUND_TOOLS, EnvError,
                                    RefundEnvAdapter)
from ghostlab.envs.qualification import CHECKS, run_qualification
from ghostlab.envs.registry import EnvRegistry
from ghostlab.experiments import ops
from ghostlab.experiments.configs import ConfigError
from ghostlab.experiments.store import ExperimentStore

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"


# ---------------------------------------------------------------------------
# Defective adapters (a)–(g), each a subclass in this test module.
# ---------------------------------------------------------------------------

class ResetNoClearAdapter(RefundEnvAdapter):
    """(a) Mutations survive reset: only the first reset takes effect."""

    def reset(self, fixture):
        if self._world is None:
            super().reset(fixture)
        # else: keep the mutated world (defect).


class SharedStateAdapter(RefundEnvAdapter):
    """(b) All instances share one world: reset passes, isolation fails."""

    _shared_world = None

    def reset(self, fixture):
        super().reset(fixture)
        cls = type(self)
        if cls._shared_world is None:
            cls._shared_world = self._world
        else:
            shared = cls._shared_world
            fresh = self._world
            shared.fixture = fresh.fixture
            shared.scenario = fresh.scenario
            shared.actor_id = fresh.actor_id
            shared.ledger = []
            shared.trace = []
            shared.calls = 0
            shared._fault_fired = False
            self._world = shared


class MutatingRejectionAdapter(RefundEnvAdapter):
    """(c) Rejected calls mutate the ledger before raising."""

    def invoke(self, tool, arguments):
        try:
            return super().invoke(tool, arguments)
        except Exception:
            world = self._ready()
            tag = f"evil-{world.calls}-{len(world.ledger) + 1}"
            world.ledger.append({
                "transaction_id": tag,
                "order_id": world.fixture.order_id,
                "customer_id": world.fixture.customer_id,
                "amount_minor": 1,
                "idempotency_key": tag,
            })
            raise


class EvidenceOmissionAdapter(RefundEnvAdapter):
    """(d1) Evidence drops the last trace event (length >= 2)."""

    def evidence(self):
        found = super().evidence()
        if len(found["trace"]) >= 2:
            found["trace"] = found["trace"][:-1]
        return found


class EvidenceMisattributionAdapter(RefundEnvAdapter):
    """(d2) Evidence reverses event_ids (wrong ids/order, same length)."""

    def evidence(self):
        found = super().evidence()
        total = len(found["trace"])
        for index, event in enumerate(found["trace"]):
            event["event_id"] = total - index
        return found


class NeverFiringFaultAdapter(RefundEnvAdapter):
    """(e) Declared hook accepts set_fault but never arms, so it never fires.

    Qualification hygiene (set/clear + unknown rejection) still passes; the
    per-run activation proof (required + unfired => verdict fail) catches it.
    """

    env_id = "defect-neverfires"
    version = "test/1"

    def set_fault(self, name, params):
        if name != "timeout_after_commit":
            raise EnvError(f"unknown fault hook '{name}'")
        if type(params) is not dict or params != {}:
            raise ValueError("timeout_after_commit takes no parameters")
        return None  # accept but never arm (defect).


class CancellationIgnoringAdapter(RefundEnvAdapter):
    """(f) invoke_cancellable ignores the cancellation event."""

    def invoke_cancellable(self, tool, arguments, cancel=None):
        return self.invoke(tool, arguments)


class ReplayDivergentAdapter(RefundEnvAdapter):
    """(g) Each issue_refund appends a unique row, so replays diverge."""

    _replay_counter = 0

    def invoke(self, tool, arguments):
        result = super().invoke(tool, arguments)
        if tool == "issue_refund":
            type(self)._replay_counter += 1
            tag = type(self)._replay_counter
            world = self._ready()
            world.ledger.append({
                "transaction_id": f"evil-{tag}",
                "order_id": world.fixture.order_id,
                "customer_id": world.fixture.customer_id,
                "amount_minor": tag,
                "idempotency_key": f"evil-key-{tag}",
            })
        return result


class T1IdentityCleanAdapter(RefundEnvAdapter):
    """(h) Behavior-identical clean adapter with a T1-local identity.

    Only ``describe()`` identity differs from ``RefundEnvAdapter`` so the
    execution-gate re-verification runs on a distinct registration instead
    of duplicating the existing ``refund-env`` test.
    """

    env_id = "refund-t1-identity"
    version = "refund-t1-identity/1"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def make_registry(directory):
    return EnvRegistry(
        Path(directory) / "t1-env.sqlite",
        allowed_adapter_prefixes=("ghostlab.", __name__))


def register_test_adapter(registry, env_id, cls_name):
    return registry.register(
        env_id=env_id, env_version="test/1",
        adapter_ref=f"{__name__}:{cls_name}", display_name="T1 defective",
        declared_tools=deepcopy(REFUND_TOOLS),
        declared_modes=list(REFUND_MODES))


def register_clean_refund(registry, env_id="clean-refund"):
    return registry.register(
        env_id=env_id, env_version="refund-env/1", adapter_ref=REFUND_REF,
        display_name="Synthetic refund", declared_tools=deepcopy(REFUND_TOOLS),
        declared_modes=list(REFUND_MODES))


def failing_names(report):
    return {c["name"] for c in report["checks"] if not c["passed"]}


def valid_policy():
    return {"policy_version": "policy/1", "ceiling_tool_calls": 20,
            "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
            "ceiling_wall_clock_ms": 60000,
            "allowed_agents": ["vulnerable", "repaired"],
            "allowed_hooks": ["timeout_after_commit"]}


def valid_requested(env_id):
    return {"config_version": 1, "name": "t1 fault probe",
            "env_id": env_id, "agent": "vulnerable",
            "fixture": {"order_id": "synthetic-order-1",
                        "customer_id": "synthetic-customer-1",
                        "paid_minor": 250000, "currency": "INR"},
            "faults": [{"hook": "timeout_after_commit", "params": {},
                        "required": True}],
            "rules": ["authorized_refunds", "total_refund_within_payment",
                      "no_duplicate_full_refund", "unique_idempotency_keys"],
            "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                       "max_wall_clock_ms": 60000},
            "repetitions": {"count": 1}}


class QualificationDefectsT1Tests(unittest.TestCase):
    def setUp(self):
        SharedStateAdapter._shared_world = None
        ReplayDivergentAdapter._replay_counter = 0

    def tearDown(self):
        SharedStateAdapter._shared_world = None
        ReplayDivergentAdapter._replay_counter = 0

    def _qualify_defective(self, directory, env_id, cls_name):
        registry = make_registry(directory)
        register_test_adapter(registry, env_id, cls_name)
        report = run_qualification(registry, env_id)
        self.assertEqual([c["name"] for c in report["checks"]], list(CHECKS))
        return registry, report

    def _assert_clean_qualifies(self, registry, env_id="clean-refund"):
        register_clean_refund(registry, env_id)
        clean = run_qualification(registry, env_id)
        self.assertEqual(clean["status"], "qualified-for-declared",
                         [c for c in clean["checks"] if not c["passed"]])
        self.assertTrue(all(c["passed"] for c in clean["checks"]))
        self.assertEqual(registry.get(env_id)["status"],
                         "qualified-for-declared")
        return clean

    def test_a_reset_no_clear_fails_only_reset(self):
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry, report = self._qualify_defective(
                directory, "defect-reset", "ResetNoClearAdapter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(failing_names(report), {"reset"})
            self.assertEqual(registry.get("defect-reset")["status"], "failed")
            self._assert_clean_qualifies(registry)

    def test_b_shared_state_fails_only_isolation(self):
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry, report = self._qualify_defective(
                directory, "defect-shared", "SharedStateAdapter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(failing_names(report), {"isolation"})
            by_name = {c["name"]: c for c in report["checks"]}
            self.assertTrue(by_name["reset"]["passed"])
            self.assertEqual(registry.get("defect-shared")["status"], "failed")
            self._assert_clean_qualifies(registry)

    def test_c_mutating_rejection_fails_only_schemas(self):
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry, report = self._qualify_defective(
                directory, "defect-schemas", "MutatingRejectionAdapter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(failing_names(report), {"schemas"})
            self.assertEqual(registry.get("defect-schemas")["status"],
                             "failed")
            self._assert_clean_qualifies(registry)

    def test_d1_evidence_omission_fails_evidence(self):
        # Omission drops a trace event, so both evidence (ordered 1..N) and
        # isolation (own trace length) read the shortened trace and fail.
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry, report = self._qualify_defective(
                directory, "defect-omission", "EvidenceOmissionAdapter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(failing_names(report),
                             {"isolation", "evidence"})
            self.assertEqual(registry.get("defect-omission")["status"],
                             "failed")
            self._assert_clean_qualifies(registry)

    def test_d2_evidence_misattribution_fails_only_evidence(self):
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry, report = self._qualify_defective(
                directory, "defect-misattr", "EvidenceMisattributionAdapter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(failing_names(report), {"evidence"})
            self.assertEqual(registry.get("defect-misattr")["status"],
                             "failed")
            self._assert_clean_qualifies(registry)

    def test_e_never_firing_hook_passes_hygiene_but_fails_per_run(self):
        # Honest layering: qual fault-hooks proves set/clear hygiene and
        # unknown rejection, NOT activation. Activation is proven per run:
        # required + unfired => verdict fail (untested, never pass).
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry = make_registry(directory)
            register_test_adapter(registry, "defect-neverfires",
                                  "NeverFiringFaultAdapter")
            report = run_qualification(registry, "defect-neverfires")
            self.assertEqual(report["status"], "qualified-for-declared",
                             [c for c in report["checks"]
                              if not c["passed"]])
            by_name = {c["name"]: c for c in report["checks"]}
            self.assertTrue(by_name["fault-hooks"]["passed"])
            store = ExperimentStore(Path(directory) / "t1-exp.sqlite")
            created = ops.create_requested_config(
                store, "proj-t1", valid_requested("defect-neverfires"),
                valid_policy(), "t1-operator")
            resolved = ops.resolve_config(
                store, registry, "proj-t1", created["config_id"],
                valid_policy(), "t1-operator")
            run = ops.start_run(store, registry, "proj-t1",
                                resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "completed")
            self.assertEqual(len(body["faults"]), 1)
            self.assertEqual(body["faults"][0]["hook"],
                             "timeout_after_commit")
            self.assertTrue(body["faults"][0]["required"])
            self.assertIs(body["faults"][0]["fired"], False)
            # The ledger itself is intact (single full refund, no retry was
            # ever triggered), so the failure is SOLELY the unfired fault.
            self.assertEqual(body["rules_failed"], [])
            self.assertEqual(body["verdict"], "fail")
            self._assert_clean_qualifies(registry)

    def test_f_cancellation_ignored_fails_only_cancellation(self):
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry, report = self._qualify_defective(
                directory, "defect-cancel", "CancellationIgnoringAdapter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(failing_names(report), {"cancellation"})
            self.assertEqual(registry.get("defect-cancel")["status"], "failed")
            self._assert_clean_qualifies(registry)

    def test_g_replay_divergence_fails_only_reproduction_modes(self):
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry, report = self._qualify_defective(
                directory, "defect-replay", "ReplayDivergentAdapter")
            self.assertEqual(report["status"], "failed")
            self.assertEqual(failing_names(report), {"reproduction-modes"})
            by_name = {c["name"]: c for c in report["checks"]}
            self.assertIn("diverged", by_name["reproduction-modes"]["detail"])
            self.assertEqual(registry.get("defect-replay")["status"], "failed")
            self._assert_clean_qualifies(registry)

    def test_h_identity_change_refuses_execution(self):
        # Re-verifies the existing changed-code execution gate
        # (tests/test_booking_env_integration.py::ExecutionGateTests::
        # test_execute_refuses_changed_adapter_code, enforced by
        # controller.execute_run's digest check) on a distinct T1
        # registration: the gate is an execution-time digest comparison,
        # not the stored status — status stays qualified while runs refuse.
        with tempfile.TemporaryDirectory(prefix="t1-qual-") as directory:
            registry = make_registry(directory)
            registry.register(
                env_id="refund-t1-identity",
                env_version="refund-t1-identity/1",
                adapter_ref=f"{__name__}:T1IdentityCleanAdapter",
                display_name="T1 clean identity",
                declared_tools=deepcopy(REFUND_TOOLS),
                declared_modes=list(REFUND_MODES))
            report = run_qualification(registry, "refund-t1-identity")
            self.assertEqual(report["status"], "qualified-for-declared",
                             [c for c in report["checks"]
                              if not c["passed"]])
            store = ExperimentStore(Path(directory) / "t1-exp.sqlite")
            created = ops.create_requested_config(
                store, "proj-t1", valid_requested("refund-t1-identity"),
                valid_policy(), "t1-operator")
            resolved = ops.resolve_config(
                store, registry, "proj-t1", created["config_id"],
                valid_policy(), "t1-operator")
            connection = sqlite3.connect(Path(directory) / "t1-env.sqlite")
            try:
                connection.execute(
                    "UPDATE env_registrations SET code_digest = ?"
                    " WHERE env_id = ?", ("f" * 64, "refund-t1-identity"))
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(registry.get("refund-t1-identity")["status"],
                             "qualified-for-declared")
            with self.assertRaisesRegex(ConfigError, "changed since"):
                ops.start_run(store, registry, "proj-t1",
                              resolved["resolved_id"])
            # Control: an untampered clean registration still qualifies.
            self._assert_clean_qualifies(registry)


if __name__ == "__main__":
    unittest.main()
