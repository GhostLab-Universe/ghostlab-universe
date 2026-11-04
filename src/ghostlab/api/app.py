"""Live FastAPI service layer (Track E4).

Submitted guided configs really execute: ``POST /v1/guide/submit`` maps
an ``EffectiveConfig`` to Track C settings and resolves them without
executing, then ``POST /v1/runs`` executes the resolved config as a
run-group in a worker thread. All execution goes through
:mod:`ghostlab.experiments.ops` (never reimplemented); the service only
adds HTTP transport, project scoping, run-group bookkeeping and evidence
bundles. Zero model calls, no secrets anywhere.

Contract:

- JSON only. ``POST`` routes require ``Content-Type: application/json``.
- 1 MB request-body cap (``413 {error, detail}`` when exceeded).
- Project scoping via the ``X-GhostLab-Project`` header, validated with
  :func:`ghostlab.experiments.configs.check_project_id`. A missing or
  malformed header is ``400``; touching another project's record is
  ``403``. The registry itself is operator-global, but every route still
  requires the header so callers cannot forget scoping.
- Errors are always ``{error, detail}``: ``ConfigError``/bad input
  ``400``, cross-project ``403``, unknown ids ``404``,
  budget-exhausted ``409``, oversize ``413``, integrity/internal ``500``,
  known-but-unavailable execution backend ``503`` (never a fallback).
- ``GET /v1/backends`` lists execution backends (name, version,
  capabilities, qualification; ``trusted-local`` is labeled explicitly
  as in-process, not a sandbox). ``GET /v1/backends/{name}/status``
  adds a live liveness probe. Run responses (start, group status,
  export) carry additive ``backend``/``job``/``termination`` keys;
  ``POST /v1/runs`` accepts an optional ``backend`` selector that
  defaults to ``trusted-local``.
- No registration route. Installing adapter code stays an explicit local
  CLI operator act (``ghostlab env-register``) because registration
  resolves an allowlisted ``module:Class`` and fingerprints its source;
  exposing that over HTTP would let a remote caller drive code
  installation at an operator's browser. The service only operates on
  already-registered environments.
- ``POST /v1/envs/import-manifest`` is pure (validate + store only, never
  instantiates the adapter). ``POST /v1/envs/{id}/qualify`` and
  ``POST /v1/runs`` execute on purpose and are documented as such.
- Cancellation sets a threading event that the worker honors between
  repetitions and the controller honors before every dispatch. An
  in-flight synchronous adapter call is not interrupted and completes
  normally; a run cancelled before its first dispatch records
  ``status: cancelled`` with zero tool calls.

Persistence: ``EnvRegistry`` + ``ExperimentStore`` SQLite files,
configurable via ``create_app(registry_path=..., store_path=...)`` or the
``GHOSTLAB_REGISTRY_DB`` / ``GHOSTLAB_STORE_DB`` environment variables,
defaulting to ``artifacts/local/*.sqlite`` (gitignored). Run-groups and
submitted EffectiveConfigs live in process memory; a restart drops group
handles (stored runs and configs survive in SQLite).
"""

import json
import os
import shutil
import subprocess
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..envs.capabilities import discover
from ..envs.manifest import import_manifest
from ..envs.qualification import run_qualification
from ..envs.registry import EnvRegistry, utcnow
from ..experiments import ops
from ..experiments.configs import (canonical_digest, check_project_id,
                                   scan_for_secrets)
from ..experiments.configs import ConfigError
from ..experiments.controller import BudgetExhaustedError
from ..experiments.store import CrossProjectError, ExperimentStore
from . import guide as guide_module

MAX_BODY_BYTES = 1_000_000
PROJECT_HEADER = "x-ghostlab-project"
REGISTRY_ENV = "GHOSTLAB_REGISTRY_DB"
STORE_ENV = "GHOSTLAB_STORE_DB"

_TERMINAL_GROUPS = ("completed", "cancelled", "failed")

BACKENDS_VERSION = "backends/1"
TRUSTED_LOCAL_BACKEND = "trusted-local"
_JOB_STATES = ("prepared", "starting", "running", "completed", "failed",
               "cancelled", "timed_out")
_LIVENESS_TIMEOUT_SECONDS = 2


