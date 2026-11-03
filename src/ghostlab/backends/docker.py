"""Docker container execution backend (TRACK B4, host-gated).

:class:`DockerBackend` implements the frozen :class:`ExecutionBackend`
surface by running the sibling ``ghostlab.backends.worker`` loop inside a
hardened Linux container per job (``docker run`` with stdin/stdout pipes).
The wire protocol is byte-identical to :class:`LocalBackend` (same worker,
same :mod:`protocol` codec, same lifecycle/receipt semantics); only the
transport and the enforcement mechanism differ.

HONESTY RULES (never claim containment without executed evidence):

- Without a reachable Docker daemon this backend reports
  ``qualification.status == "blocked-no-daemon"`` and ``start_job`` raises
  :class:`BackendUnavailableError`. Nothing is claimed from config files.
- With a daemon but no executed adversarial run the status is
  ``"unqualified"``. ``"qualified"`` appears only after
  :meth:`record_qualification` stores executed evidence (plus a daemon
  fingerprint); a changed daemon reverts the status to ``"unqualified"``.
- Lifecycle tests that run against a stub ``docker`` executable prove
  protocol/lifecycle handling only — never isolation.

Hard per-job containment (see :func:`build_run_args`):

- ``--network none``, ``--user job``, ``--read-only``,
  ``--cap-drop ALL``, ``--security-opt no-new-privileges:true``,
  ``--pids-limit``, ``--memory``, ``--cpus``.
- Exactly three mounts: the per-job inputs directory (bind, read-only),
  ``/work/scratch`` (bounded tmpfs), ``/tmp`` (small tmpfs). No repo, home,
  credential, or socket mounts. No ``--privileged``/``--device``/``--ipc``/
  ``--pid``/``--uts``/``--volume``/``--publish`` forms, ever.
- Container names always carry the ``ghostlab-b4-`` prefix plus a random
  suffix; teardown only ever touches names passing
  :func:`owns_container_name`, so other sessions' containers are safe.

Only the standard library plus the sibling ``interface``/``protocol``/
``intake`` modules are used; ``docker`` is driven as a CLI subprocess
(``docker run``/``kill``/``inspect``/``rm``/``ps``/``image``), never via a
socket from inside a job.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import intake as _intake
from . import protocol as _protocol
from .interface import (TERMINAL_STATES, BackendError, BackendUnavailableError,
                        ExecutionBackend, JobSpec)

__all__ = [
    "CONTAINER_PREFIX",
    "DEFAULT_IMAGE",
    "DEFAULT_WORKER_ARGV",
    "ALLOWED_PREFIXES",
    "DaemonProbe",
    "DockerBackend",
    "activation_steps",
    "build_run_args",
    "check_hardened_argv",
    "container_name_for_job",
    "owns_container_name",
    "probe_daemon",
    "probe_wsl_docker",
    "resolve_docker_exe",
    "verify_mount_source",
    "bind_src",
]

CONTAINER_PREFIX = "ghostlab-b4-"
DEFAULT_IMAGE = "ghostlab-b4-worker"
DEFAULT_WORKER_ARGV = ("python", "-m", "ghostlab.backends.worker")
ALLOWED_PREFIXES = "ghostlab."
CONTAINER_WORKDIR = "/work"
CONTAINER_INPUTS = "/work/inputs"
CONTAINER_SCRATCH = "/work/scratch"

_DOCKER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9]{1,16}$")
_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}$")
_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_SIZE_PATTERN = re.compile(r"^(\d+)([bkmg])$")
_ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_CONTAINER_ENV_KEYS = frozenset({
    "GHOSTLAB_JOB_ID",
    "GHOSTLAB_ADAPTER_REF",
    "GHOSTLAB_ALLOWED_PREFIXES",
    "GHOSTLAB_MAX_OUTPUT_BYTES",
    "PYTHONPATH",
})

_MAX_NAME_LEN = 220
_MAX_STDERR_LINES = 200
_MAX_STDERR_BYTES = 64 * 1024
_LINE_READ_LIMIT = _protocol.MAX_MESSAGE_BYTES + 2
_DOCKER_TIMEOUT = 20
_PROBE_CACHE_SECONDS = 60

_EOF = object()
_OVERSIZE_LINE = object()


# -- pure helpers: names ------------------------------------------------

def container_name_for_job(job_id: str, *, suffix: str | None = None) -> str:
    """Build the owned container name for a job (prefix + random suffix)."""
    if type(job_id) is not str or not _JOB_ID_PATTERN.match(job_id):
        raise BackendError(f"invalid job_id '{job_id}'")
    if suffix is None:
        suffix = uuid.uuid4().hex[:8]
    if type(suffix) is not str or not _SUFFIX_PATTERN.match(suffix):
        raise BackendError(f"invalid container suffix '{suffix}'")
    name = f"{CONTAINER_PREFIX}{job_id}-{suffix}"
    if len(name) > _MAX_NAME_LEN or not _DOCKER_NAME_PATTERN.match(name):
        raise BackendError(f"container name for job '{job_id}' is not usable")
    return name


def owns_container_name(name) -> bool:
    """True only for container names this track created (safe to manage)."""
    return (type(name) is str and len(name) <= _MAX_NAME_LEN
            and len(name) > len(CONTAINER_PREFIX)
            and name.startswith(CONTAINER_PREFIX)
            and _DOCKER_NAME_PATTERN.match(name) is not None)


# -- pure helpers: argv assembly + hardening audit ------------------------

def _check_size(value, what: str) -> str:
    if type(value) is int:
        if value <= 0:
            raise BackendError(f"{what} must be positive")
        return f"{value}b"
    if type(value) is not str:
        raise BackendError(f"{what} must be like '256m'")
    text = value.strip().lower()
    match = _SIZE_PATTERN.match(text)
    if not match or int(match.group(1)) <= 0:
        raise BackendError(f"{what} must be like '256m'")
    return text


def _check_cpus(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BackendError("cpus must be a positive number") from None
    if not 0 < number <= 16:
        raise BackendError("cpus must be within (0, 16]")
    return str(value).strip() if type(value) is str else repr(number)


def _check_pids(value) -> str:
    if type(value) is bool or type(value) is not int or value <= 0:
        raise BackendError("pids_limit must be a positive integer")
    return str(value)


def _check_image(image) -> str:
    if type(image) is not str or not _IMAGE_PATTERN.match(image):
        raise BackendError(f"invalid worker image '{image}'")
    return image


def _check_env(env) -> list[tuple[str, str]]:
    if type(env) is not dict:
        raise BackendError("container env must be an object")
    pairs = []
    for key, value in env.items():
        if (type(key) is not str or not _ENV_KEY_PATTERN.match(key)
                or key not in _CONTAINER_ENV_KEYS):
            raise BackendError(f"refused container env key '{key}'")
        if (type(value) is not str or not value or len(value) > 4096
                or "\x00" in value or "\n" in value or "\r" in value):
            raise BackendError(f"refused container env value for '{key}'")
        pairs.append((key, value))
    return pairs


def _check_command(command) -> list[str]:
    if (not isinstance(command, (list, tuple)) or not command
            or any(type(part) is not str or not part or "\x00" in part
                   for part in command)):
        raise BackendError("container command must be non-empty strings")
    return list(command)


def build_run_args(*, docker_exe="docker", container_name: str, image: str,
                   inputs_dir: str, env: dict, memory="256m", cpus="0.5",
                   pids_limit=64, scratch_size="64m", tmp_size="16m",
                   command) -> list[str]:
    """Assemble the hardened ``docker run`` argv for one job.

    Callers must pass an inputs dir already verified with
    :func:`verify_mount_source`. The returned argv is audited with
    :func:`check_hardened_argv` before it is returned.
    """
    if isinstance(docker_exe, (list, tuple)):
        exe = [str(part) for part in docker_exe]
        if not exe or any(not part or "\x00" in part for part in exe):
            raise BackendError("invalid docker executable")
    elif type(docker_exe) is str and docker_exe and "\x00" not in docker_exe:
        exe = [docker_exe]
    else:
        raise BackendError("invalid docker executable")
    if not owns_container_name(container_name):
        raise BackendError(f"refused container name '{container_name}'")
    image = _check_image(image)
    if type(inputs_dir) is not str or not inputs_dir:
        raise BackendError("inputs_dir must be a path string")
    pairs = _check_env(env)
    parts = _check_command(command)
    argv = exe + ["run", "-i", "--name", container_name,
                 "--network", "none", "--user", "job", "--read-only",
                 "--cap-drop", "ALL", "--security-opt",
                 "no-new-privileges:true", "--pids-limit",
                 _check_pids(pids_limit), "--memory", _check_size(memory,
                                                                  "memory"),
                 "--cpus", _check_cpus(cpus), "--workdir", CONTAINER_WORKDIR,
                 "--mount",
                 f"type=bind,src={inputs_dir},dst={CONTAINER_INPUTS},readonly",
                 "--mount",
                 f"type=tmpfs,dst={CONTAINER_SCRATCH},"
                 f"tmpfs-size={_check_size(scratch_size, 'scratch_size')},"
                 "tmpfs-mode=1777", "--mount",
                 f"type=tmpfs,dst=/tmp,"
                 f"tmpfs-size={_check_size(tmp_size, 'tmp_size')},"
                 "tmpfs-mode=1777"]
    for key, value in pairs:
        argv += ["-e", f"{key}={value}"]
    argv += [image] + parts
    check_hardened_argv(argv)
    return argv


def check_hardened_argv(argv) -> None:
    """Audit a ``docker run`` argv; raise BackendError on any weakening."""
    if (not isinstance(argv, (list, tuple)) or "run" not in argv
            or len(argv) < 4):
        raise BackendError("not a docker run argv")
    args = list(argv[list(argv).index("run") + 1:])
    if "-i" not in args:
        raise BackendError("hardening audit: '-i' missing")

    def _flag_value(flag: str):
        if flag not in args:
            return None
        idx = args.index(flag)
        if idx + 1 >= len(args):
            raise BackendError(f"hardening audit: '{flag}' has no value")
        return args[idx + 1]

    if _flag_value("--name") is None or not owns_container_name(
            _flag_value("--name")):
        raise BackendError("hardening audit: owned --name missing")
    if _flag_value("--network") != "none":
        raise BackendError("hardening audit: '--network none' missing")
    if _flag_value("--user") != "job":
        raise BackendError("hardening audit: '--user job' missing")
    if "--read-only" not in args:
        raise BackendError("hardening audit: '--read-only' missing")
    if _flag_value("--cap-drop") != "ALL":
        raise BackendError("hardening audit: '--cap-drop ALL' missing")
    if _flag_value("--security-opt") != "no-new-privileges:true":
        raise BackendError("hardening audit: no-new-privileges missing")
    for flag in ("--pids-limit", "--memory", "--cpus"):
        if _flag_value(flag) is None:
            raise BackendError(f"hardening audit: '{flag}' missing")
    mounts = [args[i + 1] for i, tok in enumerate(args)
              if tok == "--mount" and i + 1 < len(args)]
    if len(mounts) != 3:
        raise BackendError("hardening audit: expected exactly 3 --mount entries")
    if not any(m.startswith("type=bind,src=") and m.endswith(
            f",dst={CONTAINER_INPUTS},readonly") for m in mounts):
        raise BackendError("hardening audit: read-only inputs mount missing")
    if not any(m.startswith(f"type=tmpfs,dst={CONTAINER_SCRATCH},")
               for m in mounts):
        raise BackendError("hardening audit: scratch tmpfs mount missing")
    if not any(m.startswith("type=tmpfs,dst=/tmp,") for m in mounts):
        raise BackendError("hardening audit: /tmp tmpfs mount missing")
    forbidden = {"--privileged", "--device", "--ipc", "--pid", "--uts",
                 "--cap-add", "--gpus", "--runtime", "-v", "--volume",
                 "--publish", "-p", "--net", "--privileged"}
    for token in args:
        if (token in forbidden or token.startswith("--device=")
                or token.startswith("--ipc=") or token.startswith("--pid=")):
            raise BackendError(
                f"hardening audit: forbidden token '{token}'")
    for i, token in enumerate(args):
        if token == "-e" and i + 1 < len(args):
            key = args[i + 1].split("=", 1)[0]
            if key not in _CONTAINER_ENV_KEYS:
                raise BackendError(
                    f"hardening audit: forbidden env key '{key}'")


# -- pure helpers: workspace + host probing --------------------------------

def verify_mount_source(candidate, *, workspace_root, must_exist=True) -> str:
    """Realpath-verify a mount source stays inside the workspace root."""
    if isinstance(workspace_root, Path):
        workspace_root = str(workspace_root)
    if isinstance(candidate, Path):
        candidate = str(candidate)
    if type(workspace_root) is not str or not workspace_root:
        raise BackendError("workspace_root must be a path string")
    if type(candidate) is not str or not candidate or "\x00" in candidate:
        raise BackendError("mount source must be a path string")
    root = os.path.realpath(workspace_root)
    if not os.path.isdir(root):
        raise BackendError("workspace root is not a directory")
    target = os.path.realpath(candidate)
    folded_root, folded_target = (os.path.normcase(root),
                                  os.path.normcase(target))
    inside = (folded_target == folded_root
              or folded_target.startswith(folded_root + os.sep))
    if not inside:
        raise BackendError(
            f"mount source '{candidate}' escapes the workspace")
    if must_exist and not os.path.exists(target):
        raise BackendError(f"mount source '{candidate}' does not exist")
    return target


def bind_src(path: str) -> str:
    """Render a verified host path for ``--mount type=bind,src=...``."""
    if type(path) is not str or not path or "\x00" in path:
        raise BackendError("bind source must be a path string")
    if os.name == "nt":
        return path.replace("\\", "/")
    return path


def resolve_docker_exe(explicit=None):
    """Resolve the docker CLI (None when no executable is available)."""
    if explicit is None:
        found = shutil.which("docker")
        return found
    if isinstance(explicit, (list, tuple)):
        parts = list(explicit)
        if (not parts or any(type(p) is not str or not p or "\x00" in p
                             for p in parts)):
            raise BackendError("invalid docker executable")
        return parts
    if type(explicit) is not str or not explicit or "\x00" in explicit:
        raise BackendError("invalid docker executable")
    return explicit


def _exe_list(explicit=None):
    resolved = resolve_docker_exe(explicit)
    if resolved is None:
        return None
    return list(resolved) if isinstance(resolved, list) else [resolved]


@dataclass
class DaemonProbe:
    available: bool
    docker_exe: str | None
    client_version: str | None
    server_version: str | None
    error: str | None
    version_output: str = ""
    info_output: str = ""


_VERSION_RE = re.compile(r"Version:\s+(\S+)")


def _parse_versions(text: str):
    parts = re.split(r"(?m)^Server:", text)
    client = _VERSION_RE.search(parts[0]) if parts else None
    server = _VERSION_RE.search(parts[1]) if len(parts) > 1 else None
    return ((client.group(1) if client else None),
            (server.group(1) if server else None))


_DAEMON_DOWN_MARKERS = ("cannot connect to the docker daemon",
                        "error during connect",
                        "is the docker daemon running",
                        "dockerdesktoplinuxengine",
                        "the system cannot find the file specified",
                        "no such file or directory")


def _looks_daemon_down(text: str) -> bool:
    folded = (text or "").lower()
    return any(marker in folded for marker in _DAEMON_DOWN_MARKERS)


def probe_daemon(docker_exe=None, timeout_s: int = 15) -> DaemonProbe:
    """Probe the docker CLI + daemon without starting anything.

    Never launches Docker Desktop, dockerd, or any container; it only runs
    ``docker version`` and ``docker info`` with bounded timeouts.
    """
    exe = _exe_list(docker_exe)
    if exe is None:
        return DaemonProbe(False, None, None, None,
                           "docker CLI not found on PATH")
    try:
        version = subprocess.run(exe + ["version"], capture_output=True,
                                 text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as error:
        return DaemonProbe(False, exe[0], None, None,
                           f"docker version failed: {error}")
    combined = (version.stdout or "") + "\n" + (version.stderr or "")
    client_version, parsed_server = _parse_versions(combined)
    try:
        info = subprocess.run(exe + ["info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True,
                              timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as error:
        return DaemonProbe(False, exe[0], client_version, parsed_server,
                           f"docker info failed: {error}",
                           version_output=combined.strip())
    if info.returncode == 0:
        server_version = (info.stdout or "").strip() or parsed_server
        return DaemonProbe(True, exe[0], client_version, server_version,
                           None, version_output=combined.strip(),
                           info_output=(info.stdout or "").strip())
    detail = ((info.stderr or "").strip().splitlines() or [""])[:1][0]
    if not detail:
        detail = (combined.strip().splitlines() or [""])[:1][0]
    return DaemonProbe(False, exe[0], client_version, parsed_server,
                       detail or "docker daemon unreachable",
                       version_output=combined.strip(),
                       info_output=(info.stderr or "").strip())


def probe_wsl_docker(timeout_s: int = 60) -> dict:
    """Best-effort record of WSL distros + docker inside WSL (never raises)."""
    record: dict = {"wsl_present": False, "distros": "",
                    "docker_present": None, "docker_output": "", "error": ""}
    wsl = shutil.which("wsl")
    if wsl is None:
        record["error"] = "wsl not found on PATH"
        return record
    record["wsl_present"] = True
    try:
        distros = subprocess.run([wsl, "-l", "-v"], capture_output=True,
                                 text=True, timeout=min(timeout_s, 30))
        record["distros"] = ((distros.stdout or "")
                             + (distros.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        record["error"] = f"wsl -l -v failed: {error}"
        return record
    try:
        inside = subprocess.run([wsl, "docker", "version"],
                                capture_output=True, text=True,
                                timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as error:
        record["error"] = f"wsl docker version failed: {error}"
        return record
    record["docker_present"] = inside.returncode == 0
    record["docker_output"] = ((inside.stdout or "")
                               + (inside.stderr or "")).strip()
    return record


def activation_steps() -> str:
    """Exact operator steps to qualify this backend on a host."""
    return "\n".join([
        "1. Start a Linux container daemon: launch Docker Desktop (Windows)",
        "   and wait until `docker info` exits 0, or enable the Docker",
        "   Desktop WSL2 integration / run dockerd inside WSL2 Ubuntu.",
        "2. Verify: `docker version` shows a Server entry and `docker info`",
        "   exits 0. Record both outputs with the qualification report.",
        "3. From the repo root, build the operator worker image:",
        "     docker build -f src/ghostlab/backends/Dockerfile.worker \\",
        "       -t ghostlab-b4-worker .",
        "   then record the content pin (local builds have no RepoDigest",
        "   until pushed; the image Id is the pin):",
        "     docker inspect --format '{{.Id}}' ghostlab-b4-worker",
        "4. Re-run the B4 suite (PYTHONPATH=src):",
        "     python -m unittest tests.test_backend_docker_b4 -v",
        "   Daemon-enforced checks (adversarial profile, usability run,",
        "   digest pinning) run only with a reachable daemon; with no",
        "   daemon they skip as BLOCKED, never as passed.",
    ])


# -- job record + framed stdio --------------------------------------------

class _Job:
    def __init__(self, spec: JobSpec, workspace: str, inputs: str,
                 container: str, image: str, proc) -> None:
        self.spec = spec
        self.workspace = workspace
        self.inputs = inputs
        self.container = container
        self.image = image
        self.proc = proc
        self.state = "starting"
        self.request_seq = 0
        self.event_seq = 0
        self.output_bytes = 0
        self.started_at = time.monotonic()
        self.deadline = (self.started_at
                         + spec.limits["wall_clock_ms"] / 1000.0)
        self.inbox: queue.Queue = queue.Queue()
        self.pending: list = []
        self.receipts: list = []
        self.stderr_lines: list = []
        self.stderr_bytes = 0
        self.exit_code = None
        self.lock = threading.Lock()
        self.eof_seen = False
        self.stdout_thread = None
        self.stderr_thread = None


def _bounded_readline(stream, limit: int):
    """Read one \\n-terminated line capped at ``limit`` bytes (framed)."""
    try:
        chunk = stream.readline(limit)
    except (OSError, ValueError):
        return None
    if not chunk:
        return None
    if len(chunk) >= limit and not chunk.endswith(b"\n"):
        while True:
            try:
                rest = stream.readline(65536)
            except (OSError, ValueError):
                break
            if not rest or rest.endswith(b"\n"):
                break
        return _OVERSIZE_LINE
    return chunk


class DockerBackend(ExecutionBackend):
    """Hardened-container backend; host-gated, see module docstring."""

    name: str = "docker"

    def __init__(self, workspace_root, *, image: str = DEFAULT_IMAGE,
                 image_digest: str | None = None, docker_exe=None,
                 worker_argv=None, pythonpath: str = "/work/inputs:/src",
                 memory="256m", cpus="0.5", pids_limit=64,
                 scratch_size="64m", tmp_size="16m") -> None:
        if isinstance(workspace_root, Path):
            workspace_root = str(workspace_root)
        if type(workspace_root) is not str or not workspace_root:
            raise ValueError("workspace_root must be a path string")
        self._root = os.path.realpath(workspace_root)
        os.makedirs(self._root, exist_ok=True)
        if not os.path.isdir(self._root):
            raise BackendError("workspace root is not a directory")
        self._image = _check_image(image)
        if image_digest is not None and (
                type(image_digest) is not str
                or not _DIGEST_PATTERN.match(image_digest)):
            raise BackendError("image_digest must be a sha256 hex digest")
        self._digest = image_digest
        # Validated but never executed here; resolution happens per call so
        # offline operations (prepare, capabilities) never require a daemon.
        resolve_docker_exe(docker_exe)
        self._docker_exe = docker_exe
        if worker_argv is None:
            self._worker_argv = list(DEFAULT_WORKER_ARGV)
        else:
            # Test-only hook: run a protocol probe instead of the worker
            # under the SAME hardened flags/mounts. Never set in production.
            self._worker_argv = _check_command(worker_argv)
        if (type(pythonpath) is not str or not pythonpath
                or len(pythonpath) > 4096 or "\x00" in pythonpath):
            raise BackendError("pythonpath must be a short clean string")
        self._pythonpath = pythonpath
        self._memory = _check_size(memory, "memory")
        self._cpus = _check_cpus(cpus)
        self._pids_limit = _check_pids(pids_limit)
        self._scratch_size = _check_size(scratch_size, "scratch_size")
        self._tmp_size = _check_size(tmp_size, "tmp_size")
        self._jobs: dict[str, _Job] = {}
        self._prepared: dict[str, dict] = {}
        self._guard = threading.Lock()
        self._probe_cache: tuple[float, DaemonProbe | None] = (0.0, None)

    @property
    def image_ref(self) -> str:
        if self._digest:
            return f"{self._image}@sha256:{self._digest}"
        return self._image

    # -- docker CLI transport -------------------------------------------

    def _docker(self, args: list[str], *, timeout: int = _DOCKER_TIMEOUT):
        exe = _exe_list(self._docker_exe)
        if exe is None:
            raise BackendUnavailableError("no-daemon: docker CLI not found")
        try:
            completed = subprocess.run(exe + args, capture_output=True,
                                       text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise BackendError(f"docker {' '.join(args)} timed out") from error
        except OSError as error:
            raise BackendUnavailableError(
                f"no-daemon: docker CLI failed: {error}") from error
        if (completed.returncode != 0 and _looks_daemon_down(
                (completed.stdout or "") + (completed.stderr or ""))):
            detail = ((completed.stderr or "").strip().splitlines()
                      or ["docker daemon unreachable"])[:1][0]
            raise BackendUnavailableError(f"no-daemon: {detail}")
        return completed

    def _probe_cached(self) -> DaemonProbe:
        now = time.monotonic()
        if (self._probe_cache[1] is not None
                and now - self._probe_cache[0] < _PROBE_CACHE_SECONDS):
            cached = self._probe_cache[1]
            assert cached is not None
            return cached
        probe = probe_daemon(self._docker_exe)
        self._probe_cache = (now, probe)
        return probe

    def _container_exists(self, name: str) -> bool:
        if not owns_container_name(name):
            raise BackendError(f"refused container name '{name}'")
        try:
            completed = self._docker(
                ["inspect", "--format", "{{.Id}}", name], timeout=10)
        except BackendError:
            return False
        return completed.returncode == 0

    # -- capabilities / prepare ----------------------------------------

    def _qualification_path(self) -> str:
        return os.path.join(self._root, "qualification.json")

    def _load_qualification(self):
        try:
            with open(self._qualification_path(), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            return None
        if type(record) is not dict:
            return None
        if record.get("status") not in ("qualified", "failed"):
            return None
        if type(record.get("report_id")) is not str or not record["report_id"]:
            return None
        return record

    def capabilities(self) -> dict:
        probe = self._probe_cached()
        qualification: dict
        if not probe.available:
            qualification = {"status": "blocked-no-daemon"}
        else:
            record = self._load_qualification()
            if (record is not None
                    and record.get("server_version") == probe.server_version
                    and record.get("image_ref") == self.image_ref):
                qualification = {"status": record["status"],
                                 "report_id": record["report_id"]}
            else:
                qualification = {"status": "unqualified"}
        return {
            "isolation": {
                "filesystem": ("container read-only rootfs + read-only "
                               "inputs mount + tmpfs scratch "
                               "(requires daemon; see qualification)"),
                "process": ("container pid namespace, user job, "
                            "pids-limit, dropped caps "
                            "(requires daemon; see qualification)"),
                "network": "none",
            },
            "limits": ["wall_clock_ms", "max_output_bytes",
                       "max_message_bytes", "max_message_depth",
                       "max_archive_bytes", "max_archive_files",
                       "container_cpus", "container_memory",
                       "container_pids", "container_scratch"],
            "qualification": qualification,
        }

    def record_qualification(self, *, status: str, report_id: str,
                             evidence: dict) -> dict:
        """Store executed adversarial evidence (daemon must be reachable)."""
        if status not in ("qualified", "failed"):
            raise BackendError("status must be 'qualified' or 'failed'")
        if type(report_id) is not str or not report_id or len(report_id) > 128:
            raise BackendError("report_id must be a short non-empty string")
        if type(evidence) is not dict or not evidence:
            raise BackendError("evidence must be a non-empty object")
        probe = probe_daemon(self._docker_exe)
        if not probe.available:
            raise BackendError(
                "cannot record qualification without a reachable daemon")
        record = {"status": status, "report_id": report_id,
                  "evidence": evidence,
                  "server_version": probe.server_version,
                  "client_version": probe.client_version,
                  "image_ref": self.image_ref,
                  "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())}
        tmp_path = self._qualification_path() + f".{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
        os.replace(tmp_path, self._qualification_path())
        self._probe_cache = (0.0, None)
        return {"status": status, "report_id": report_id}

    def prepare(self, package: dict) -> dict:
        if type(package) is not dict or "kind" not in package:
            raise BackendError("package must declare a kind")
        kind = package["kind"]
        if kind in ("tar.gz", "zip"):
            data = package.get("data")
            if type(data) is not bytes or not data:
                raise BackendError("archive packages must carry data bytes")
            manifest = _intake.validate_archive(
                data, kind, _intake.DEFAULT_MAX_ARCHIVE_BYTES,
                _intake.DEFAULT_MAX_FILES, _intake.DEFAULT_MAX_RATIO)
            identity = manifest["identity"]
            with self._guard:
                self._prepared[identity] = {"kind": kind, "data": data,
                                            "manifest": manifest}
            return {"identity": identity,
                    "provenance": {"kind": kind, "sha256": identity,
                                   "bytes": len(data),
                                   "files": len(manifest["files"]),
                                   "total_size": manifest["total_size"]}}
        if kind == "checked-in":
            identity = package.get("identity")
            if (type(identity) is not str or not identity
                    or len(identity) > 256):
                raise BackendError(
                    "checked-in packages must pin an identity")
            return {"identity": identity,
                    "provenance": {"kind": "checked-in"}}
        raise BackendError(f"unsupported package kind '{kind}'")

    # -- lifecycle ------------------------------------------------------

    def _require(self, job_id: str) -> _Job:
        if type(job_id) is not str:
            raise BackendError("unknown job")
        with self._guard:
            job = self._jobs.get(job_id)
        if job is None:
            raise BackendError(f"unknown job '{job_id}'")
        return job

    def start_job(self, spec: JobSpec) -> None:
        if type(spec) is not JobSpec:
            raise BackendError("spec must be a JobSpec")
        with self._guard:
            if spec.job_id in self._jobs:
                raise BackendError(f"duplicate job '{spec.job_id}'")
            prepared = None
            if spec.package["kind"] in ("tar.gz", "zip"):
                prepared = self._prepared.get(spec.package["identity"])
                if prepared is None:
                    raise BackendError(
                        "archive package was not prepared by this backend")
            elif spec.package["kind"] != "checked-in":
                raise BackendError(
                    f"unsupported package kind '{spec.package['kind']}'")
        if spec.no_network is not True:
            raise BackendError(
                "docker backend refuses no_network=False "
                "(--network none is mandatory)")
        probe = probe_daemon(self._docker_exe)
        self._probe_cache = (time.monotonic(), probe)
        if not probe.available:
            raise BackendUnavailableError(
                f"no-daemon: {probe.error or 'docker daemon unreachable'}")
        inspected = self._docker(["image", "inspect", self.image_ref],
                                 timeout=_DOCKER_TIMEOUT)
        if inspected.returncode != 0:
            raise BackendError(
                f"worker image '{self.image_ref}' is missing; operator must "
                "build it from the repo root: docker build -f "
                "src/ghostlab/backends/Dockerfile.worker -t "
                f"{self._image} .")
        workspace = os.path.realpath(os.path.join(self._root, spec.job_id))
        if workspace != os.path.join(self._root, spec.job_id):
            raise BackendError("job workspace escapes the root")
        if os.path.lexists(workspace):
            raise BackendError(f"workspace for job '{spec.job_id}' exists")
        os.makedirs(workspace)
        inputs = os.path.join(workspace, "inputs")
        os.makedirs(inputs)
        if prepared is not None:
            _intake.extract_archive(
                prepared["data"], prepared["kind"], inputs,
                max_bytes=_intake.DEFAULT_MAX_ARCHIVE_BYTES,
                max_files=_intake.DEFAULT_MAX_FILES,
                max_ratio=_intake.DEFAULT_MAX_RATIO)
        verify_mount_source(inputs, workspace_root=self._root)
        container = container_name_for_job(spec.job_id)
        env = {"GHOSTLAB_JOB_ID": spec.job_id,
               "GHOSTLAB_ADAPTER_REF": spec.adapter_ref,
               "GHOSTLAB_ALLOWED_PREFIXES": ALLOWED_PREFIXES,
               "GHOSTLAB_MAX_OUTPUT_BYTES": str(
                   spec.limits["max_output_bytes"]),
               "PYTHONPATH": self._pythonpath}
        argv = build_run_args(
            docker_exe=_exe_list(self._docker_exe) or "docker",
            container_name=container, image=self.image_ref,
            inputs_dir=bind_src(inputs), env=env, memory=self._memory,
            cpus=self._cpus,
            pids_limit=int(self._pids_limit), scratch_size=self._scratch_size,
            tmp_size=self._tmp_size, command=self._worker_argv)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=False)
        except OSError as error:
            shutil.rmtree(workspace, ignore_errors=True)
            raise BackendError(f"container spawn failed: {error}") from error
        job = _Job(spec, workspace, inputs, container, self.image_ref, proc)
        with self._guard:
            self._jobs[spec.job_id] = job
        job.stdout_thread = threading.Thread(
            target=self._read_stdout, args=(job,), daemon=True,
            name=f"ghostlab-b4-out-{spec.job_id}")
        job.stderr_thread = threading.Thread(
            target=self._read_stderr, args=(job,), daemon=True,
            name=f"ghostlab-b4-err-{spec.job_id}")
        job.stdout_thread.start()
        job.stderr_thread.start()

    # -- messaging (same wire semantics as the local reference) ---------

    def send(self, job_id: str, command: dict) -> None:
        job = self._require(job_id)
        if type(command) is not dict or type(command.get("type")) is not str:
            raise BackendError("command must carry a type string")
        if command["type"] not in _protocol.WORKER_COMMANDS:
            raise BackendError(f"unknown command '{command['type']}'")
        payload = command.get("payload", {})
        if type(payload) is not dict:
            raise BackendError("command payload must be an object")
        with job.lock:
            if job.state in TERMINAL_STATES:
                raise BackendError(f"job '{job_id}' is {job.state}")
            seq = job.request_seq
            job.request_seq += 1
        try:
            line = _protocol.encode_message(
                job_id, seq, command["type"], payload)
        except _protocol.ProtocolError as error:
            raise BackendError(str(error)) from error
        if job.proc.stdin is None:
            self._transition(job, "failed", "worker-stdin-missing")
            raise BackendError(f"job '{job_id}' input is closed")
        try:
            job.proc.stdin.write(line + b"\n")
            job.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            self._transition(job, "failed", "send-broken-pipe",
                             {"error": str(error)[:200]})
            raise BackendError(f"job '{job_id}' input is closed") from error

    def _transition(self, job: _Job, state: str, reason: str,
                    extra: dict | None = None) -> None:
        with job.lock:
            if job.state in TERMINAL_STATES:
                return
            job.state = state
            receipt: dict = {"kind": "lifecycle", "state": state,
                             "reason": reason}
            if extra:
                receipt.update(extra)
            job.receipts.append(receipt)

    def _enforce_deadline(self, job: _Job) -> None:
        with job.lock:
            terminal = job.state in TERMINAL_STATES
        if terminal or time.monotonic() < job.deadline:
            return
        self._terminate_now(job)
        self._transition(job, "timed_out", "wall-clock-exhausted",
                         {"wall_clock_ms": job.spec.limits["wall_clock_ms"]})

    def _kill_container(self, job: _Job, signal: str) -> None:
        try:
            self._docker(["kill", f"--signal={signal}", job.container],
                         timeout=10)
        except BackendError:
            pass

    def _remove_container(self, job: _Job) -> bool:
        try:
            completed = self._docker(["rm", "-f", job.container], timeout=15)
        except BackendError:
            return False
        return completed.returncode == 0

    def _terminate_now(self, job: _Job) -> None:
        """SIGTERM then SIGKILL-equivalent removal (fail-closed paths)."""
        self._kill_container(job, "TERM")
        exited = self._wait_proc(job, 1.0)
        if not exited:
            self._kill_container(job, "KILL")
            self._wait_proc(job, 5.0)
        self._remove_container(job)

    @staticmethod
    def _wait_proc(job: _Job, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            if job.proc.poll() is not None:
                return True
            time.sleep(0.05)
        return job.proc.poll() is not None

    def _process_line(self, job: _Job, raw) -> dict | None:
        if raw is _OVERSIZE_LINE:
            self._terminate_now(job)
            self._transition(job, "failed", "worker-line-oversize",
                             {"truncated": True})
            return self._synth(job, "failed",
                               {"error": "oversize-worker-line",
                                "truncated": True})
        with job.lock:
            cap = job.spec.limits["max_output_bytes"]
        job.output_bytes += len(raw)
        if job.output_bytes > cap:
            self._terminate_now(job)
            self._transition(job, "failed", "output-exhausted",
                             {"truncated": True,
                              "output_bytes": job.output_bytes})
            return self._synth(job, "failed",
                               {"error": "output-exhausted",
                                "truncated": True})
        try:
            event = _protocol.decode_message(raw)
        except _protocol.ProtocolError as error:
            self._terminate_now(job)
            self._transition(job, "failed", "malformed-worker-output",
                             {"error": str(error)[:300]})
            return self._synth(job, "failed",
                               {"error": "malformed-worker-output",
                                "message": str(error)[:300]})
        if event["job_id"] != job.spec.job_id:
            self._terminate_now(job)
            self._transition(job, "failed", "cross-job-output")
            return self._synth(job, "failed",
                               {"error": "cross-job-output"})
        if event["seq"] != job.event_seq:
            self._terminate_now(job)
            self._transition(job, "failed", "worker-seq-gap",
                             {"expected": job.event_seq,
                              "got": event["seq"]})
            return self._synth(job, "failed",
                               {"error": "worker-seq-gap"})
        if event["type"] not in _protocol.WORKER_EVENTS:
            self._terminate_now(job)
            self._transition(job, "failed", "unknown-worker-event",
                             {"event": str(event["type"])[:64]})
            return self._synth(job, "failed",
                               {"error": "unknown-worker-event"})
        job.event_seq += 1
        if event["type"] == "started":
            with job.lock:
                if job.state == "starting":
                    job.state = "running"
        elif event["type"] == "truncated":
            self._transition(job, "failed", "worker-truncated",
                             {"truncated": True})
        elif event["type"] == "completed":
            self._transition(job, "completed", "worker-completed")
        elif event["type"] == "failed":
            detail = event["payload"].get("message", "")
            self._transition(job, "failed", "worker-failed",
                             {"error": str(event["payload"].get(
                                 "error", ""))[:200],
                              "message": str(detail)[:300]})
        return event

    def _synth(self, job: _Job, event_type: str, payload: dict) -> dict:
        event = {"v": _protocol.PROTOCOL_VERSION, "job_id": job.spec.job_id,
                 "seq": job.event_seq, "type": event_type, "payload": payload}
        job.event_seq += 1
        return event

    def _note_exit(self, job: _Job) -> None:
        code = job.proc.poll()
        job.exit_code = code
        if code is None:
            return
        with job.lock:
            terminal = job.state in TERMINAL_STATES
        if not terminal:
            self._transition(job, "failed", "worker-exited",
                             {"exit_code": code})

    def _pump(self, job: _Job, timeout: float) -> None:
        """Move queued worker lines into validated pending events."""
        deadline = None if timeout <= 0 else time.monotonic() + timeout
        while True:
            self._enforce_deadline(job)
            with job.lock:
                terminal = job.state in TERMINAL_STATES
            if terminal:
                timeout_left = 0.0
            elif deadline is None:
                timeout_left = 0.0
            else:
                timeout_left = max(0.0, deadline - time.monotonic())
                if timeout_left <= 0 and job.pending:
                    return
            try:
                if terminal or deadline is None or job.pending:
                    # Events are ready: drain what is queued, never block
                    # for the full poll timeout once a result exists.
                    item = job.inbox.get_nowait()
                else:
                    item = job.inbox.get(timeout=timeout_left)
            except queue.Empty:
                if terminal or job.eof_seen:
                    # EOF may precede process reaping; re-check the exit.
                    self._note_exit(job)
                return
            if item is _EOF:
                with job.lock:
                    job.eof_seen = True
                    drained = job.inbox.empty()
                if drained:
                    self._note_exit(job)
                    if terminal or job.proc.poll() is not None:
                        return
                    if deadline is None:
                        return
                    continue
                continue
            with job.lock:
                terminal_now = job.state in TERMINAL_STATES
            if terminal_now:
                continue  # drain and discard late output after terminal
            event = self._process_line(job, item)
            if event is not None:
                job.pending.append(event)
            with job.lock:
                became_terminal = job.state in TERMINAL_STATES
            if became_terminal:
                continue  # drain queued lines without blocking
            if deadline is None:
                continue  # timeout 0: drain everything available
            if time.monotonic() >= deadline:
                return

    def poll(self, job_id: str, timeout_ms: int = 0) -> list:
        job = self._require(job_id)
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise BackendError("timeout_ms must be a non-negative integer")
        self._pump(job, timeout_ms / 1000.0)
        with job.lock:
            events = job.pending
            job.pending = []
        return events

    def status(self, job_id: str) -> dict:
        job = self._require(job_id)
        self._pump(job, 0)  # surface queued EOF/exit before reporting
        if job.eof_seen:
            self._note_exit(job)  # exit may be reaped after EOF was seen
        self._enforce_deadline(job)
        with job.lock:
            state = job.state
        return {"job_id": job.spec.job_id, "state": state,
                "output_bytes": job.output_bytes,
                "elapsed_ms": int(
                    (time.monotonic() - job.started_at) * 1000)}

    # -- teardown ---------------------------------------------------------

    def cancel(self, job_id: str, grace_ms: int = 2000) -> dict:
        job = self._require(job_id)
        if type(grace_ms) is not int or grace_ms < 0:
            raise BackendError("grace_ms must be a non-negative integer")
        with job.lock:
            state = job.state
        if state in TERMINAL_STATES:
            return {"state": state, "confirmed": True}
        self._transition(job, "cancelled", "cancel-requested")
        self._kill_container(job, "TERM")
        exited = self._wait_proc(job, grace_ms / 1000.0 if grace_ms else 0)
        if not exited:
            self._kill_container(job, "KILL")
            self._wait_proc(job, 5.0)
        self._remove_container(job)
        confirmed = (job.proc.poll() is not None
                     and not self._container_exists(job.container))
        with job.lock:
            job.receipts.append({"kind": "termination",
                                 "confirmed": confirmed,
                                 "exit_code": job.proc.poll()})
        return {"state": "cancelled", "confirmed": confirmed}

    def close(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job.proc.poll() is None:
            self._kill_container(job, "TERM")
            if not self._wait_proc(job, 2.0):
                self._kill_container(job, "KILL")
                self._wait_proc(job, 5.0)
        for stream in (job.proc.stdin, job.proc.stdout, job.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        for thread in (job.stdout_thread, job.stderr_thread):
            if thread is not None:
                thread.join(timeout=5)
        # Final drain: queued lines may still hold the terminal event.
        try:
            while True:
                item = job.inbox.get_nowait()
                if item is _EOF:
                    job.eof_seen = True
                    continue
                with job.lock:
                    terminal = job.state in TERMINAL_STATES
                if terminal:
                    continue
                event = self._process_line(job, item)
                if event is not None:
                    job.pending.append(event)
        except queue.Empty:
            pass
        container_removed = self._remove_container(job)
        if self._container_exists(job.container):
            container_removed = False
        with job.lock:
            if job.state not in TERMINAL_STATES:
                code = job.proc.poll()
                job.exit_code = code
                job.state = "failed"
                job.receipts.append({"kind": "lifecycle", "state": "failed",
                                     "reason": "worker-exited",
                                     "exit_code": code})
            job.receipts.append({"kind": "output",
                                 "bytes": job.output_bytes,
                                 "truncated": any(
                                     r.get("truncated") for r in job.receipts)})
            if job.stderr_lines:
                job.receipts.append({"kind": "stderr",
                                     "lines": list(job.stderr_lines)})
            job.receipts.append({"kind": "container",
                                 "name": job.container,
                                 "removed": container_removed})
            receipts = list(job.receipts)
            state = job.state
        try:
            verify_mount_source(job.workspace, workspace_root=self._root,
                                must_exist=False)
        except BackendError:
            pass
        else:
            shutil.rmtree(job.workspace, ignore_errors=True)
        with self._guard:
            self._jobs.pop(job_id, None)
        return {"job_id": job_id, "state": state, "receipts": receipts}

    def sweep_own_containers(self) -> dict:
        """Remove stopped/orphaned containers carrying our prefix only."""
        try:
            listed = self._docker(
                ["ps", "-aq", "--filter", f"name={CONTAINER_PREFIX}"],
                timeout=_DOCKER_TIMEOUT)
        except BackendUnavailableError:
            raise
        except BackendError as error:
            raise BackendError(f"container sweep failed: {error}") from error
        if listed.returncode != 0:
            raise BackendError(
                f"container sweep failed: {(listed.stderr or '').strip()}")
        removed: list[str] = []
        errors: list[str] = []
        for line in (listed.stdout or "").splitlines():
            token = line.strip()
            if not token:
                continue
            name = token
            if "/" in token or not token.startswith(CONTAINER_PREFIX):
                # `ps` printed an id: resolve it to a name before touching.
                try:
                    inspected = self._docker(
                        ["inspect", "--format", "{{.Name}}", token],
                        timeout=10)
                except BackendError as error:
                    errors.append(f"{token}: {error}")
                    continue
                if inspected.returncode != 0:
                    continue
                name = (inspected.stdout or "").strip().lstrip("/")
            if not owns_container_name(name):
                continue
            try:
                done = self._docker(["rm", "-f", name], timeout=15)
            except BackendError as error:
                errors.append(f"{name}: {error}")
                continue
            if done.returncode == 0:
                removed.append(name)
            else:
                errors.append(f"{name}: {(done.stderr or '').strip()}")
        return {"removed": removed, "errors": errors}

    def _read_stdout(self, job: _Job) -> None:
        try:
            while True:
                line = _bounded_readline(job.proc.stdout, _LINE_READ_LIMIT)
                if line is None:
                    break
                job.inbox.put(line)
        except (OSError, ValueError):
            pass
        finally:
            job.inbox.put(_EOF)

    def _read_stderr(self, job: _Job) -> None:
        try:
            while True:
                line = job.proc.stderr.readline(8192)
                if not line:
                    break
                with job.lock:
                    if (len(job.stderr_lines) < _MAX_STDERR_LINES
                            and job.stderr_bytes < _MAX_STDERR_BYTES):
                        text = line.decode("utf-8", "replace").rstrip("\r\n")
                        job.stderr_lines.append(text)
                        job.stderr_bytes += len(line)
        except (OSError, ValueError):
            pass
