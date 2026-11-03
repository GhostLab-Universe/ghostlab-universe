"""Reference local-process backend (protocol/lifecycle only, NEVER a sandbox).

:class:`LocalBackend` implements the frozen :class:`ExecutionBackend`
surface by spawning ``python -m ghostlab.backends.worker`` as a plain OS
child process per job. It exists to validate protocol handling, lifecycle
transitions, budgets, and evidence plumbing — it provides NO isolation:
the child shares the host filesystem, process table, and network, and
``capabilities()`` reports exactly that (``unqualified``).

LIMITATIONS (honest, by design):
- No filesystem/process/network isolation; a curious job can read the
  host, siblings, and the repo. Never run unreviewed code here.
- Cancel terminates the direct child only; on Windows especially,
  detached grandchildren may survive (no job-object/descendant kill).
- Resource enforcement covers wall-clock time, output bytes, and message
  shape only — no CPU/memory/PID/disk quotas.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import intake as _intake
from . import protocol as _protocol
from .interface import (TERMINAL_STATES, BackendError, ExecutionBackend,
                        JobSpec)

# Test-only hook: override the ``-m`` worker module so tests can spawn a
# bad-worker shim without editing worker.py. Never set in production.
WORKER_MODULE_ENV = "GHOSTLAB_BACKEND_WORKER_MODULE"
WORKER_MODULE_DEFAULT = "ghostlab.backends.worker"

_MODULE_PATTERN = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")

# What the child is allowed to inherit; everything else is scrubbed.
_PASSTHROUGH_ENV = ("PATH", "SYSTEMROOT", "PYTHONPATH")

_MAX_STDERR_LINES = 200
_MAX_STDERR_BYTES = 64 * 1024
_LINE_READ_LIMIT = _protocol.MAX_MESSAGE_BYTES + 2

_EOF = object()
_OVERSIZE_LINE = object()


def _worker_module() -> str:
    name = os.environ.get(WORKER_MODULE_ENV, WORKER_MODULE_DEFAULT)
    if type(name) is not str or not _MODULE_PATTERN.match(name):
        raise BackendError("invalid worker module override")
    return name


def _bounded_readline(stream, limit: int):
    """Read one \\n-terminated line capped at ``limit`` bytes."""
    chunks: list = []
    total = 0
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if chunk == b"\n":
            break
        if total > limit:
            # Drain to end of line so the stream stays framed, then flag.
            while True:
                rest = stream.read(4096)
                if not rest or rest.endswith(b"\n") or b"\n" in rest:
                    break
            return _OVERSIZE_LINE
    if not chunks:
        return None
    return b"".join(chunks)


class _Job:
    def __init__(self, spec: JobSpec, workspace: str, proc) -> None:
        self.spec = spec
        self.workspace = workspace
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


class LocalBackend(ExecutionBackend):
    """OS-process reference backend; see module docstring (not a sandbox)."""

    name: str = "local"

    def __init__(self, workspace_root) -> None:
        if isinstance(workspace_root, Path):
            workspace_root = str(workspace_root)
        if type(workspace_root) is not str or not workspace_root:
            raise ValueError("workspace_root must be a path string")
        self._root = os.path.realpath(workspace_root)
        os.makedirs(self._root, exist_ok=True)
        if not os.path.isdir(self._root):
            raise BackendError("workspace root is not a directory")
        self._jobs: dict[str, _Job] = {}
        self._prepared: dict[str, dict] = {}
        self._guard = threading.Lock()

    # -- capabilities / prepare ----------------------------------------

    def capabilities(self) -> dict:
        return {
            "isolation": {"filesystem": "none", "process": "os-process",
                          "network": "host"},
            "limits": ["wall_clock_ms", "max_output_bytes",
                       "max_message_bytes", "max_message_depth",
                       "max_archive_bytes", "max_archive_files"],
            "qualification": {"status": "unqualified"},
        }

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
        workspace = os.path.realpath(
            os.path.join(self._root, spec.job_id))
        if workspace != os.path.join(self._root, spec.job_id):
            raise BackendError("job workspace escapes the root")
        if os.path.lexists(workspace):
            raise BackendError(f"workspace for job '{spec.job_id}' exists")
        os.makedirs(workspace)
        if prepared is not None:
            _intake.extract_archive(
                prepared["data"], prepared["kind"], workspace,
                max_bytes=_intake.DEFAULT_MAX_ARCHIVE_BYTES,
                max_files=_intake.DEFAULT_MAX_FILES,
                max_ratio=_intake.DEFAULT_MAX_RATIO)
        env = {key: os.environ[key] for key in _PASSTHROUGH_ENV
               if key in os.environ}
        if "PYTHONPATH" in env:
            # The child runs with cwd set to the job workspace, so relative
            # entries (e.g. PYTHONPATH=src) must be anchored to the
            # controller's cwd or the worker cannot import ghostlab.
            anchored = []
            for entry in env["PYTHONPATH"].split(os.pathsep):
                anchored.append(os.path.abspath(entry or os.getcwd()))
            env["PYTHONPATH"] = os.pathsep.join(anchored)
        env["GHOSTLAB_JOB_ID"] = spec.job_id
        env["GHOSTLAB_ADAPTER_REF"] = spec.adapter_ref
        env["GHOSTLAB_ALLOWED_PREFIXES"] = "ghostlab."
        env["GHOSTLAB_MAX_OUTPUT_BYTES"] = str(
            spec.limits["max_output_bytes"])
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", _worker_module()],
                cwd=workspace, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        except OSError as error:
            shutil.rmtree(workspace, ignore_errors=True)
            raise BackendError(f"worker spawn failed: {error}") from error
        job = _Job(spec, workspace, proc)
        with self._guard:
            self._jobs[spec.job_id] = job
        stdout_thread = threading.Thread(
            target=self._read_stdout, args=(job,), daemon=True,
            name=f"ghostlab-out-{spec.job_id}")
        stderr_thread = threading.Thread(
            target=self._read_stderr, args=(job,), daemon=True,
            name=f"ghostlab-err-{spec.job_id}")
        job.stdout_thread = stdout_thread
        job.stderr_thread = stderr_thread
        stdout_thread.start()
        stderr_thread.start()

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

    # -- messaging ------------------------------------------------------

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
        self._terminate(job)
        self._transition(job, "timed_out", "wall-clock-exhausted",
                         {"wall_clock_ms": job.spec.limits["wall_clock_ms"]})

    def _terminate(self, job: _Job) -> None:
        try:
            job.proc.terminate()
        except (OSError, ValueError):
            pass

    def _process_line(self, job: _Job, raw) -> dict | None:
        if raw is _OVERSIZE_LINE:
            self._terminate(job)
            try:
                job.proc.kill()
            except (OSError, ValueError):
                pass
            self._transition(job, "failed", "worker-line-oversize",
                             {"truncated": True})
            return self._synth(job, "failed",
                               {"error": "oversize-worker-line",
                                "truncated": True})
        with job.lock:
            cap = job.spec.limits["max_output_bytes"]
        job.output_bytes += len(raw)
        if job.output_bytes > cap:
            self._terminate(job)
            try:
                job.proc.kill()
            except (OSError, ValueError):
                pass
            self._transition(job, "failed", "output-exhausted",
                             {"truncated": True,
                              "output_bytes": job.output_bytes})
            return self._synth(job, "failed",
                               {"error": "output-exhausted",
                                "truncated": True})
        try:
            event = _protocol.decode_message(raw)
        except _protocol.ProtocolError as error:
            self._terminate(job)
            self._transition(job, "failed", "malformed-worker-output",
                             {"error": str(error)[:300]})
            return self._synth(job, "failed",
                               {"error": "malformed-worker-output",
                                "message": str(error)[:300]})
        if event["job_id"] != job.spec.job_id:
            self._terminate(job)
            self._transition(job, "failed", "cross-job-output")
            return self._synth(job, "failed",
                               {"error": "cross-job-output"})
        if event["seq"] != job.event_seq:
            self._terminate(job)
            self._transition(job, "failed", "worker-seq-gap",
                             {"expected": job.event_seq,
                              "got": event["seq"]})
            return self._synth(job, "failed",
                               {"error": "worker-seq-gap"})
        if event["type"] not in _protocol.WORKER_EVENTS:
            self._terminate(job)
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
                if terminal or deadline is None:
                    item = job.inbox.get_nowait()
                else:
                    item = job.inbox.get(timeout=timeout_left)
            except queue.Empty:
                if terminal:
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
                    # Worker closed stdout but lives on: keep waiting only
                    # while the caller still has time budget.
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
        self._enforce_deadline(job)
        # Status never consumes events: peek at liveness only.
        if job.proc.poll() is not None and job.eof_seen:
            with job.lock:
                terminal = job.state in TERMINAL_STATES
            if not terminal:
                # Process is gone but queued lines may still hold the
                # terminal event; leave the verdict to poll()/close().
                pass
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
        self._terminate(job)
        try:
            job.proc.wait(timeout=grace_ms / 1000.0 if grace_ms else 0)
        except subprocess.TimeoutExpired:
            try:
                job.proc.kill()
            except (OSError, ValueError):
                pass
            try:
                job.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        except (OSError, ValueError):
            pass
        confirmed = job.proc.poll() is not None
        with job.lock:
            job.receipts.append({"kind": "termination",
                                 "confirmed": confirmed,
                                 "exit_code": job.proc.poll()})
        return {"state": "cancelled", "confirmed": confirmed}

    def close(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job.proc.poll() is None:
            self._terminate(job)
            try:
                job.proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                try:
                    job.proc.kill()
                except (OSError, ValueError):
                    pass
                try:
                    job.proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    pass
        for stream in (job.proc.stdin, job.proc.stdout, job.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        for thread in (getattr(job, "stdout_thread", None),
                       getattr(job, "stderr_thread", None)):
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
            receipts = list(job.receipts)
            state = job.state
        shutil.rmtree(job.workspace, ignore_errors=True)
        with self._guard:
            self._jobs.pop(job_id, None)
        return {"job_id": job_id, "state": state, "receipts": receipts}