class BackendUnavailableError(Exception):
    """A known backend refused execution (mapped to 503, no fallback)."""

    def __init__(self, backend: str, reason: str):
        super().__init__(f"backend '{backend}' unavailable: {reason}")
        self.backend = backend
        self.reason = reason


def _backend_catalog() -> list:
    """Static backend descriptors (B5 status surface).

    Single seam for the isolated-backend milestone: a future
    ``ExecutionBackend`` registry plugs in here by returning these same
    descriptor shapes. Until then the catalog is honest about what
    exists: in-process trusted-local execution only. The container
    backend is listed as known-but-unavailable so selection refuses
    with 503 instead of silently falling back.
    """
    trusted = {
        "name": TRUSTED_LOCAL_BACKEND,
        "version": BACKENDS_VERSION,
        "trusted_local": True,
        "isolation": "in-process-checked-in-local",
        "isolation_scope": ("none: runs in the controller process;"
                            " reviewed checked-in adapter code only;"
                            " never presented as a sandbox"),
        "limitations": [
            "no OS/process boundary: jobs share the controller process",
            "reviewed checked-in adapter code only (allowlist + digest)",
            "no per-job resource enforcement beyond controller budgets",
            "cancellation is cooperative between repetitions and dispatches",
        ],
        "available": True,
        "unavailable_reason": None,
        "capabilities": {
            "execute_runs": True,
            "cancel": True,
            "lifecycle": list(_JOB_STATES),
            "network": "in-process (shares the controller process;"
                       " no network boundary)",
            "model_access": ("controller-side only;"
                             " no provider keys reach jobs"),
        },
        "qualification": {
            "status": "trusted-local (explicit; not an isolation boundary)",
            "qualified_for_isolation": False,
            "detail": ("trusted built-in path: allowlist + code digest +"
                       " project scoping + budgets; adversarial isolation"
                       " qualification does not apply in-process"),
            "checks": [],
        },
        "limits": {
            "enforced": False,
            "cpu": None,
            "memory": None,
            "pids": None,
            "wall_clock": None,
            "output": None,
            "detail": ("controller budgets only;"
                       " no OS-level enforcement in-process"),
        },
        "package_identity": {
            "kind": "checked-in-adapter",
            "image": None,
            "detail": ("operator-registered allowlisted module:Class +"
                       " code digest, bound per env at resolve time"),
        },
    }
    container = {
        "name": "linux-container",
        "version": BACKENDS_VERSION,
        "trusted_local": False,
        "isolation": "container (planned; no runtime qualified)",
        "isolation_scope": ("unavailable: no container runtime is qualified"
                            " in this build"),
        "limitations": [
            "backend unavailable in this build: selection refuses (503)",
            "no adversarial qualification evidence exists",
            "no image approved or verified",
        ],
        "available": False,
        "unavailable_reason": ("no container runtime is qualified in this"
                               " build: the daemon is unreachable or"
                               " unapproved and no adversarial"
                               " qualification evidence exists; refusing"
                               " instead of falling back"),
        "capabilities": {
            "execute_runs": False,
            "cancel": False,
            "lifecycle": list(_JOB_STATES),
            "network": "default none (planned)",
            "model_access": ("controller broker only; keys never in jobs"
                             " (planned)"),
        },
        "qualification": {
            "status": "unqualified-for-isolation",
            "qualified_for_isolation": False,
            "detail": ("adversarial qualification suite has not passed"
                       " against this backend"),
            "checks": [],
        },
        "limits": {
            "enforced": False,
            "cpu": None,
            "memory": None,
            "pids": None,
            "wall_clock": None,
            "output": None,
            "detail": "unenforced: backend unavailable",
        },
        "package_identity": {
            "kind": "container-image",
            "image": None,
            "detail": "no image approved or verified in this build",
        },
    }
    return [trusted, container]


def _describe_backend(name: str) -> dict:
    for descriptor in _backend_catalog():
        if descriptor["name"] == name:
            return deepcopy(descriptor)
    raise _ServiceError(404, "not_found", f"unknown backend '{name}'")


