"""B5 backend acceptance: booking example through the backend seam.

Executable end-to-end acceptance (exit 0 iff every stage passes)::

    python examples/equipment_booking/backend_acceptance.py

Stages: env qualification, a trusted-local baseline run, the same booking
scenario executed through the controller backend registry
(``register_backend``/``get_backend``) on an acceptance backend, refusal of
unknown and unavailable backends with no fallback and no run record,
qualification-status honesty for the real local/docker backends,
cancellation, evidence/receipt identity, and a final zero-model-call
determinism check. One ``PASS``/``FAIL`` line is printed per stage.

The acceptance backend below is an in-process test double that mirrors
the worker/local wire semantics (unframed ``{type, payload}`` commands,
enveloped events, single inline evidence chunk). It executes the checked-in
booking adapter in-process, provides NO isolation, and is NEVER a sandbox;
it exists only to drive the real controller seam (pin validation, job
receipts, observed-vs-claimed separation, cancellation and evidence
digests) from this example. The real backends are exercised directly for
capability honesty: ``LocalBackend`` must report ``unqualified`` and
``DockerBackend`` must report ``blocked-no-daemon`` or ``unqualified``,
never ``qualified`` without executed evidence.

Zero model calls, no secrets, stdlib first. All databases and workspaces
live under one track-private temp directory.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import sys
import tempfile
import threading
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from examples.equipment_booking import driver_booking  # noqa: E402
from examples.equipment_booking.env_adapter import (  # noqa: E402
    BOOKING_MODES, BOOKING_TOOLS, BookingEnvAdapter)
from ghostlab.backends import protocol as backend_protocol  # noqa: E402
from ghostlab.backends.docker import DockerBackend  # noqa: E402
from ghostlab.backends.interface import (  # noqa: E402
    BackendError, BackendUnavailableError, JobSpec)
from ghostlab.backends.local import LocalBackend  # noqa: E402
from ghostlab.backends.receipts import JobReceiptStore  # noqa: E402
from ghostlab.envs.qualification import run_qualification  # noqa: E402
from ghostlab.envs.registry import EnvRegistry  # noqa: E402
from ghostlab.experiments import checks as check_registry  # noqa: E402
from ghostlab.experiments import drivers as driver_registry  # noqa: E402
from ghostlab.experiments import ops  # noqa: E402
from ghostlab.experiments.checks import CheckError  # noqa: E402
from ghostlab.experiments.configs import (  # noqa: E402
    ConfigError, canonical_digest)
from ghostlab.experiments.controller import (  # noqa: E402
    get_backend, register_backend, registered_backends,
    unregister_backend)
from ghostlab.experiments.store import ExperimentStore  # noqa: E402

PROJECT_ID = "proj-b5-accept"
BOOKING_REF = "examples.equipment_booking.env_adapter:BookingEnvAdapter"
ENV_ID = "booking-env"
BACKEND_NAME = "b5-accept"
UNAVAIL_NAME = "b5-unavail"
UNKNOWN_NAME = "b5-ghost-unknown"
BOOKING_FIXTURE = {"equipment_id": "synthetic-microscope-1",
                   "user_id": "synthetic-user-1",
                   "slot": "2026-11-03T09:00Z"}
BOOKING_RULES = list(driver_booking.CHECK_IDS)
ALLOWLIST = ("ghostlab.", "examples.equipment_booking.")


def _policy() -> dict:
    return {"policy_version": "policy/1", "ceiling_tool_calls": 20,
            "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
            "ceiling_wall_clock_ms": 60000,
            "allowed_agents": ["vulnerable", "repaired"],
            "allowed_hooks": ["timeout_after_commit"]}


def _requested(name: str, agent: str, backend=None) -> dict:
    body = {"config_version": 1, "name": name, "env_id": ENV_ID,
            "agent": agent, "fixture": dict(BOOKING_FIXTURE),
            "faults": [{"hook": "timeout_after_commit", "params": {},
                        "required": True}],
            "rules": list(BOOKING_RULES),
            "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                       "max_wall_clock_ms": 60000},
            "repetitions": {"count": 1}}
    if backend is not None:
        body["backend"] = backend
    return body


def _register_driver_and_checks() -> None:
    if driver_registry.get_driver(driver_booking.ENV_ID) is None:
        driver_registry.register_driver(driver_booking.ENV_ID,
                                        driver_booking)
    for spec in driver_booking.BOOKING_CHECK_SPECS:
        try:
            check_registry.get_check(spec["check_id"])
        except CheckError:
            check_registry.register_check(**spec)


class _AcceptJob:
    def __init__(self, spec: JobSpec) -> None:
        self.spec = spec
        self.adapter = None
        self.state = "starting"
        self.queue: list = []
        self.event_seq = 0
        self.output_bytes = 0
        self.hello_seen = False


class AcceptanceBackend:
    """In-process acceptance double (NOT a sandbox; see module docstring).

    Mirrors ``worker.py``/``local.py`` wire semantics so controller
    behavior transfers: ack ``tool_result`` for setup commands, one
    ``tool_result`` per invoke, a single inline ``evidence_chunk`` and
    ``completed`` on close, with gap-free event sequences from 0.
    """

    name = BACKEND_NAME
    version = "execution-backend/1"

    def __init__(self, *, unavailable: bool = False) -> None:
        self._unavailable = unavailable
        self._jobs: dict = {}
        self.calls: list = []

    def capabilities(self) -> dict:
        return {
            "isolation": {
                "filesystem": "none (test double; NOT a sandbox)",
                "process": "none (test double; NOT a sandbox)",
                "network": "none (test double; NOT a sandbox)"},
            "limits": ["wall_clock_ms", "max_output_bytes"],
            "qualification": {"status": "unqualified-test-double"},
        }

    def _require(self, job_id: str) -> _AcceptJob:
        if type(job_id) is not str:
            raise BackendError("unknown job")
        job = self._jobs.get(job_id)
        if job is None:
            raise BackendError(f"unknown job '{job_id}'")
        return job

    def prepare(self, package: dict) -> dict:
        self.calls.append(("prepare", deepcopy(package)))
        if self._unavailable:
            raise BackendUnavailableError(
                "acceptance backend down for this stage")
        if type(package) is not dict \
                or set(package) != {"kind", "identity"}:
            raise BackendError("package must be exactly {kind, identity}")
        if package["kind"] != "checked-in":
            raise BackendError(
                f"unsupported package kind {package['kind']!r}")
        identity = package["identity"]
        if type(identity) is not str or not identity or len(identity) > 256:
            raise BackendError("checked-in packages must pin an identity")
        return {"identity": identity,
                "provenance": {"kind": "checked-in", "test-double": True}}

    def start_job(self, spec) -> None:
        self.calls.append(("start_job", getattr(spec, "job_id", None)))
        if self._unavailable:
            raise BackendUnavailableError(
                "acceptance backend down for this stage")
        if type(spec) is not JobSpec:
            raise BackendError("spec must be a JobSpec")
        if spec.job_id in self._jobs:
            raise BackendError(f"duplicate job '{spec.job_id}'")
        if spec.package["kind"] != "checked-in":
            raise BackendError(
                f"unsupported package kind {spec.package['kind']!r}")
        self._jobs[spec.job_id] = _AcceptJob(spec)
        return None

    def _emit(self, job: _AcceptJob, event_type: str, payload: dict) -> None:
        try:
            raw = backend_protocol.encode_message(
                job.spec.job_id, job.event_seq, event_type, payload)
        except Exception:
            fallback = {"v": 1, "job_id": job.spec.job_id,
                        "seq": job.event_seq, "type": "truncated",
                        "payload": {"reason": "output-exhausted"}}
            job.event_seq += 1
            job.queue.append(fallback)
            job.state = "failed"
            return
        job.event_seq += 1
        job.output_bytes += len(raw) + 1
        job.queue.append(json.loads(raw.decode("utf-8")))

    @staticmethod
    def _tool_result(ok: bool, result=None, error=None, message=None) -> dict:
        payload: dict = {"ok": ok}
        if ok:
            if result is not None:
                payload["result"] = result
        else:
            payload["error"] = error or "AdapterError"
            payload["message"] = str(message or "")[:500]
        return payload

    def _fail_job(self, job: _AcceptJob, error: str, message: str) -> None:
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
                self._fail_job(job, "ProtocolViolation", "duplicate hello")
                return
            if job.spec.adapter_ref != BOOKING_REF:
                self._fail_job(job, "ValueError",
                               "adapter module is not in the allowlist")
                return
            try:
                job.adapter = BookingEnvAdapter()
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
            self._fail_job(job, "ProtocolViolation", "hello required first")
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
                    False, error=type(error).__name__, message=str(error)))
                return
            self._emit(job, "tool_result", self._tool_result(True))
            return
        if ctype == "invoke":
            tool, arguments = payload.get("tool"), payload.get("arguments")
            if type(tool) is not str or type(arguments) is not dict:
                self._fail_job(job, "ProtocolViolation",
                               "invoke needs tool+arguments")
                return
            try:
                outcome = job.adapter.invoke(tool, arguments)
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__, message=str(error)))
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
                    False, error=type(error).__name__, message=str(error)))
                return
            self._emit(job, "tool_result", self._tool_result(True))
            return
        if ctype == "clear_faults":
            try:
                job.adapter.clear_faults()
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__, message=str(error)))
                return
            self._emit(job, "tool_result", self._tool_result(True))
            return
        if ctype == "evidence":
            try:
                evidence = job.adapter.evidence()
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__, message=str(error)))
                return
            self._emit(job, "evidence_chunk", {"evidence": evidence})
            return
        if ctype == "snapshot":
            try:
                outcome = job.adapter.snapshot()
            except Exception as error:
                self._emit(job, "tool_result", self._tool_result(
                    False, error=type(error).__name__, message=str(error)))
                return
            self._emit(job, "tool_result",
                       self._tool_result(True, result=outcome))
            return
        if ctype == "close":
            self._emit(job, "completed", {})
            job.state = "completed"
            return
        self._fail_job(job, "UnknownCommand", f"unknown command '{ctype}'")

    def poll(self, job_id: str, timeout_ms: int = 0) -> list:
        job = self._require(job_id)
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise BackendError("timeout_ms must be a non-negative integer")
        pending, job.queue = job.queue, []
        return pending

    def status(self, job_id: str) -> dict:
        job = self._require(job_id)
        return {"job_id": job.spec.job_id, "state": job.state,
                "output_bytes": job.output_bytes, "elapsed_ms": 0}

    def cancel(self, job_id: str, grace_ms: int = 2000) -> dict:
        job = self._require(job_id)
        if type(grace_ms) is not int or grace_ms < 0:
            raise BackendError("grace_ms must be a non-negative integer")
        if job.state in ("completed", "failed", "cancelled", "timed_out"):
            return {"state": job.state, "confirmed": True}
        job.state = "cancelled"
        job.queue.clear()
        return {"state": "cancelled", "confirmed": True}

    def close(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job.state not in ("completed", "failed", "cancelled",
                             "timed_out"):
            job.state = "failed"
        self._jobs.pop(job_id, None)
        return {"job_id": job_id, "state": job.state,
                "receipts": [
                    {"kind": "lifecycle", "state": job.state,
                     "reason": "acceptance-double"},
                    {"kind": "output", "bytes": job.output_bytes,
                     "truncated": False}]}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _resolve_and_run(ctx: dict, name: str, agent: str, backend=None,
                     cancel=None) -> dict:
    """Create + resolve + execute one booking config; return the run body."""
    store, registry = ctx["store"], ctx["registry"]
    policy = ctx["policy"]
    created = ops.create_requested_config(
        store, PROJECT_ID, _requested(name, agent, backend), policy,
        "b5-acceptance")
    resolved = ops.resolve_config(
        store, registry, PROJECT_ID, created["config_id"], policy,
        "b5-acceptance")
    started = ops.start_run(store, registry, PROJECT_ID,
                            resolved["resolved_id"], cancel=cancel)
    ctx["runs"].append(started["run"])
    return started


def _stage_env_qualify(ctx: dict) -> str:
    _register_driver_and_checks()
    registry = EnvRegistry(Path(ctx["tmp"]) / "env.sqlite",
                           allowed_adapter_prefixes=ALLOWLIST)
    record = registry.register(
        env_id=ENV_ID, env_version="booking-env/1", adapter_ref=BOOKING_REF,
        display_name="Synthetic equipment booking",
        declared_tools=deepcopy(BOOKING_TOOLS),
        declared_modes=list(BOOKING_MODES), registered_by="b5-acceptance")
    report = run_qualification(registry, ENV_ID,
                               fixture=dict(BOOKING_FIXTURE))
    _check(report["status"] == "qualified-for-declared",
           f"booking env not qualified-for-declared: {report['status']}")
    ctx.update(registry=registry,
               store=ExperimentStore(Path(ctx["tmp"]) / "experiments.sqlite"),
               policy=_policy(), code_digest=record["code_digest"],
               runs=[])
    failed = [c["name"] for c in report["checks"] if not c["passed"]]
    _check(not failed, f"qualification checks failed: {failed}")
    return (f"env {ENV_ID} qualified-for-declared "
            f"({len(report['checks'])} checks, digest pinned)")


def _stage_trusted_local(ctx: dict) -> str:
    naive = _resolve_and_run(ctx, "booking b5 trusted naive",
                             "vulnerable")["run"]
    _check(naive["status"] == "completed",
           f"trusted naive status {naive['status']}")
    _check(naive["verdict"] == "fail",
           f"trusted naive verdict {naive['verdict']}")
    _check(naive["rules_failed"] == ["no_duplicate_booking"],
           f"trusted naive rules_failed {naive['rules_failed']}")
    _check(naive["model_calls"] == 0, "trusted naive made model calls")
    _check(naive["tool_calls"] == 3,
           f"trusted naive tool_calls {naive['tool_calls']}")
    _check(naive["faults"][0]["fired"] is True,
           "trusted naive fault did not fire")
    _check(naive["evidence"]["snapshot"]["total_bookings"] == 2,
           "trusted naive should duplicate to 2 bookings")
    _check("backend" not in naive,
           "trusted-local run body must not carry a backend link")
    fixed = _resolve_and_run(ctx, "booking b5 trusted repaired",
                             "repaired")["run"]
    _check(fixed["status"] == "completed" and fixed["verdict"] == "pass",
           f"trusted repaired {fixed['status']}/{fixed['verdict']}")
    _check(fixed["rules_failed"] == [] and fixed["model_calls"] == 0,
           "trusted repaired failed rules or made model calls")
    _check(fixed["evidence"]["snapshot"]["total_bookings"] == 1,
           "trusted repaired should hold exactly 1 booking")
    ctx["trusted_naive"] = naive
    return ("vulnerable completed/fail (duplicate caught, 2 bookings); "
            "repaired completed/pass (1 booking); model_calls=0")


def _stage_backend_seam(ctx: dict) -> str:
    started = _resolve_and_run(ctx, "booking b5 backend naive", "vulnerable",
                               backend={"name": BACKEND_NAME})
    body = started["run"]
    _check(body["status"] == "completed",
           f"backend run status {body['status']}")
    _check(body["model_calls"] == 0, "backend run made model calls")
    _check(body["tool_calls"] == 3,
           f"backend run tool_calls {body['tool_calls']}")
    link = body.get("backend")
    _check(type(link) is dict, "backend run lacks the backend link")
    _check(link["name"] == BACKEND_NAME, f"backend link {link}")
    _check(link["job_id"].startswith("job-"),
           f"backend job id {link['job_id']}")
    _check(link["backend_version"] == "execution-backend/1",
           f"backend version {link['backend_version']}")
    _check(link["state"] == "completed" and link["termination"] is None,
           f"backend link state {link}")
    _check(link["truncated"] is False and link["missing_evidence"] is False,
           f"backend link markers {link}")
    trusted = ctx["trusted_naive"]
    _check(body["verdict"] == trusted["verdict"]
           and body["evidence"] == trusted["evidence"]
           and body["checks"] == trusted["checks"]
           and body["tool_calls"] == trusted["tool_calls"],
           "backend evidence/verdict differs from trusted-local baseline")
    sibling = Path(ctx["store"].db_path).with_name(
        Path(ctx["store"].db_path).stem + "-job-receipts"
        + Path(ctx["store"].db_path).suffix)
    receipt = JobReceiptStore(sibling).get_receipt(PROJECT_ID,
                                                   link["job_id"])
    _check(receipt["state"] == "completed",
           f"receipt state {receipt['state']}")
    _check([h["state"] for h in receipt["history"]]
           == ["prepared", "starting", "running", "completed"],
           f"receipt history {receipt['history']}")
    ctx["backend_run"] = body
    ctx["receipt_path"] = sibling
    return (f"booking vulnerable via '{BACKEND_NAME}' completed/fail, "
            f"job {link['job_id']}, evidence identical to trusted-local")


def _stage_unknown_refusal(ctx: dict) -> str:
    store, registry, policy = ctx["store"], ctx["registry"], ctx["policy"]
    runs_before = len(store.list_runs(PROJECT_ID))
    resolved_before = len(store.list_resolved(PROJECT_ID))
    try:
        get_backend(UNKNOWN_NAME)
    except ConfigError:
        pass
    else:
        raise AssertionError("get_backend accepted an unknown backend")
    created = ops.create_requested_config(
        store, PROJECT_ID,
        _requested("booking b5 unknown backend", "vulnerable",
                   backend={"name": UNKNOWN_NAME}),
        policy, "b5-acceptance")
    try:
        ops.resolve_config(store, registry, PROJECT_ID,
                           created["config_id"], policy, "b5-acceptance")
    except ConfigError as error:
        _check("unknown backend" in str(error),
               f"unexpected resolve refusal: {error}")
    else:
        raise AssertionError("unknown backend resolved without refusal")
    _check(len(store.list_runs(PROJECT_ID)) == runs_before,
           "unknown-backend refusal created a run record (fallback?)")
    _check(len(store.list_resolved(PROJECT_ID)) == resolved_before,
           "unknown-backend refusal stored a resolved config")
    return ("resolve refused with ConfigError 'unknown backend'; "
            "no resolved config, no run record, no fallback")


def _stage_unavailable_refusal(ctx: dict) -> str:
    store, registry, policy = ctx["store"], ctx["registry"], ctx["policy"]
    created = ops.create_requested_config(
        store, PROJECT_ID,
        _requested("booking b5 unavailable backend", "vulnerable",
                   backend={"name": UNAVAIL_NAME}),
        policy, "b5-acceptance")
    resolved = ops.resolve_config(store, registry, PROJECT_ID,
                                  created["config_id"], policy,
                                  "b5-acceptance")
    pin = resolved["resolved"]["pins"]["backend"]
    _check(pin["name"] == UNAVAIL_NAME
           and pin["backend_version"] == "execution-backend/1",
           f"unavailable pin {pin}")
    runs_before = len(store.list_runs(PROJECT_ID))
    try:
        ops.start_run(store, registry, PROJECT_ID, resolved["resolved_id"])
    except BackendUnavailableError:
        pass
    else:
        raise AssertionError("unavailable backend executed without refusal")
    _check(len(store.list_runs(PROJECT_ID)) == runs_before,
           "unavailable-backend refusal created a run record (fallback?)")
    _check(store.list_runs(PROJECT_ID, resolved["resolved_id"]) == [],
           "unavailable-backend resolved id gained a run record")
    _check(store.budget_used(PROJECT_ID, resolved["resolved_id"],
                             "tool_calls") == 0,
           "unavailable-backend refusal consumed budget")
    receipts = JobReceiptStore(ctx["receipt_path"])
    jobs = [j for j in receipts.list_receipts(PROJECT_ID)
            if j["backend"] == UNAVAIL_NAME]
    _check(len(jobs) == 1, f"expected 1 refused job receipt, got {jobs}")
    receipt = receipts.get_receipt(PROJECT_ID, jobs[0]["job_id"])
    _check(receipt["state"] == "failed"
           and [h["state"] for h in receipt["history"]]
           == ["prepared", "failed"],
           f"refused receipt {receipt['state']} {receipt['history']}")
    ctx["unavailable_resolved"] = resolved["resolved_id"]
    return ("resolve pinned without probing; start refused with "
            "BackendUnavailableError (HTTP 503 at the API layer); "
            "no run record, no budget, receipt prepared->failed")


def _stage_qualification_honesty(ctx: dict) -> str:
    tmp = Path(ctx["tmp"])
    local = LocalBackend(tmp / "local-workspaces")
    caps = local.capabilities()
    _check(caps["qualification"] == {"status": "unqualified"},
           f"local qualification {caps['qualification']}")
    _check(caps["isolation"] == {"filesystem": "none",
                                 "process": "os-process",
                                 "network": "host"},
           f"local isolation {caps['isolation']}")
    _check("wall_clock_ms" in caps["limits"]
           and "max_output_bytes" in caps["limits"],
           f"local limits {caps['limits']}")
    docker = DockerBackend(tmp / "docker-workspaces")
    dcaps = docker.capabilities()
    status = dcaps["qualification"].get("status")
    _check(status in ("blocked-no-daemon", "unqualified"),
           f"docker qualification {dcaps['qualification']}")
    _check(status != "qualified",
           "docker claimed qualified without executed evidence")
    _check(dcaps["isolation"]["network"] == "none",
           f"docker network posture {dcaps['isolation']}")
    _check("requires daemon" in dcaps["isolation"]["filesystem"],
           "docker filesystem claim lacks the daemon qualifier")
    double_caps = AcceptanceBackend().capabilities()
    _check(double_caps["qualification"]["status"]
           == "unqualified-test-double"
           and "NOT a sandbox" in double_caps["isolation"]["filesystem"],
           f"acceptance double caps {double_caps}")
    try:
        from ghostlab.api.app import _backend_catalog, _select_backend
        from ghostlab.api.app import BackendUnavailableError as ApiDown
    except Exception:
        return (f"local unqualified (fs none/host net); docker {status} "
                "(no qualified claim); API catalog unavailable, "
                "controller checks only")
    catalog = {item["name"]: item for item in _backend_catalog()}
    _check(catalog["trusted-local"]["qualification"]
           ["qualified_for_isolation"] is False,
           "API trusted-local claimed isolation qualification")
    _check(catalog["linux-container"]["available"] is False,
           "API linux-container claimed availability")
    try:
        _select_backend("linux-container")
    except ApiDown:
        pass
    else:
        raise AssertionError("API selected an unavailable backend")
    return (f"local unqualified (fs none/host net); docker {status} "
            "(no qualified claim); API trusted-local not isolation-"
            "qualified, linux-container unavailable -> 503, no fallback")


def _stage_cancellation(ctx: dict) -> str:
    cancel = threading.Event()
    cancel.set()
    started = _resolve_and_run(ctx, "booking b5 backend cancel",
                               "vulnerable", backend={"name": BACKEND_NAME},
                               cancel=cancel)
    body = started["run"]
    _check(body["status"] == "cancelled" and body["verdict"] == "incomplete",
           f"cancelled run {body['status']}/{body['verdict']}")
    _check(body["tool_calls"] == 0 and body["model_calls"] == 0,
           f"cancelled run calls {body['tool_calls']}")
    _check(body["evidence"]["trace"] == []
           and type(body["evidence"]["snapshot"]) is dict,
           "cancelled run should keep the reset snapshot, empty trace")
    link = body["backend"]
    _check(link["state"] == "cancelled"
           and link["termination"]["reason"] == "cancelled"
           and link["missing_evidence"] is False,
           f"cancelled link {link}")
    receipt = JobReceiptStore(ctx["receipt_path"]).get_receipt(
        PROJECT_ID, link["job_id"])
    _check(receipt["state"] == "cancelled",
           f"cancelled receipt {receipt['state']}")
    # Direct ExecutionBackend lifecycle: hello, then cancel + close.
    direct = AcceptanceBackend()
    package = {"kind": "checked-in", "identity": ctx["code_digest"]}
    _check(direct.prepare(package)["identity"] == ctx["code_digest"],
           "direct prepare pinned a different identity")
    spec = JobSpec(job_id="job-b5-direct-cancel", project_id=PROJECT_ID,
                   package=dict(package), adapter_ref=BOOKING_REF,
                   fixture=dict(BOOKING_FIXTURE),
                   limits={"wall_clock_ms": 60000,
                           "max_output_bytes": 1000000},
                   no_network=True)
    direct.start_job(spec)
    direct.send(spec.job_id, {"type": "hello", "payload": {}})
    events = direct.poll(spec.job_id, 0)
    _check([e["type"] for e in events] == ["started"],
           f"direct hello events {events}")
    ack = direct.cancel(spec.job_id, 2000)
    _check(ack == {"state": "cancelled", "confirmed": True},
           f"direct cancel ack {ack}")
    _check(direct.status(spec.job_id)["state"] == "cancelled",
           "direct status not cancelled after cancel")
    closed = direct.close(spec.job_id)
    _check(closed["state"] == "cancelled" and closed["receipts"],
           f"direct close {closed}")
    return ("controller preset-cancel: cancelled/incomplete, 0 calls, "
            "receipt cancelled; direct cancel confirmed + closed")


def _stage_evidence_identity(ctx: dict) -> str:
    body = ctx["backend_run"]
    job_id = body["backend"]["job_id"]
    receipts = JobReceiptStore(ctx["receipt_path"])
    receipt = receipts.get_receipt(PROJECT_ID, job_id)
    _check(receipt["job_id"] == job_id
           and receipt["project_id"] == PROJECT_ID,
           "receipt identity does not match the run backend link")
    _check(receipt["backend"] == BACKEND_NAME
           and receipt["backend_version"] == "execution-backend/1",
           f"receipt backend {receipt['backend']}")
    _check(receipt["package"] == {"kind": "checked-in",
                                  "identity": ctx["code_digest"]},
           f"receipt package {receipt['package']}")
    raw = json.dumps(body["evidence"], sort_keys=True,
                     separators=(",", ":"), ensure_ascii=True,
                     allow_nan=False).encode("utf-8")
    observed = hashlib.sha256(raw).hexdigest()
    _check(receipt["observed"].get("evidence_digest") == observed,
           "observed evidence digest does not match the run evidence")
    for section in ("prepare", "hello", "status", "close"):
        _check(section in receipt["claimed"],
               f"claimed facts lack {section}")
    _check("evidence_digest" not in receipt["claimed"]
           or receipt["claimed"].get("evidence_digest") == observed,
           "claimed evidence digest was presented as observed truth")
    replay = {k: v for k, v in receipt.items() if k != "receipt_digest"}
    _check(canonical_digest(replay) == receipt["receipt_digest"],
           "receipt digest does not verify (tamper evidence broken)")
    _check(receipt["event_digests"] and len(receipt["message_digests"])
           > len(receipt["event_digests"]),
           "receipt message digests incomplete")
    try:
        receipts.get_receipt("proj-b5-other", job_id)
    except PermissionError:
        pass
    else:
        raise AssertionError("cross-project receipt read was allowed")
    stored = ctx["store"].get_run(PROJECT_ID, body["run_id"])
    _check(stored["run"]["digest"] == body["digest"],
           "stored run digest changed on read-back")
    snapshot = body["evidence"]["snapshot"]
    _check(snapshot["total_bookings"] == len(snapshot["reservations"]),
           "evidence snapshot booking count inconsistent")
    _check([e["event_id"] for e in body["evidence"]["trace"]] == [1, 2, 3],
           "evidence trace event ids not ordered 1..3")
    return (f"job {job_id} receipt: package pinned to adapter digest, "
            "observed digest matches run evidence, claimed kept separate, "
            "receipt digest verifies, cross-project read refused")


def _stage_determinism(ctx: dict) -> str:
    runs = ctx["runs"]
    _check(len(runs) == 4, f"expected 4 recorded runs, got {len(runs)}")
    _check(all(run["model_calls"] == 0 for run in runs),
           f"model calls leaked: {[r['model_calls'] for r in runs]}")
    _check(ctx["backend_run"]["evidence"] == ctx["trusted_naive"]["evidence"]
           and ctx["backend_run"]["checks"] == ctx["trusted_naive"]["checks"],
           "backend/trusted evidence diverged (nondeterminism)")
    return ("4 runs recorded (2 trusted-local, 1 backend, 1 cancelled); "
            "every model_calls == 0; backend evidence deterministic")


_STAGES = (
    ("env-qualify", _stage_env_qualify),
    ("trusted-local-baseline", _stage_trusted_local),
    ("backend-seam-run", _stage_backend_seam),
    ("unknown-backend-refusal", _stage_unknown_refusal),
    ("unavailable-backend-refusal", _stage_unavailable_refusal),
    ("qualification-honesty", _stage_qualification_honesty),
    ("cancellation", _stage_cancellation),
    ("evidence-identity", _stage_evidence_identity),
    ("determinism-zero-model-calls", _stage_determinism),
)


def main() -> int:
    base = "/tmp" if os.path.isdir("/tmp") else None
    failures: list = []
    registered: list = []
    with tempfile.TemporaryDirectory(prefix="b5-accept-", dir=base) as tmp:
        ctx: dict = {"tmp": tmp}
        try:
            for name, backend in ((BACKEND_NAME, AcceptanceBackend()),
                                  (UNAVAIL_NAME,
                                   AcceptanceBackend(unavailable=True))):
                if name in registered_backends():
                    unregister_backend(name)
                register_backend(name, backend)
                registered.append(name)
            for stage, fn in _STAGES:
                try:
                    detail = fn(ctx)
                except Exception as error:
                    print(f"FAIL {stage}: {type(error).__name__}: {error}",
                          flush=True)
                    failures.append(stage)
                else:
                    print(f"PASS {stage}: {detail}", flush=True)
        finally:
            for name in registered:
                try:
                    unregister_backend(name)
                except ValueError:
                    pass
            gc.collect()
    passed = len(_STAGES) - len(failures)
    if failures:
        print(f"ACCEPTANCE FAIL ({passed}/{len(_STAGES)} stages passed; "
              f"failed: {', '.join(failures)})", flush=True)
        return 1
    print(f"ACCEPTANCE PASS ({passed}/{len(_STAGES)} stages passed; "
          "model_calls=0)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
