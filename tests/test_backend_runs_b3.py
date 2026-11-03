"""Track B3: controller backend integration, receipts, lifecycle.

Covers explicit backend selection (default trusted-local stays
byte-identical), refusal of unknown/unavailable backends without
fallback or run records, the per-run job lifecycle on backend runs,
project-scoped job receipts with observed-vs-claimed separation,
cancel/wall/output-exhaustion mapping, reconcile sweeps and
cross-project job refusal.

Two backend doubles are used: the real ``LocalBackend`` (subprocess
worker; refund env only, since its worker allowlist is ``ghostlab.``)
for transport-level proof, and the in-process ``FakeBackend`` below for
booking runs and fault injection. The fake executes adapter objects
in-process and is NEVER a sandbox; it mirrors worker.py/local.py wire
semantics (unframed commands, enveloped events, ack tool_results,
single inline evidence_chunk, close/completed) so controller behavior
transfers.

Every test uses track-private temporary databases; no live files and no
other session's processes are touched.
"""

import contextlib
import gc
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.equipment_booking import driver_booking  # noqa: E402
from examples.equipment_booking.env_adapter import (  # noqa: E402
    BOOKING_TOOLS, BookingEnvAdapter)
from ghostlab.backends import protocol as backend_protocol  # noqa: E402
from ghostlab.backends.interface import (  # noqa: E402
    BackendError, BackendUnavailableError, JobSpec)
from ghostlab.backends.local import LocalBackend  # noqa: E402
from ghostlab.backends.receipts import (  # noqa: E402
    CrossProjectError as ReceiptCrossProjectError)
from ghostlab.backends.receipts import (  # noqa: E402
    JobHandle, JobReceiptStore, ProtocolError, validate_command,
    validate_envelope)
from ghostlab.envs.adapters import (  # noqa: E402
    REFUND_MODES, REFUND_TOOLS, RefundEnvAdapter)
from ghostlab.envs.qualification import run_qualification  # noqa: E402
from ghostlab.envs.registry import EnvRegistry  # noqa: E402
from ghostlab.experiments import checks as checks_module  # noqa: E402
from ghostlab.experiments import drivers  # noqa: E402
from ghostlab.experiments import ops  # noqa: E402
from ghostlab.experiments.checks import CheckError  # noqa: E402
from ghostlab.experiments.configs import (  # noqa: E402
    ConfigError, Policy, RequestedConfig, canonical_digest,
    resolve_requested, validate_requested)
from ghostlab.experiments.controller import (  # noqa: E402
    execute_run, get_backend, register_backend, unregister_backend)
from ghostlab.experiments.store import (  # noqa: E402
    CrossProjectError, ExperimentStore)

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
BOOKING_REF = "examples.equipment_booking.env_adapter:BookingEnvAdapter"
BOOKING_MODES = ["recorded-actions", "deterministic-reset"]
BOOKING_FIXTURE = {"equipment_id": "synthetic-microscope-1",
                   "user_id": "synthetic-user-1",
                   "slot": "2026-11-03T09:00Z"}
REFUND_FIXTURE = {"order_id": "synthetic-order-1",
                  "customer_id": "synthetic-customer-1",
                  "paid_minor": 250000, "currency": "INR"}
BOOKING_IDS = ("authorized_bookings", "no_duplicate_booking",
               "unique_idempotency_keys")
REFUND_IDS = ("authorized_refunds", "total_refund_within_payment",
              "no_duplicate_full_refund", "unique_idempotency_keys")

# Exact stored run-body keys for trusted-local runs (run_id/digest are
# assigned by the store). Any accidental addition breaks this test.
TRUSTED_RUN_KEYS = frozenset({
    "schema_version", "project_id", "resolved_id", "repetition",
    "attempt", "seed", "status", "verdict", "model_calls", "tool_calls",
    "wall_clock_ms", "faults", "evidence", "checks",
    "rules_evaluated", "rules_failed", "error", "started_at",
    "completed_at", "run_id", "digest"})


def setUpModule():
    if drivers.get_driver(driver_booking.ENV_ID) is None:
        drivers.register_driver(driver_booking.ENV_ID, driver_booking)
    for spec in driver_booking.BOOKING_CHECK_SPECS:
        try:
            checks_module.get_check(spec["check_id"])
        except CheckError:
            checks_module.register_check(**spec)


@contextlib.contextmanager
def temp_dir():
    base = "/tmp" if os.path.isdir("/tmp") else None
    with tempfile.TemporaryDirectory(prefix="b3-", dir=base) as directory:
        yield directory
        gc.collect()


def make_registry(directory):
    return EnvRegistry(
        Path(directory) / "env.sqlite",
        allowed_adapter_prefixes=("ghostlab.",
                                  "examples.equipment_booking."))


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


def make_store(directory):
    return ExperimentStore(Path(directory) / "experiments.sqlite")


def valid_policy(**overrides):
    policy = {"policy_version": "policy/1", "ceiling_tool_calls": 20,
              "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
              "ceiling_wall_clock_ms": 60000,
              "allowed_agents": ["vulnerable", "repaired"],
              "allowed_hooks": ["timeout_after_commit"]}
    policy.update(overrides)
    return policy


def refund_requested(**overrides):
    obj = {"config_version": 1, "name": "refund timeout probe",
           "env_id": "refund-env", "agent": "vulnerable",
           "fixture": dict(REFUND_FIXTURE),
           "faults": [{"hook": "timeout_after_commit", "params": {},
                       "required": True}],
           "rules": list(REFUND_IDS),
           "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                      "max_wall_clock_ms": 60000},
           "repetitions": {"count": 1}}
    obj.update(overrides)
    return obj


def booking_requested(**overrides):
    obj = {"config_version": 1, "name": "booking timeout probe",
           "env_id": "booking-env", "agent": "vulnerable",
           "fixture": dict(BOOKING_FIXTURE),
           "faults": [{"hook": "timeout_after_commit", "params": {},
                       "required": True}],
           "rules": list(BOOKING_IDS),
           "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                      "max_wall_clock_ms": 60000},
           "repetitions": {"count": 1}}
    obj.update(overrides)
    return obj


@contextlib.contextmanager
def swapped_backend(name, backend):
    """Temporarily register ``backend`` as ``name`` (restores after)."""
    try:
        previous = get_backend(name)
    except ConfigError:
        previous = None
    else:
        unregister_backend(name)
    register_backend(name, backend)
    try:
        yield backend
    finally:
        unregister_backend(name)
        if previous is not None:
            register_backend(name, previous)