def _probe_liveness(name: str) -> dict:
    """Bounded offline liveness probe (no secrets, no model calls)."""
    checked_at = utcnow()
    if name == TRUSTED_LOCAL_BACKEND:
        return {"backend": name, "reachable": True,
                "checked_at": checked_at,
                "detail": ("in-process execution path in the controller"
                           " process; no external runtime to probe")}
    if name == "linux-container":
        docker = shutil.which("docker")
        if docker is None:
            return {"backend": name, "reachable": False,
                    "checked_at": checked_at,
                    "detail": ("docker CLI not found on PATH;"
                               " daemon unreachable")}
        try:
            proc = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True,
                timeout=_LIVENESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return {"backend": name, "reachable": False,
                    "checked_at": checked_at,
                    "detail": ("docker info timed out after"
                               f" {_LIVENESS_TIMEOUT_SECONDS}s;"
                               " daemon unreachable")}
        except OSError:
            return {"backend": name, "reachable": False,
                    "checked_at": checked_at,
                    "detail": ("docker CLI could not be executed;"
                               " daemon unreachable")}
        if proc.returncode != 0:
            return {"backend": name, "reachable": False,
                    "checked_at": checked_at,
                    "detail": (f"docker info exited"
                               f" {proc.returncode}; daemon unreachable")}
        version = proc.stdout.strip().splitlines()
        label = version[0][:32] if version and version[0] else "unknown"
        return {"backend": name, "reachable": True,
                "checked_at": checked_at,
                "detail": (f"docker daemon reachable (server {label});"
                           " backend still unqualified, refusing execution")}
    raise _ServiceError(404, "not_found", f"unknown backend '{name}'")


def _select_backend(requested) -> dict:
    """Validate an explicit backend selection (fail closed, no fallback)."""
    if requested is None:
        return _describe_backend(TRUSTED_LOCAL_BACKEND)
    if type(requested) is not str or not requested:
        raise _ServiceError(400, "invalid_request",
                            "runs body 'backend' must be a non-empty"
                            " string")
    descriptor = _describe_backend(requested[:100])
    if not descriptor["available"]:
        raise BackendUnavailableError(descriptor["name"],
                                      descriptor["unavailable_reason"]
                                      or "backend unavailable")
    return descriptor


def _backend_view(descriptor: dict) -> dict:
    return {"name": descriptor["name"],
            "trusted_local": descriptor["trusted_local"],
            "isolation": descriptor["isolation"]}


def _job_termination(status: str, error, job_id: str) -> tuple:
    job = {"job_id": job_id, "state": status}
    if status in _TERMINAL_GROUPS:
        return job, {"reason": status, "detail": error}
    return job, None


class _ServiceError(Exception):
    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


def _error(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": error, "detail": detail})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_paths() -> tuple:
    """Registry/store defaults under gitignored ``artifacts/local``."""
    root = _repo_root()
    registry = os.environ.get(REGISTRY_ENV) or str(
        root / "artifacts" / "local" / "ghostlab-env.sqlite")
    store = os.environ.get(STORE_ENV) or str(
        root / "artifacts" / "local" / "ghostlab-experiments.sqlite")
    return registry, store


def _require_project(request: Request) -> str:
    project_id = request.headers.get(PROJECT_HEADER)
    if project_id is None or project_id == "":
        raise _ServiceError(400, "missing_project",
                            "X-GhostLab-Project header is required")
    try:
        check_project_id(project_id)
    except ConfigError:
        raise _ServiceError(400, "invalid_project",
                            "invalid X-GhostLab-Project header") from None
    return project_id


async def _read_json(request: Request, *, allow_empty: bool = False):
    content_type = request.headers.get("content-type", "")
    length_header = request.headers.get("content-length")
    if "application/json" not in content_type.lower():
        if allow_empty and (length_header is None or length_header == "0"):
            return {}
        raise _ServiceError(400, "invalid_request",
                            "Content-Type must be application/json")
    if length_header is not None:
        try:
            declared = int(length_header)
        except ValueError:
            raise _ServiceError(400, "invalid_request",
                                "invalid Content-Length") from None
        if declared > MAX_BODY_BYTES:
            raise _ServiceError(413, "payload_too_large",
                                f"body exceeds {MAX_BODY_BYTES} bytes")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise _ServiceError(413, "payload_too_large",
                            f"body exceeds {MAX_BODY_BYTES} bytes")
    if not body:
        if allow_empty:
            return {}
        raise _ServiceError(400, "invalid_json",
                            "empty body, expected JSON")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _ServiceError(400, "invalid_json",
                            "body is not valid JSON") from None


def _is_unknown_id(message: str) -> bool:
    lowered = message.lower()
    if "unknown field" in lowered:
        return False
    return "unknown" in lowered


def _map_failure(error: Exception) -> JSONResponse:
    if isinstance(error, _ServiceError):
        return _error(error.status, error.error, error.detail)
    if isinstance(error, BackendUnavailableError):
        return _error(503, "backend_unavailable", str(error))
    if isinstance(error, CrossProjectError):
        return _error(403, "forbidden", str(error))
    if isinstance(error, BudgetExhaustedError):
        return _error(409, "budget_exhausted", str(error))
    if isinstance(error, ConfigError):
        message = str(error)
        if _is_unknown_id(message):
            return _error(404, "not_found", message)
        return _error(400, "invalid_config", message)
    if isinstance(error, ValueError):
        message = str(error)
        lowered = message.lower()
        if _is_unknown_id(message):
            return _error(404, "not_found", message)
        if "digest mismatch" in lowered or "checksum" in lowered:
            return _error(500, "internal",
                            "stored record failed integrity check")
        return _error(400, "invalid_request", message)
    return _error(500, "internal", "internal error")


def _strict_keys(body, allowed: set, what: str) -> None:
    if type(body) is not dict:
        raise _ServiceError(400, "invalid_request",
                            f"{what} body must be a JSON object")
    for key in body:
        if key not in allowed:
            raise _ServiceError(400, "invalid_request",
                                f"{what} body has unknown field {key!r}")


def create_app(registry_path=None, store_path=None,
               allowed_adapter_prefixes=("ghostlab.",)) -> FastAPI:
    """Build the live service app with configurable SQLite paths."""
    default_registry, default_store = default_paths()
    registry = EnvRegistry(registry_path or default_registry,
                           allowed_adapter_prefixes=tuple(
                               allowed_adapter_prefixes))
    store = ExperimentStore(store_path or default_store)

    app = FastAPI(title="GhostLab live service (E4)")
    groups: dict = {}
    submissions: dict = {}
    lock = threading.Lock()
    app.state.registry = registry
    app.state.store = store
    app.state.groups = groups
    app.state.submissions = submissions

    def _next_attempt(project_id: str, resolved_id: str) -> int:
        runs = store.list_runs(project_id, resolved_id)
        if not runs:
            return 0
        return max(run["attempt"] for run in runs) + 1

    def _worker(group_id: str) -> None:
        with lock:
            group = groups.get(group_id)
            if group is None:
                return
            project_id = group["project_id"]
            resolved_id = group["resolved_id"]
            cancel = group["cancel"]
            attempt = group["attempt"]
        try:
            resolved_record = store.get_resolved(project_id, resolved_id)
        except Exception as error:
            with lock:
                group["error"] = f"{type(error).__name__}: {error}"
                group["status"] = "failed"
                group["updated_at"] = utcnow()
            return
        count = resolved_record["resolved"]["effective"]["repetitions"][
            "count"]
        for repetition in range(count):
            if cancel.is_set():
                with lock:
                    group["status"] = "cancelled"
                    group["updated_at"] = utcnow()
                return
            try:
                run_record = ops.start_run(
                    store, registry, project_id, resolved_id,
                    repetition=repetition, attempt=attempt,
                    cancel=cancel)
            except BudgetExhaustedError as error:
                with lock:
                    group["error"] = f"budget_exhausted: {error}"
                    group["status"] = ("completed" if group["runs"]
                                       else "failed")
                    group["updated_at"] = utcnow()
                return
            except (ConfigError, CrossProjectError, ValueError) as error:
                with lock:
                    group["error"] = f"{type(error).__name__}: {error}"
                    group["status"] = "failed"
                    group["updated_at"] = utcnow()
                return
            except Exception as error:  # never leak internals
                with lock:
                    group["error"] = ("internal:"
                                      f" {type(error).__name__}")
                    group["status"] = "failed"
                    group["updated_at"] = utcnow()
                return
            with lock:
                group["runs"].append(run_record)
                group["updated_at"] = utcnow()
        with lock:
            if group["status"] != "running":
                return
            runs = group["runs"]
            last_cancelled = bool(
                runs and runs[-1]["run"]["status"] == "cancelled")
            if cancel.is_set() and (len(runs) < count or last_cancelled):
                group["status"] = "cancelled"
            else:
                group["status"] = "completed"
            group["updated_at"] = utcnow()

    def _group_for(project_id: str, group_id: str) -> dict:
        with lock:
            group = groups.get(group_id)
            if group is None:
                raise _ServiceError(404, "not_found",
                                    f"unknown run group '{group_id}'")
            if group["project_id"] != project_id:
                raise CrossProjectError(
                    f"run group '{group_id}' belongs to a different"
                    " project")
            return group

    @app.get("/v1/envs")
    async def list_envs(request: Request):
        try:
            project_id = _require_project(request)
            envs = [
                {"env_id": item["env_id"],
                 "env_version": item["env_version"],
                 "adapter_ref": item["adapter_ref"],
                 "display_name": item["display_name"],
                 "status": item["status"],
                 "code_digest": item["code_digest"]}
                for item in registry.list_envs()
            ]
            return {"project_id": project_id, "envs": envs}
        except Exception as error:
            return _map_failure(error)

    @app.post("/v1/envs/import-manifest")
    async def import_env_manifest(request: Request):
        try:
            project_id = _require_project(request)
            body = await _read_json(request)
            _strict_keys(body, {"manifest"}, "import-manifest")
            if "manifest" not in body or type(body["manifest"]) is not dict:
                raise _ServiceError(400, "invalid_request",
                                    "import-manifest body needs a"
                                    " 'manifest' object")
            data = json.dumps(body["manifest"]).encode("utf-8")
            if len(data) > MAX_BODY_BYTES:
                raise _ServiceError(413, "payload_too_large",
                                    f"body exceeds {MAX_BODY_BYTES} bytes")
            try:
                result = import_manifest(registry, data)
            except ValueError as error:
                message = str(error)
                if "unknown env_id" in message:
                    raise _ServiceError(404, "not_found",
                                        message) from error
                raise _ServiceError(400, "invalid_request",
                                    message) from error
            return {"project_id": project_id, **result}
        except Exception as error:
            return _map_failure(error)

    @app.get("/v1/envs/{env_id}/capabilities")
    async def env_capabilities(request: Request, env_id: str):
        try:
            project_id = _require_project(request)
            digest = request.query_params.get("manifest_digest")
            if digest == "":
                digest = None
            try:
                found = discover(registry, env_id,
                                 manifest_digest=digest)
            except ValueError as error:
                message = str(error)
                lowered = message.lower()
                if "unknown env_id" in lowered \
                        or "unknown manifest" in lowered:
                    raise _ServiceError(404, "not_found",
                                        message) from error
                raise _ServiceError(400, "invalid_request",
                                    message) from error
            return {"project_id": project_id, **found}
        except Exception as error:
            return _map_failure(error)

    @app.post("/v1/envs/{env_id}/qualify")
    async def env_qualify(request: Request, env_id: str):
        try:
            project_id = _require_project(request)
            body = await _read_json(request, allow_empty=True)
            _strict_keys(body, {"manifest_digest", "fixture"}, "qualify")
            digest = body.get("manifest_digest")
            fixture = body.get("fixture")
            if digest is not None and type(digest) is not str:
                raise _ServiceError(400, "invalid_request",
                                    "manifest_digest must be a string"
                                    " or null")
            if fixture is not None and type(fixture) is not dict:
                raise _ServiceError(400, "invalid_request",
                                    "fixture must be an object or null")
            try:
                report = run_qualification(
                    registry, env_id, manifest_digest=digest,
                    fixture=fixture)
            except ValueError as error:
                message = str(error)
                lowered = message.lower()
                if "unknown env_id" in lowered \
                        or "unknown manifest" in lowered:
                    raise _ServiceError(404, "not_found",
                                        message) from error
                raise _ServiceError(400, "invalid_request",
                                    message) from error
            return {"project_id": project_id, "report": report}
        except Exception as error:
            return _map_failure(error)

    @app.get("/v1/envs/{env_id}/report")
    async def env_report(request: Request, env_id: str):
        try:
            project_id = _require_project(request)
            try:
                report = registry.latest_report(env_id)
            except ValueError as error:
                raise _ServiceError(404, "not_found",
                                    str(error)) from error
            if report is None:
                raise _ServiceError(404, "not_found",
                                    f"no qualification report for"
                                    f" env_id '{env_id}'")
            return {"project_id": project_id, "report": report}
        except Exception as error:
            return _map_failure(error)

    @app.get("/v1/backends")
    async def list_backends(request: Request):
        try:
            project_id = _require_project(request)
            return {"project_id": project_id,
                    "backends": _backend_catalog()}
        except Exception as error:
            return _map_failure(error)

    @app.get("/v1/backends/{name}/status")
    async def backend_status(request: Request, name: str):
        try:
            project_id = _require_project(request)
            descriptor = _describe_backend(name[:100])
            liveness = _probe_liveness(descriptor["name"])
            return {"project_id": project_id,
                    "backend": descriptor,
                    "live_liveness": liveness}
        except Exception as error:
            return _map_failure(error)

    @app.post("/v1/guide/submit")
    async def guide_submit(request: Request):
        try:
            project_id = _require_project(request)
            body = await _read_json(request)
            _strict_keys(body, {"effective_config"}, "guide submit")
            if "effective_config" not in body:
                raise _ServiceError(400, "invalid_request",
                                    "guide submit body needs"
                                    " 'effective_config'")
            mapped = guide_module.map_effective_to_requested(
                body["effective_config"], registry=registry)
            created = ops.create_requested_config(
                store, project_id, mapped["requested"], mapped["policy"],
                "guide-service")
            resolved = ops.resolve_config(
                store, registry, project_id, created["config_id"],
                mapped["policy"], "guide-service",
                manifest_digest=mapped["manifest_digest"])
            with lock:
                submissions[resolved["resolved_id"]] = {
                    "project_id": project_id,
                    "requested_id": created["config_id"],
                    "effective_config": mapped["effective"],
                    "mapping_notes": mapped["mapping_notes"],
                }
            return {"project_id": project_id,
                    "requested_id": created["config_id"],
                    "resolved_id": resolved["resolved_id"],
                    "resolved": resolved["resolved"],
                    "mapping_notes": mapped["mapping_notes"]}
        except Exception as error:
            return _map_failure(error)

    @app.post("/v1/runs")
    async def start_runs(request: Request):
        try:
            project_id = _require_project(request)
            body = await _read_json(request)
            _strict_keys(body, {"resolved_id", "attempt", "backend"},
                         "runs")
            resolved_id = body.get("resolved_id")
            if type(resolved_id) is not str or not resolved_id:
                raise _ServiceError(400, "invalid_request",
                                    "runs body needs a 'resolved_id'"
                                    " string")
            # Attempts are single-use per (resolved, repetition): an omitted
            # attempt auto-assigns the next free number so an identical
            # config can re-execute (retest/Run-again). Two groups started
            # concurrently may collide; the loser fails honestly with the
            # store's already-recorded error.
            if body.get("attempt") is None:
                attempt = _next_attempt(project_id, resolved_id)
            elif type(body.get("attempt")) is int \
                    and body["attempt"] >= 0:
                attempt = body["attempt"]
            else:
                raise _ServiceError(400, "invalid_request",
                                    "runs body 'attempt' must be an"
                                    " integer >= 0")
            # Explicit backend selection, validated before any record is
            # created: unknown names 404, known-but-unavailable backends
            # 503. There is no silent fallback to another backend.
            backend = _select_backend(body.get("backend"))
            resolved_record = store.get_resolved(project_id, resolved_id)
            aggregate = resolved_record["resolved"]["effective"]["limits"][
                "max_total_tool_calls"]
            used = store.budget_used(project_id, resolved_id,
                                     "tool_calls")
            if used >= aggregate:
                raise BudgetExhaustedError(
                    f"budget envelope exhausted: {used}/{aggregate} tool"
                    " calls consumed; the environment was not touched")
            group_id = "grp-" + uuid.uuid4().hex[:16]
            created_at = utcnow()
            job_id = "job-" + uuid.uuid4().hex[:16]
            group = {"group_id": group_id, "project_id": project_id,
                     "resolved_id": resolved_id, "status": "running",
                     "attempt": attempt,
                     "backend": backend["name"], "job_id": job_id,
                     "runs": [], "error": None,
                     "cancel": threading.Event(),
                     "created_at": created_at, "updated_at": created_at}
            with lock:
                groups[group_id] = group
            worker = threading.Thread(target=_worker, args=(group_id,),
                                      daemon=True)
            worker.start()
            with lock:
                group["thread"] = worker
            return {"project_id": project_id, "group_id": group_id,
                    "resolved_id": resolved_id, "status": "running",
                    "attempt": attempt,
                    "backend": _backend_view(backend),
                    "job": {"job_id": job_id, "state": "starting"},
                    "termination": None}
        except Exception as error:
            return _map_failure(error)

    @app.get("/v1/runs/{group_id}")
    async def run_group_status(request: Request, group_id: str):
        try:
            project_id = _require_project(request)
            group = _group_for(project_id, group_id)
            with lock:
                runs = deepcopy(group["runs"])
                status = group["status"]
                error = group["error"]
                resolved_id = group["resolved_id"]
                attempt = group["attempt"]
                backend_name = group["backend"]
                job_id = group["job_id"]
                created_at = group["created_at"]
                updated_at = group["updated_at"]
            job, termination = _job_termination(status, error, job_id)
            return {"project_id": project_id, "group_id": group_id,
                    "resolved_id": resolved_id, "status": status,
                    "attempt": attempt,
                    "backend": _backend_view(
                        _describe_backend(backend_name)),
                    "job": job, "termination": termination,
                    "runs": runs, "error": error, "created_at": created_at,
                    "updated_at": updated_at}
        except Exception as error:
            return _map_failure(error)

    @app.post("/v1/runs/{group_id}/cancel")
    async def run_group_cancel(request: Request, group_id: str):
        try:
            project_id = _require_project(request)
            group = _group_for(project_id, group_id)
            with lock:
                if group["status"] in _TERMINAL_GROUPS:
                    return {"project_id": project_id,
                            "group_id": group_id,
                            "status": group["status"], "cancelled": False}
                group["cancel"].set()
                status = group["status"]
            return {"project_id": project_id, "group_id": group_id,
                    "status": status, "cancelled": True}
        except Exception as error:
            return _map_failure(error)

    @app.post("/v1/compare")
    async def compare_configs(request: Request):
        try:
            project_id = _require_project(request)
            body = await _read_json(request)
            _strict_keys(body, {"left_id", "right_id"}, "compare")
            left_id = body.get("left_id")
            right_id = body.get("right_id")
            if type(left_id) is not str or not left_id \
                    or type(right_id) is not str or not right_id:
                raise _ServiceError(400, "invalid_request",
                                    "compare body needs 'left_id' and"
                                    " 'right_id' strings")
            result = ops.compare_resolved(store, project_id, left_id,
                                          right_id)
            return {"project_id": project_id, **result}
        except Exception as error:
            return _map_failure(error)

    @app.get("/v1/runs/{group_id}/export")
    async def run_group_export(request: Request, group_id: str):
        try:
            project_id = _require_project(request)
            group = _group_for(project_id, group_id)
            with lock:
                runs = deepcopy(group["runs"])
                status = group["status"]
                resolved_id = group["resolved_id"]
                backend_name = group["backend"]
                job_id = group["job_id"]
                group_error = group["error"]
            job, termination = _job_termination(status, group_error,
                                                job_id)
            resolved_record = store.get_resolved(project_id, resolved_id)
            with lock:
                submission = submissions.get(resolved_id)
            if submission is None \
                    or submission["project_id"] != project_id:
                effective_config = None
                mapping_notes = None
                requested_id = resolved_record["requested_id"]
            else:
                effective_config = deepcopy(
                    submission["effective_config"])
                mapping_notes = deepcopy(submission["mapping_notes"])
                requested_id = submission["requested_id"]
            digests = {
                "effective_digest": (canonical_digest(effective_config)
                                    if effective_config is not None
                                    else None),
                "resolved_digest": resolved_record["digest"],
                "run_digests": [item["digest"] for item in runs],
            }
            bundle = {"project_id": project_id, "group_id": group_id,
                      "resolved_id": resolved_id,
                      "requested_id": requested_id, "status": status,
                      "backend": _backend_view(
                          _describe_backend(backend_name)),
                      "job": job, "termination": termination,
                      "effective_config": effective_config,
                      "resolved": resolved_record["resolved"], "runs": runs,
                      "mapping_notes": mapping_notes, "digests": digests,
                      "exported_at": utcnow()}
            scan_for_secrets(bundle, "export bundle")
            return bundle
        except Exception as error:
            return _map_failure(error)

    from . import llm_routes
    llm_routes.register(app, error=_error, service_error=_ServiceError,
                        read_json=_read_json, strict_keys=_strict_keys,
                        map_failure=_map_failure)
    return app