class _FakeJob:
    def __init__(self, spec):
        self.spec = spec
        self.adapter = None
        self.state = "starting"
        self.queue = []
        self.event_seq = 0
        self.output_bytes = 0
        self.hello_seen = False
        self.invoke_count = 0
        self.poll_count = 0
        self.workspace = None
        self.truncated = False


class FakeBackend:
    """In-process test double mirroring worker.py/local.py semantics.

    Executes adapter objects in-process to validate controller wiring,
    lifecycle mapping, receipts and refusals. It provides NO isolation
    and is NEVER a sandbox. Wire behavior mirrors the real worker: ack
    ``tool_result`` for setup commands, ``tool_result`` per invoke,
    single inline ``evidence_chunk``, ``completed`` on close, gap-free
    event seqs from 0, and backend-side terminal transitions on
    truncation/failure output.
    """

    name = "fake-local"
    version = "execution-backend/1"

    def __init__(self, adapters, **flags):
        self._adapters = dict(adapters)
        self._flags = flags
        self._jobs = {}
        self.calls = []

    def capabilities(self) -> dict:
        return {
            "isolation": {
                "filesystem": "none (test double; NOT a sandbox)",
                "process": "none (test double; NOT a sandbox)",
                "network": "none (test double; NOT a sandbox)"},
            "limits": ["wall_clock_ms", "max_output_bytes"],
            "qualification": {"status": "unqualified-test-double"},
        }

    def _require(self, job_id: str) -> _FakeJob:
        if type(job_id) is not str:
            raise BackendError("unknown job")
        job = self._jobs.get(job_id)
        if job is None:
            raise BackendError(f"unknown job '{job_id}'")
        return job

    def prepare(self, package: dict) -> dict:
        self.calls.append(("prepare", deepcopy(package)))
        if self._flags.get("mode") == "unavailable":
            raise BackendUnavailableError(
                "fake backend down for this test")
        if type(package) is not dict \
                or set(package) != {"kind", "identity"}:
            raise BackendError(
                "package must be exactly {kind, identity}")
        if package["kind"] != "checked-in":
            raise BackendError(
                f"unsupported package kind {package['kind']!r}")
        identity = package["identity"]
        if type(identity) is not str or not identity \
                or len(identity) > 256:
            raise BackendError("checked-in packages must pin an identity")
        if self._flags.get("wrong_identity"):
            return {"identity": "wrong-identity-for-test",
                    "provenance": {"kind": "checked-in"}}
        return {"identity": identity,
                "provenance": {"kind": "checked-in",
                               "test-double": True}}

    def start_job(self, spec) -> None:
        self.calls.append(("start_job", getattr(spec, "job_id", None)))
        if self._flags.get("mode") == "unavailable":
            raise BackendUnavailableError(
                "fake backend down for this test")
        if type(spec) is not JobSpec:
            raise BackendError("spec must be a JobSpec")
        if spec.job_id in self._jobs:
            raise BackendError(f"duplicate job '{spec.job_id}'")
        if spec.package["kind"] != "checked-in":
            raise BackendError(
                f"unsupported package kind {spec.package['kind']!r}")
        job = _FakeJob(spec)
        root = self._flags.get("workspace_root")
        if root:
            workspace = str(Path(root) / spec.job_id)
            Path(workspace).mkdir(parents=True, exist_ok=False)
            marker = {"project_id": spec.project_id,
                      "job_id": spec.job_id}
            Path(workspace, ".ghostlab-job.json").write_text(
                json.dumps(marker), encoding="utf-8")
            job.workspace = workspace
        self._jobs[spec.job_id] = job
        return None

    def _emit(self, job: _FakeJob, event_type: str, payload: dict) -> None:
        job_id = "job-FORGED" if self._flags.get("forge_job") \
            else job.spec.job_id
        try:
            raw = backend_protocol.encode_message(
                job_id, job.event_seq, event_type, payload)
        except ProtocolError:
            fallback = {"v": 1, "job_id": job.spec.job_id,
                        "seq": job.event_seq, "type": "truncated",
                        "payload": {"reason": "output-exhausted"}}
            job.event_seq += 1
            job.queue.append(fallback)
            job.truncated = True
            job.state = "failed"
            return
        job.event_seq += 1
        job.output_bytes += len(raw) + 1
        job.queue.append(json.loads(raw.decode("utf-8")))

    def _tool_result(self, ok: bool, result=None, error=None,
                     message=None) -> dict:
        payload: dict = {"ok": ok}
        if ok:
            if result is not None:
                pad = self._flags.get("result_pad_bytes", 0)
                if pad and type(result) is dict:
                    result = dict(result)
                    result["_pad"] = "x" * pad
                payload["result"] = result
        else:
            payload["error"] = error or "AdapterError"
            payload["message"] = str(message or "")[:500]
        return payload

    def _fail_job(self, job: _FakeJob, error: str, message: str) -> None:
        self._emit(job, "failed", {"error": error,
                                   "message": str(message)[:500]})
        job.state = "failed"

    def send(self, job_id: str, command: dict) -> None:
        job = self._require(job_id)
        if type(command) is not dict \
                or type(command.get("type")) is not str:
            raise BackendError("command must carry a type string")
        if command["type"] not in backend_protocol.WORKER_COMMANDS:
            raise BackendError(f"unknown command '{command['type']}'")
        payload = command.get("payload", {})
        if type(payload) is not dict:
            raise BackendError("command payload must be an object")
        if job.state in ("completed", "failed", "cancelled", "timed_out"):
            raise BackendError(f"job '{job_id}' is {job.state}")
        ctype = command["type"]
        if ctype == "hello":
            if job.hello_seen:
                self._fail_job(job, "ProtocolViolation",
                               "duplicate hello")
                return
            factory = self._adapters.get(job.spec.adapter_ref)
            if factory is None:
                self._fail_job(job, "ValueError",
                               "adapter module is not in the allowlist")
                return
            try:
                job.adapter = factory()
                describe = job.adapter.describe()
                if type(describe) is not dict:
                    raise ValueError("describe() must return an object")
            except Exception as error:
                self._fail_job(job, type(error).__name__, str(error))
                return
            job.hello_seen = True
            self._emit(job, "started",
                         {"adapter": job.spec.adapter_ref,
                          "describe": describe})
            job.state = "running"
            return
        if not job.hello_seen or job.adapter is None:
            self._fail_job(job, "ProtocolViolation",
                           "hello required first")
            return
        if ctype == "reset":
            fixture = payload.get("fixture")
            if type(fixture) is not dict:
                self._fail_job(job, "ProtocolViolation",
                               "reset needs a fixture object")
                return
            try:
                job.adapter.reset(fixture)
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__,
                    message=str(error)))
                return
            self._emit(job, "tool_result", self._tool_result(True))
            return
        if ctype == "invoke":
            tool, arguments = payload.get("tool"), payload.get("arguments")
            if type(tool) is not str or type(arguments) is not dict:
                self._fail_job(job, "ProtocolViolation",
                               "invoke needs tool+arguments")
                return
            job.invoke_count += 1
            hook = self._flags.get("on_invoke")
            if hook is not None:
                hook(job.invoke_count, tool)
            if self._flags.get("truncate_on_invoke") == job.invoke_count:
                job.truncated = True
                self._emit(job, "truncated",
                             {"reason": "output-exhausted",
                              "output_bytes": job.output_bytes})
                job.state = "failed"
                return
            if self._flags.get("fail_exhausted_on_invoke") \
                    == job.invoke_count:
                job.truncated = True
                self._emit(job, "failed",
                             {"error": "output-exhausted",
                              "truncated": True})
                job.state = "failed"
                return
            try:
                outcome = job.adapter.invoke(tool, arguments)
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__,
                    message=str(error)))
                return
            try:
                json.dumps(outcome, allow_nan=False)
            except (TypeError, ValueError):
                self._emit(job, "truncated",
                             {"reason": "output-exhausted"})
                job.state = "failed"
                return
            self._emit(job, "tool_result",
                         self._tool_result(True, result=outcome))
            return
        if ctype == "set_fault":
            name, params = payload.get("name"), payload.get("params")
            if type(name) is not str or type(params) is not dict:
                self._fail_job(job, "ProtocolViolation",
                               "set_fault needs name+params")
                return
            try:
                job.adapter.set_fault(name, params)
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__,
                    message=str(error)))
                return
            self._emit(job, "tool_result", self._tool_result(True))
            return
        if ctype == "clear_faults":
            try:
                job.adapter.clear_faults()
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__,
                    message=str(error)))
                return
            self._emit(job, "tool_result", self._tool_result(True))
            return
        if ctype == "evidence":
            try:
                evidence = job.adapter.evidence()
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__,
                    message=str(error)))
                return
            chunk = {"evidence": evidence}
            if self._flags.get("lie_digest"):
                chunk["evidence_digest"] = "0" * 64
            self._emit(job, "evidence_chunk", chunk)
            return
        if ctype == "snapshot":
            try:
                outcome = job.adapter.snapshot()
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__,
                    message=str(error)))
                return
            self._emit(job, "tool_result",
                         self._tool_result(True, result=outcome))
            return
        if ctype == "close":
            self._emit(job, "completed", {})
            job.state = "completed"
            return
        self._fail_job(job, "UnknownCommand",
                       f"unknown command '{ctype}'")

    def poll(self, job_id: str, timeout_ms: int = 0) -> list:
        job = self._require(job_id)
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise BackendError(
                "timeout_ms must be a non-negative integer")
        job.poll_count += 1
        trip = self._flags.get("timed_out_on_poll")
        if trip is not None and job.poll_count > trip \
                and job.state == "running":
            job.state = "timed_out"
        if self._flags.get("hang"):
            time.sleep(min(timeout_ms, 25) / 1000.0)
            return []
        if job.state == "timed_out":
            pending, job.queue = job.queue, []
            return pending
        pending, job.queue = job.queue, []
        return pending

    def status(self, job_id: str) -> dict:
        job = self._require(job_id)
        body = {"job_id": job.spec.job_id, "state": job.state,
                "output_bytes": job.output_bytes, "elapsed_ms": 0}
        if job.workspace is not None:
            body["workspace"] = job.workspace
        return body

    def cancel(self, job_id: str, grace_ms: int = 2000) -> dict:
        job = self._require(job_id)
        if type(grace_ms) is not int or grace_ms < 0:
            raise BackendError(
                "grace_ms must be a non-negative integer")
        if self._flags.get("cancel_fails"):
            raise BackendError("cancel failed for this test")
        if job.state in ("completed", "failed", "cancelled",
                         "timed_out"):
            return {"state": job.state, "confirmed": True}
        job.state = "cancelled"
        job.queue.clear()
        return {"state": "cancelled", "confirmed": True}

    def close(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job.state not in ("completed", "failed", "cancelled",
                             "timed_out"):
            job.state = "failed"
        if job.workspace is not None:
            import shutil
            shutil.rmtree(job.workspace, ignore_errors=True)
        self._jobs.pop(job_id, None)
        return {"job_id": job_id, "state": job.state,
                "receipts": [
                    {"kind": "lifecycle", "state": job.state,
                     "reason": "test-double"},
                    {"kind": "output", "bytes": job.output_bytes,
                     "truncated": job.truncated}]}


def fake_adapters():
    return {REFUND_REF: RefundEnvAdapter,
            BOOKING_REF: BookingEnvAdapter}


class NoDriverAdapter(RefundEnvAdapter):
    """Refund behavior under an env id with no registered driver."""

    env_id = "b3-nodriver"
    version = "b3-nodriver/1"


class BackendSelectionTests(unittest.TestCase):
    def test_requested_backend_shape_strict(self):
        policy = valid_policy()
        good = refund_requested(backend={"name": "local"})
        body = validate_requested(good, policy)
        self.assertEqual(body["backend"], {"name": "local"})
        good = refund_requested(
            backend={"name": "local",
                     "package": {"kind": "checked-in",
                                 "identity": "abc"}})
        body = validate_requested(good, policy)
        self.assertEqual(body["backend"]["package"],
                         {"kind": "checked-in", "identity": "abc"})
        requested = RequestedConfig.from_obj(good, policy)
        self.assertEqual(requested.to_dict(), body)
        bad = [
            {"name": "local", "zzz": 1},
            {"name": "local",
             "package": {"kind": "checked-in", "identity": "a",
                         "zzz": 1}},
            {"name": "Local"},
            {"name": ""},
            {"name": 7},
            {"package": {"kind": "checked-in", "identity": "a"}},
            {"name": "local", "package": "checked-in"},
            {"name": "local", "package": {"kind": "Checked-in",
                                          "identity": "a"}},
            {"name": "local", "package": {"kind": "checked-in",
                                          "identity": ""}},
            {"name": "local", "package": {"kind": "checked-in"}},
        ]
        for backend in bad:
            with self.subTest(backend=backend):
                with self.assertRaises(ConfigError):
                    validate_requested(
                        refund_requested(backend=backend), policy)
        with self.assertRaises(ConfigError):
            validate_requested(
                refund_requested(backend=None), policy)

    def test_default_resolve_pins_trusted_local_run_body_unchanged(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            created = ops.create_requested_config(
                store, "proj-b3", refund_requested(), policy, "b3-test")
            # Requested bodies without a backend are byte-identical.
            self.assertNotIn("backend", created["config"])
            self.assertEqual(created["config"],
                             refund_requested())
            resolved = ops.resolve_config(
                store, registry, "proj-b3", created["config_id"],
                policy, "b3-test")
            self.assertEqual(resolved["resolved"]["pins"]["backend"],
                             {"name": "trusted-local",
                              "backend_version": "trusted-local/1"})
            run = ops.start_run(store, registry, "proj-b3",
                                resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(set(body), TRUSTED_RUN_KEYS)
            self.assertNotIn("backend", body)
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["tool_calls"], 3)
            # No receipt file is derived for the in-process path.
            sibling = Path(store.db_path).with_name(
                Path(store.db_path).stem + "-job-receipts"
                + Path(store.db_path).suffix)
            self.assertFalse(sibling.exists())
            # Explicit trusted-local selects the same identical path.
            created = ops.create_requested_config(
                store, "proj-b3",
                refund_requested(backend={"name": "trusted-local"}),
                policy, "b3-test")
            resolved = ops.resolve_config(
                store, registry, "proj-b3", created["config_id"],
                policy, "b3-test")
            self.assertEqual(resolved["resolved"]["pins"]["backend"],
                             {"name": "trusted-local",
                              "backend_version": "trusted-local/1"})
            run = ops.start_run(store, registry, "proj-b3",
                                resolved["resolved_id"])
            self.assertEqual(set(run["run"]), TRUSTED_RUN_KEYS)
            self.assertNotIn("backend", run["run"])

    def test_unknown_backend_refused_at_resolve(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            created = ops.create_requested_config(
                store, "proj-b3",
                refund_requested(
                    backend={"name": "no-such-backend"}),
                policy, "b3-test")
            with self.assertRaisesRegex(ConfigError, "unknown backend"):
                ops.resolve_config(store, registry, "proj-b3",
                                   created["config_id"], policy,
                                   "b3-test")
            self.assertEqual(store.list_resolved("proj-b3"), [])

    def test_unknown_backend_refused_at_start(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            created = ops.create_requested_config(
                store, "proj-b3", refund_requested(), policy, "b3-test")
            resolved = ops.resolve_config(
                store, registry, "proj-b3", created["config_id"],
                policy, "b3-test")
            body = deepcopy(resolved["resolved"])
            body["resolved_config_id"] = "res-b3-crafted-unknown"
            body["pins"]["backend"] = {
                "name": "no-such-backend",
                "backend_version": "execution-backend/1",
                "package_kind": "checked-in",
                "package_identity": "0" * 64}
            body["digest"] = canonical_digest(
                {k: v for k, v in body.items() if k != "digest"})
            crafted = store.save_resolved(
                project_id="proj-b3",
                requested_id=resolved["requested_id"], body=body,
                digest=body["digest"], created_at=body["resolved_at"])
            with self.assertRaisesRegex(ConfigError, "unknown backend"):
                ops.start_run(store, registry, "proj-b3",
                              crafted["resolved_id"])
            self.assertEqual(store.list_runs("proj-b3"), [])
            self.assertEqual(
                store.budget_used("proj-b3", crafted["resolved_id"],
                                  "tool_calls"), 0)
            sibling = Path(store.db_path).with_name(
                Path(store.db_path).stem + "-job-receipts"
                + Path(store.db_path).suffix)
            self.assertFalse(sibling.exists())

    def test_unavailable_backend_refused_without_fallback_or_record(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            registration = register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(), mode="unavailable")
            with swapped_backend("b3-unavail", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(
                        backend={"name": "b3-unavail"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                # Resolution pins without probing availability.
                self.assertEqual(
                    resolved["resolved"]["pins"]["backend"],
                    {"name": "b3-unavail",
                     "backend_version": "execution-backend/1",
                     "package_kind": "checked-in",
                     "package_identity":
                     registration["code_digest"]})
                with self.assertRaises(BackendUnavailableError):
                    ops.start_run(store, registry, "proj-b3",
                                  resolved["resolved_id"])
                # Refusal: no run record, no ledger entry, no fallback
                # execution (a fallback would have completed a run).
                self.assertEqual(store.list_runs("proj-b3"), [])
                self.assertEqual(
                    store.budget_used("proj-b3",
                                      resolved["resolved_id"],
                                      "tool_calls"), 0)
                self.assertTrue(
                    any(call[0] == "prepare"
                        for call in fake.calls))
                sibling = Path(store.db_path).with_name(
                    Path(store.db_path).stem + "-job-receipts"
                    + Path(store.db_path).suffix)
                self.assertTrue(sibling.exists())
                receipts = JobReceiptStore(sibling)
                jobs = receipts.list_receipts("proj-b3")
                self.assertEqual(len(jobs), 1)
                receipt = receipts.get_receipt(
                    "proj-b3", jobs[0]["job_id"])
                self.assertEqual(receipt["state"], "failed")
                self.assertEqual(
                    [h["state"] for h in receipt["history"]],
                    ["prepared", "failed"])

    def test_prepare_identity_mismatch_refused(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(), wrong_identity=True)
            with swapped_backend("b3-mismatch", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(
                        backend={"name": "b3-mismatch"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                with self.assertRaisesRegex(ConfigError, "match"):
                    ops.start_run(store, registry, "proj-b3",
                                  resolved["resolved_id"])
                self.assertEqual(store.list_runs("proj-b3"), [])

    def test_unsupported_package_kind_refused_on_real_backend(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            backend = LocalBackend(Path(directory) / "workspaces")
            with swapped_backend("local", backend):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={
                        "name": "local",
                        "package": {"kind": "nope",
                                    "identity": "x"}}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                self.assertEqual(
                    resolved["resolved"]["pins"]["backend"],
                    {"name": "local",
                     "backend_version": "execution-backend/1",
                     "package_kind": "nope",
                     "package_identity": "x"})
                with self.assertRaisesRegex(
                        BackendError, "unsupported package kind"):
                    ops.start_run(store, registry, "proj-b3",
                                  resolved["resolved_id"])
                self.assertEqual(store.list_runs("proj-b3"), [])

    def test_backend_run_requires_registered_driver(self):
        with temp_dir() as directory:
            registry = EnvRegistry(
                Path(directory) / "env.sqlite",
                allowed_adapter_prefixes=(
                    "ghostlab.", "examples.equipment_booking.",
                    __name__))
            registry.register(
                env_id="b3-nodriver", env_version="b3-nodriver/1",
                adapter_ref=f"{__name__}:NoDriverAdapter",
                display_name="Driverless",
                declared_tools=deepcopy(REFUND_TOOLS),
                declared_modes=list(REFUND_MODES))
            run_qualification(registry, "b3-nodriver")
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters())
            with swapped_backend("b3-driverless", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(env_id="b3-nodriver",
                                     backend={
                                         "name": "b3-driverless"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                # The controller never imports adapter code to sniff a
                # driver on the backend path: no registered driver is a
                # refusal, not a legacy fallback.
                with self.assertRaisesRegex(ConfigError, "no driver"):
                    ops.start_run(store, registry, "proj-b3",
                                  resolved["resolved_id"])
                self.assertEqual(store.list_runs("proj-b3"), [])

    def test_execute_run_registry_overrides(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            created = ops.create_requested_config(
                store, "proj-b3",
                refund_requested(backend={"name": "b3-direct"}),
                policy, "b3-test")
            stored = store.get_requested("proj-b3",
                                         created["config_id"])
            requested = RequestedConfig.from_obj(
                stored["config"], valid_policy())
            policy_obj = Policy.from_obj(valid_policy())
            fake = FakeBackend(
                fake_adapters(),
                workspace_root=str(Path(directory) / "workspaces"))
            resolved = resolve_requested(
                requested, project_id="proj-b3",
                requested_id=created["config_id"],
                requested_digest=stored["digest"],
                requested_by="b3-test", policy=policy_obj,
                registry=registry, backends={"b3-direct": fake})
            saved = store.save_resolved(
                project_id="proj-b3",
                requested_id=created["config_id"],
                body=resolved.body(), digest=resolved.body()["digest"],
                created_at=resolved.body()["resolved_at"])
            receipts = JobReceiptStore(
                Path(directory) / "direct-receipts.sqlite")
            run = execute_run(
                store, registry, project_id="proj-b3",
                resolved_id=saved["resolved_id"],
                backends={"b3-direct": fake}, receipt_store=receipts)
            self.assertEqual(run["run"]["status"], "completed")
            self.assertEqual(run["run"]["backend"]["name"], "b3-direct")
            receipt = receipts.get_receipt(
                "proj-b3", run["run"]["backend"]["job_id"])
            self.assertEqual(receipt["state"], "completed")

    def test_registry_validation(self):
        with self.assertRaises(ValueError):
            register_backend("Bad Name", FakeBackend({}))
        with self.assertRaises(ValueError):
            register_backend("trusted-local", FakeBackend({}))
        with self.assertRaises(ValueError):
            register_backend("b3-bad-surface", object())
        with self.assertRaises(ValueError):
            unregister_backend("b3-never-there")
        with self.assertRaises(ConfigError):
            get_backend("b3-never-there")
        fake = FakeBackend({})
        with swapped_backend("b3-roundtrip", fake):
            self.assertIs(get_backend("b3-roundtrip"), fake)
            with self.assertRaises(ValueError):
                register_backend("b3-roundtrip", FakeBackend({}))
        with self.assertRaises(ConfigError):
            get_backend("b3-roundtrip")


class RealBackendRunTests(unittest.TestCase):
    def test_local_refund_run_completes_with_receipts(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            registration = register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            backend = LocalBackend(Path(directory) / "workspaces")
            with swapped_backend("local", backend):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={"name": "local"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                self.assertEqual(
                    resolved["resolved"]["pins"]["backend"],
                    {"name": "local",
                     "backend_version": "execution-backend/1",
                     "package_kind": "checked-in",
                     "package_identity":
                     registration["code_digest"]})
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["model_calls"], 0)
            self.assertEqual(body["tool_calls"], 3)
            self.assertIn("backend", body)
            link = body["backend"]
            self.assertEqual(link["name"], "local")
            self.assertTrue(link["job_id"].startswith("job-"))
            self.assertEqual(link["backend_version"],
                             "execution-backend/1")
            self.assertEqual(link["state"], "completed")
            self.assertIsNone(link["termination"])
            self.assertFalse(link["truncated"])
            self.assertFalse(link["missing_evidence"])
            self.assertGreater(link["output_bytes"], 0)
            self.assertGreater(link["events"], 0)
            self.assertGreater(link["commands"], 0)
            self.assertEqual(
                store.budget_used("proj-b3", resolved["resolved_id"],
                                  "tool_calls"), 3)
            sibling = Path(store.db_path).with_name(
                Path(store.db_path).stem + "-job-receipts"
                + Path(store.db_path).suffix)
            self.assertTrue(sibling.exists())
            receipts = JobReceiptStore(sibling)
            receipt = receipts.get_receipt("proj-b3", link["job_id"])
            self.assertEqual(receipt["state"], "completed")
            self.assertIsNone(receipt["resolution"])
            self.assertEqual(
                [h["state"] for h in receipt["history"]],
                ["prepared", "starting", "running", "completed"])
            self.assertTrue(receipt["event_digests"])
            self.assertGreater(len(receipt["message_digests"]),
                               len(receipt["event_digests"]))
            self.assertIn("evidence_digest",
                          receipt["observed"])
            for section in ("prepare", "hello", "status", "close"):
                self.assertIn(section, receipt["claimed"])
            # The real backend reports no workspace path, so none is
            # recorded (sweeping stays best-effort for recorded ones).
            self.assertIsNone(receipt["workspace"])
            # Tamper evidence: the receipt digest covers the receipt.
            digest = receipt["receipt_digest"]
            replay = {k: v for k, v in receipt.items()
                      if k != "receipt_digest"}
            self.assertEqual(canonical_digest(replay), digest)
            # Equivalence with the identical trusted-local run.
            created = ops.create_requested_config(
                store, "proj-b3", refund_requested(), policy,
                "b3-test")
            resolved = ops.resolve_config(
                store, registry, "proj-b3", created["config_id"],
                policy, "b3-test")
            local = ops.start_run(store, registry, "proj-b3",
                                  resolved["resolved_id"])["run"]
            self.assertEqual(body["verdict"], local["verdict"])
            self.assertEqual(body["evidence"], local["evidence"])
            self.assertEqual(body["checks"], local["checks"])
            self.assertEqual(body["tool_calls"], local["tool_calls"])

    def test_preset_cancel_real_backend(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            backend = LocalBackend(Path(directory) / "workspaces")
            with swapped_backend("local", backend):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={"name": "local"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                cancel = threading.Event()
                cancel.set()
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"],
                                    cancel=cancel)
            body = run["run"]
            self.assertEqual(body["status"], "cancelled")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertEqual(body["tool_calls"], 0)
            # The reset state was collected before teardown, mirroring
            # the in-process preset-cancel shape.
            self.assertIsInstance(body["evidence"]["snapshot"], dict)
            self.assertEqual(body["evidence"]["trace"], [])
            self.assertFalse(body["backend"]["missing_evidence"])
            self.assertEqual(
                body["backend"]["termination"]["reason"], "cancelled")
            self.assertEqual(body["backend"]["state"], "cancelled")


class FakeBackendLifecycleTests(unittest.TestCase):
    def test_booking_run_completes_with_receipts(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_booking(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(
                fake_adapters(),
                workspace_root=str(Path(directory) / "workspaces"))
            with swapped_backend("local", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    booking_requested(backend={"name": "local"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                self.assertEqual(
                    resolved["resolved"]["pins"]["backend"]["name"],
                    "local")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["model_calls"], 0)
            self.assertEqual(body["tool_calls"], 3)
            link = body["backend"]
            self.assertEqual(link["name"], "local")
            self.assertEqual(link["state"], "completed")
            self.assertIsNone(link["termination"])
            self.assertFalse(link["truncated"])
            self.assertFalse(link["missing_evidence"])
            sibling = Path(store.db_path).with_name(
                Path(store.db_path).stem + "-job-receipts"
                + Path(store.db_path).suffix)
            receipts = JobReceiptStore(sibling)
            receipt = receipts.get_receipt("proj-b3", link["job_id"])
            self.assertEqual(receipt["state"], "completed")
            self.assertEqual(
                [h["state"] for h in receipt["history"]],
                ["prepared", "starting", "running", "completed"])
            self.assertTrue(receipt["event_digests"])
            self.assertIn("evidence_digest", receipt["observed"])
            # The fake reports its workspace; the backend removed it on
            # close while the receipt keeps the path for audit.
            self.assertTrue(receipt["workspace"].endswith(link["job_id"]))
            self.assertFalse(Path(receipt["workspace"]).exists())
            # Equivalence with the identical trusted-local booking run.
            created = ops.create_requested_config(
                store, "proj-b3", booking_requested(), policy,
                "b3-test")
            resolved = ops.resolve_config(
                store, registry, "proj-b3", created["config_id"],
                policy, "b3-test")
            local = ops.start_run(store, registry, "proj-b3",
                                  resolved["resolved_id"])["run"]
            self.assertEqual(body["verdict"], local["verdict"])
            self.assertEqual(body["evidence"], local["evidence"])
            self.assertEqual(body["checks"], local["checks"])
            self.assertEqual(body["tool_calls"], local["tool_calls"])

    def test_preset_cancel_backend_run(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters())
            with swapped_backend("b3-cancel", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={"name": "b3-cancel"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                cancel = threading.Event()
                cancel.set()
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"],
                                    cancel=cancel)
            body = run["run"]
            self.assertEqual(body["status"], "cancelled")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertEqual(body["tool_calls"], 0)
            self.assertIsInstance(body["evidence"]["snapshot"], dict)
            self.assertEqual(body["evidence"]["trace"], [])
            self.assertFalse(body["backend"]["missing_evidence"])
            self.assertEqual(body["backend"]["state"], "cancelled")
            self.assertEqual(
                body["backend"]["termination"]["reason"], "cancelled")
            self.assertEqual(
                store.budget_used("proj-b3", resolved["resolved_id"],
                                  "tool_calls"), 0)

    def test_midrun_cancel_terminates_with_missing_evidence(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            cancel = threading.Event()

            def hook(count, tool):
                del tool
                if count >= 2:
                    cancel.set()

            fake = FakeBackend(fake_adapters(), on_invoke=hook)
            with swapped_backend("b3-midcancel", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(
                        backend={"name": "b3-midcancel"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"],
                                    cancel=cancel)
            body = run["run"]
            self.assertEqual(body["status"], "cancelled")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertEqual(body["tool_calls"], 2)
            # The killed job serves no evidence: the incompleteness is
            # preserved as missing-evidence markers, never fabricated.
            self.assertEqual(body["evidence"],
                             {"snapshot": None, "trace": None})
            self.assertTrue(body["backend"]["missing_evidence"])
            self.assertEqual(body["backend"]["state"], "cancelled")
            self.assertEqual(
                body["backend"]["termination"]["reason"], "cancelled")
            self.assertEqual(
                store.budget_used("proj-b3", resolved["resolved_id"],
                                  "tool_calls"), 2)
            sibling = Path(store.db_path).with_name(
                Path(store.db_path).stem + "-job-receipts"
                + Path(store.db_path).suffix)
            receipt = JobReceiptStore(sibling).get_receipt(
                "proj-b3", body["backend"]["job_id"])
            self.assertEqual(receipt["state"], "cancelled")
            self.assertEqual(
                receipt["observed"]["termination"]["reason"],
                "cancelled")

    def test_cancel_failure_still_records_cancelled_run(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            cancel = threading.Event()

            def hook(count, tool):
                del tool
                if count >= 1:
                    cancel.set()

            fake = FakeBackend(fake_adapters(), on_invoke=hook,
                               cancel_fails=True)
            with swapped_backend("b3-cancel-fail", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(
                        backend={"name": "b3-cancel-fail"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"],
                                    cancel=cancel)
            body = run["run"]
            self.assertEqual(body["status"], "cancelled")
            self.assertEqual(body["tool_calls"], 1)
            self.assertFalse(
                body["backend"]["termination"]["confirmed"])
            self.assertEqual(
                body["backend"]["termination"]["reason"], "cancelled")

    def test_wall_exhaustion_mapped(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(), hang=True)
            with swapped_backend("b3-wall", fake):
                requested = refund_requested(
                    backend={"name": "b3-wall"})
                requested["limits"]["max_wall_clock_ms"] = 120
                created = ops.create_requested_config(
                    store, "proj-b3", requested, policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "budget_exhausted")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertEqual(body["tool_calls"], 0)
            self.assertEqual(body["evidence"],
                             {"snapshot": None, "trace": None})
            self.assertTrue(body["backend"]["missing_evidence"])
            self.assertFalse(body["backend"]["truncated"])
            self.assertEqual(
                body["backend"]["termination"]["reason"],
                "wall_exhausted")
            self.assertEqual(body["backend"]["state"], "cancelled")

    def test_backend_timeout_mapped(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(), timed_out_on_poll=2)
            with swapped_backend("b3-timeout", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(
                        backend={"name": "b3-timeout"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "budget_exhausted")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertEqual(body["backend"]["state"], "timed_out")
            self.assertEqual(
                body["backend"]["termination"]["reason"],
                "backend_timed_out")
            sibling = Path(store.db_path).with_name(
                Path(store.db_path).stem + "-job-receipts"
                + Path(store.db_path).suffix)
            receipt = JobReceiptStore(sibling).get_receipt(
                "proj-b3", body["backend"]["job_id"])
            self.assertEqual(receipt["state"], "timed_out")
            self.assertTrue(receipt["observed"]["backend_timed_out"])

    def test_truncated_event_mapped(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(), truncate_on_invoke=2)
            with swapped_backend("b3-trunc", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={"name": "b3-trunc"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "budget_exhausted")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertTrue(body["backend"]["truncated"])
            self.assertEqual(
                body["backend"]["termination"]["reason"],
                "output_truncated")

    def test_output_exhausted_failure_mapped(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(),
                               fail_exhausted_on_invoke=1)
            with swapped_backend("b3-exhaust", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(
                        backend={"name": "b3-exhaust"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "budget_exhausted")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertTrue(body["backend"]["truncated"])

    def test_controller_output_cap_mapped(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(),
                               result_pad_bytes=20_000)
            with swapped_backend("b3-cap", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={"name": "b3-cap"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                with mock.patch(
                        "ghostlab.backends.receipts"
                        ".MAX_JOB_OUTPUT_BYTES", 40_000):
                    run = ops.start_run(store, registry, "proj-b3",
                                        resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "budget_exhausted")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertTrue(body["backend"]["truncated"])
            self.assertGreater(body["backend"]["output_bytes"], 40_000)

    def test_evidence_digest_mismatch_is_error(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(), lie_digest=True)
            with swapped_backend("b3-liar", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={"name": "b3-liar"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "error")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertIn("mismatch", body["error"])
            self.assertTrue(body["backend"]["missing_evidence"])
            sibling = Path(store.db_path).with_name(
                Path(store.db_path).stem + "-job-receipts"
                + Path(store.db_path).suffix)
            receipt = JobReceiptStore(sibling).get_receipt(
                "proj-b3", body["backend"]["job_id"])
            self.assertTrue(receipt["observed"]["evidence_mismatch"])
            self.assertEqual(receipt["claimed"]["evidence_digest"],
                             "0" * 64)
            self.assertNotEqual(
                receipt["observed"]["evidence_digest"],
                receipt["claimed"]["evidence_digest"])

    def test_forged_job_id_is_error(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy()
            fake = FakeBackend(fake_adapters(), forge_job=True)
            with swapped_backend("b3-forge", fake):
                created = ops.create_requested_config(
                    store, "proj-b3",
                    refund_requested(backend={"name": "b3-forge"}),
                    policy, "b3-test")
                resolved = ops.resolve_config(
                    store, registry, "proj-b3", created["config_id"],
                    policy, "b3-test")
                run = ops.start_run(store, registry, "proj-b3",
                                    resolved["resolved_id"])
            body = run["run"]
            self.assertEqual(body["status"], "error")
            self.assertEqual(body["verdict"], "incomplete")
            self.assertIn("does not match", body["error"])
            self.assertEqual(
                body["backend"]["termination"]["reason"],
                "protocol_violation")


class ReceiptStoreTests(unittest.TestCase):
    def test_reconcile_marks_orphans_and_sweeps_workspaces(self):
        with temp_dir() as directory:
            receipts = JobReceiptStore(Path(directory) / "jobs.sqlite")
            package = {"kind": "checked-in", "identity": "abc"}
            orphan = str(Path(directory) / "orphan-ws")
            Path(orphan).mkdir()
            Path(orphan, ".ghostlab-job.json").write_text(
                json.dumps({"project_id": "proj-b3",
                            "job_id": "job-orphan"}),
                encoding="utf-8")
            bare = str(Path(directory) / "bare-ws")
            Path(bare).mkdir()
            wrong = str(Path(directory) / "wrong-ws")
            Path(wrong).mkdir()
            Path(wrong, ".ghostlab-job.json").write_text(
                json.dumps({"project_id": "proj-b3",
                            "job_id": "job-other"}),
                encoding="utf-8")
            done = str(Path(directory) / "done-ws")
            Path(done).mkdir()
            Path(done, ".ghostlab-job.json").write_text(
                json.dumps({"project_id": "proj-b3",
                            "job_id": "job-done"}),
                encoding="utf-8")
            receipts.create_job(
                project_id="proj-b3", job_id="job-orphan",
                backend="local", backend_version="execution-backend/1",
                package=package, workspace=orphan)
            receipts.record_state("proj-b3", "job-orphan", "running",
                                  note="test")
            receipts.create_job(
                project_id="proj-b3", job_id="job-bare",
                backend="local", backend_version="execution-backend/1",
                package=package, workspace=bare)
            receipts.create_job(
                project_id="proj-b3", job_id="job-wrong",
                backend="local", backend_version="execution-backend/1",
                package=package, workspace=wrong)
            receipts.record_state("proj-b3", "job-wrong", "running",
                                  note="test")
            receipts.create_job(
                project_id="proj-b3", job_id="job-done",
                backend="local", backend_version="execution-backend/1",
                package=package, workspace=done)
            receipts.close_job("proj-b3", "job-done", "completed")
            receipts.create_job(
                project_id="proj-b3", job_id="job-nowhere",
                backend="local", backend_version="execution-backend/1",
                package=package)
            receipts.record_state("proj-b3", "job-nowhere", "running",
                                  note="test")
            result = receipts.reconcile()
            self.assertEqual(
                {item["job_id"] for item in result["orphaned"]},
                {"job-orphan", "job-bare", "job-wrong", "job-nowhere"})
            self.assertEqual(result["swept"], [orphan])
            self.assertFalse(Path(orphan).exists())
            self.assertTrue(Path(bare).exists())
            self.assertTrue(Path(wrong).exists())
            self.assertTrue(Path(done).exists())
            for job_id in ("job-orphan", "job-bare", "job-wrong",
                           "job-nowhere"):
                receipt = receipts.get_receipt("proj-b3", job_id)
                self.assertTrue(
                    receipt["resolution"].startswith(
                        "controller_restarted:unresolved"),
                    receipt["resolution"])
                self.assertIn("no auto-retry",
                              receipt["history"][-1]["note"])
            # No retry: in-flight states are untouched, terminal jobs
            # are never marked.
            self.assertEqual(
                receipts.get_receipt(
                    "proj-b3", "job-orphan")["state"], "running")
            self.assertEqual(
                receipts.get_receipt(
                    "proj-b3", "job-bare")["state"], "prepared")
            done_receipt = receipts.get_receipt("proj-b3", "job-done")
            self.assertIsNone(done_receipt["resolution"])
            again = receipts.reconcile()
            self.assertEqual(again, {"orphaned": [], "swept": []})

    def test_cross_project_job_ops_refused(self):
        with temp_dir() as directory:
            receipts = JobReceiptStore(Path(directory) / "jobs.sqlite")
            receipts.create_job(
                project_id="proj-a", job_id="job-owned",
                backend="local", backend_version="execution-backend/1",
                package={"kind": "checked-in", "identity": "abc"})
            # The backend is None: any call reaching past the project
            # check would crash instead of refusing.
            foreign = JobHandle(receipts, None, "proj-b", "job-owned")
            with self.assertRaises(CrossProjectError):
                foreign.send({"type": "hello"})
            with self.assertRaises(CrossProjectError):
                foreign.poll(0)
            with self.assertRaises(CrossProjectError):
                foreign.status()
            with self.assertRaises(CrossProjectError):
                foreign.cancel()
            with self.assertRaises(CrossProjectError):
                foreign.close()
            with self.assertRaises(CrossProjectError):
                receipts.require_job("proj-b", "job-owned")
            with self.assertRaises(CrossProjectError):
                receipts.get_receipt("proj-b", "job-owned")
            self.assertEqual(receipts.list_receipts("proj-b"), [])
            with self.assertRaises(ValueError):
                receipts.require_job("proj-a", "job-missing")
            # The receipt error is the store's project error: one class.
            self.assertIs(ReceiptCrossProjectError, CrossProjectError)
            # Positive control: the owning project passes the check
            # (the fake then refuses the unknown-to-it job itself).
            own = JobHandle(receipts, FakeBackend({}), "proj-a",
                            "job-owned")
            with self.assertRaises(BackendError):
                own.poll(0)

    def test_terminal_states_are_final(self):
        with temp_dir() as directory:
            receipts = JobReceiptStore(Path(directory) / "jobs.sqlite")
            receipts.create_job(
                project_id="proj-b3", job_id="job-term",
                backend="local", backend_version="execution-backend/1",
                package={"kind": "checked-in", "identity": "abc"})
            receipts.close_job("proj-b3", "job-term", "completed")
            with self.assertRaises(ValueError):
                receipts.record_state("proj-b3", "job-term", "running")
            with self.assertRaises(ValueError):
                receipts.close_job("proj-b3", "job-term", "running")

    def test_wire_validation(self):
        good_command = {"type": "invoke",
                        "payload": {"tool": "t", "arguments": {}}}
        self.assertEqual(validate_command(good_command), good_command)
        self.assertEqual(validate_command({"type": "hello"}),
                         {"type": "hello", "payload": {}})
        for bad in ({"type": "nope"}, {"type": "hello", "zzz": 1},
                    {"type": "hello", "payload": []}, {"payload": {}},
                    "hello"):
            with self.subTest(command=bad):
                with self.assertRaises(ProtocolError):
                    validate_command(bad)
        good_event = {"v": 1, "job_id": "job-1", "seq": 4,
                      "type": "tool_result",
                      "payload": {"ok": True}}
        validate_envelope(good_event, job_id="job-1",
                          direction="event", expected_seq=4)
        with self.assertRaises(ProtocolError):
            validate_envelope(dict(good_event, seq=5), job_id="job-1",
                              direction="event", expected_seq=4)
        with self.assertRaises(ProtocolError):
            validate_envelope(dict(good_event, type="nope"),
                              job_id="job-1", direction="event")
        from ghostlab.backends.receipts import CrossJobError
        with self.assertRaises(CrossJobError):
            validate_envelope(dict(good_event, job_id="job-2"),
                              job_id="job-1", direction="event")


class LegacyCompatTests(unittest.TestCase):
    def test_legacy_resolved_without_pin_executes_trusted_local(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            registration = register_refund(registry)
            store = make_store(directory)
            body = {
                "schema_version": 1,
                "resolved_config_id": "res-b3-legacy",
                "project_id": "proj-b3",
                "requested_config_id": "req-b3-legacy",
                "requested_digest": "0" * 64,
                "resolved_at": "2026-11-03T00:00:00Z",
                "resolved_by": "b3-test",
                "pins": {"env_id": registration["env_id"],
                         "env_version": registration["env_version"],
                         "adapter_ref": registration["adapter_ref"],
                         "code_digest": registration["code_digest"],
                         "manifest_digest": None,
                         "world_version": "refund-world/1",
                         "adapter_version": "refund-adapters/1",
                         "controller_version": "experiment-controller/1",
                         "discovered_tools": deepcopy(REFUND_TOOLS),
                         "discovered_modes": list(REFUND_MODES)},
                "effective": {
                    "agent": "vulnerable",
                    "fixture": dict(REFUND_FIXTURE), "faults": [],
                    "rules": list(REFUND_IDS),
                    "limits": {"max_tool_calls": 20,
                               "max_total_tool_calls": 200,
                               "max_wall_clock_ms": 60000},
                    "repetitions": {"count": 1}, "seeds": [0]},
                "provenance": {"policy_version": "policy/1",
                               "requested_by": "b3-test",
                               "requested_digest": "0" * 64,
                               "resolved_by": "b3-test",
                               "discovery_match": True},
            }
            body["digest"] = canonical_digest(
                {k: v for k, v in body.items() if k != "digest"})
            from ghostlab.experiments.configs import resolved_from_dict
            resolved_from_dict(deepcopy(body))
            saved = store.save_resolved(
                project_id="proj-b3", requested_id="req-b3-legacy",
                body=body, digest=body["digest"],
                created_at=body["resolved_at"])
            run = ops.start_run(store, registry, "proj-b3",
                                saved["resolved_id"])
            self.assertEqual(run["run"]["status"], "completed")
            self.assertNotIn("backend", run["run"])

    def test_old_run_records_still_readable(self):
        with temp_dir() as directory:
            store = make_store(directory)
            body = {"schema_version": 1, "project_id": "proj-b3",
                    "resolved_id": "res-old", "repetition": 0,
                    "attempt": 0, "seed": 0, "status": "completed",
                    "verdict": "pass", "model_calls": 0,
                    "tool_calls": 2, "wall_clock_ms": 5,
                    "faults": [], "evidence": {"snapshot": {},
                                               "trace": []},
                    "checks": {}, "rules_evaluated": [],
                    "rules_failed": [], "error": None,
                    "started_at": "2026-11-03T00:00:00Z",
                    "completed_at": "2026-11-03T00:00:01Z"}
            saved = store.save_run(
                project_id="proj-b3", resolved_id="res-old",
                repetition=0, attempt=0, body=body,
                created_at=body["completed_at"])
            fetched = store.get_run("proj-b3", saved["run_id"])
            self.assertEqual(fetched["run"]["verdict"], "pass")
            listed = store.list_runs("proj-b3", "res-old")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["digest"], fetched["digest"])


if __name__ == "__main__":
    unittest.main()
