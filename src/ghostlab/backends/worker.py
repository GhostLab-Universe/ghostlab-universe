"""Job-side stdio worker: ``python -m ghostlab.backends.worker``.

Runs INSIDE a job (spawned by a backend with cwd set to the per-job
workspace). Reads JSON-line commands on stdin, executes them against one
allowlisted adapter import, and writes JSON-line events on stdout. A hard
output cap triggers a ``truncated`` event followed by exit. Unexpected
tracebacks go to stderr, where the backend captures and labels them.

Configuration arrives only via the scrubbed environment the backend
builds: GHOSTLAB_JOB_ID, GHOSTLAB_ADAPTER_REF,
GHOSTLAB_ALLOWED_PREFIXES (comma-separated, default ``ghostlab.``), and
GHOSTLAB_MAX_OUTPUT_BYTES.
"""

from __future__ import annotations

import os
import sys

from . import protocol as _protocol
from .interface import ProtocolError

_COMMANDS = _protocol.WORKER_COMMANDS


def _fail(message: str) -> int:
    sys.stderr.write(f"[worker] {message}\n")
    sys.stderr.flush()
    return 1


class Worker:
    def __init__(self, job_id: str, adapter_ref: str,
                 allowed_prefixes: tuple[str, ...],
                 max_output_bytes: int) -> None:
        self.job_id = job_id
        self.adapter_ref = adapter_ref
        self.allowed_prefixes = allowed_prefixes
        self.max_output_bytes = max_output_bytes
        self.adapter = None
        self.hello_seen = False
        self.request_seq = 0
        self.event_seq = 0
        self.output_bytes = 0
        self.closed = False

    def _emit_raw(self, event_type: str, payload: dict) -> bool:
        try:
            line = _protocol.encode_message(
                self.job_id, self.event_seq, event_type, payload)
        except ProtocolError:
            return False
        if self.output_bytes + len(line) + 1 > self.max_output_bytes:
            return False
        try:
            sys.stdout.buffer.write(line + b"\n")
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            return False
        self.output_bytes += len(line) + 1
        self.event_seq += 1
        return True

    def emit(self, event_type: str, payload: dict) -> bool:
        return self._emit_raw(event_type, payload)

    def emit_truncated_and_exit(self) -> int:
        try:
            line = _protocol.encode_message(
                self.job_id, self.event_seq, "truncated",
                {"reason": "output-exhausted",
                 "output_bytes": self.output_bytes})
        except ProtocolError:
            return 1
        if self.output_bytes + len(line) + 1 <= self.max_output_bytes:
            try:
                sys.stdout.buffer.write(line + b"\n")
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError):
                pass
        return 0

    def _tool_result(self, ok: bool, result=None, error=None,
                     message=None) -> dict:
        payload: dict = {"ok": ok}
        if ok:
            if result is not None:
                payload["result"] = result
        else:
            payload["error"] = error or "AdapterError"
            payload["message"] = str(message or "")[:500]
        return payload

    def _adapter_call(self, name: str, *args):
        try:
            return True, getattr(self.adapter, name)(*args)
        except Exception as error:  # adapter bugs stay inside the job
            return False, (type(error).__name__, str(error))

def _resolve_adapter(adapter_ref: str, allowed_prefixes: tuple[str, ...]):
    from ..envs.adapters import resolve_adapter_ref
    cls = resolve_adapter_ref(adapter_ref, allowed_prefixes)
    return cls()


def _handle_command(worker: Worker, cmd_type: str, payload: dict) -> str:
    if cmd_type == "hello":
        if worker.hello_seen:
            worker.emit("failed", {"error": "ProtocolViolation",
                                   "message": "duplicate hello"})
            return "failed"
        try:
            worker.adapter = _resolve_adapter(
                worker.adapter_ref, worker.allowed_prefixes)
            describe = worker.adapter.describe()
            if type(describe) is not dict:
                raise ValueError("describe() must return an object")
        except Exception as error:
            worker.emit("failed", {"error": type(error).__name__,
                                   "message": str(error)[:500]})
            return "failed"
        worker.hello_seen = True
        if not worker.emit("started", {"adapter": worker.adapter_ref,
                                       "describe": describe}):
            return "truncated"
        return "ok"
    if not worker.hello_seen or worker.adapter is None:
        worker.emit("failed", {"error": "ProtocolViolation",
                               "message": "hello required first"})
        return "failed"
    if cmd_type == "reset":
        fixture = payload.get("fixture")
        if type(fixture) is not dict:
            worker.emit("failed", {"error": "ProtocolViolation",
                                   "message": "reset needs a fixture object"})
            return "failed"
        ok, outcome = worker._adapter_call("reset", fixture)
        if not ok:
            name, message = outcome
            if not worker.emit("tool_result", worker._tool_result(
                    False, error=name, message=message)):
                return "truncated"
            return "ok"
        if not worker.emit("tool_result", worker._tool_result(True)):
            return "truncated"
        return "ok"
    if cmd_type == "invoke":
        tool, arguments = payload.get("tool"), payload.get("arguments")
        if type(tool) is not str or type(arguments) is not dict:
            worker.emit("failed", {"error": "ProtocolViolation",
                                   "message": "invoke needs tool+arguments"})
            return "failed"
        ok, outcome = worker._adapter_call("invoke", tool, arguments)
        if not ok:
            name, message = outcome
            payload_out = worker._tool_result(False, error=name,
                                              message=message)
        else:
            payload_out = worker._tool_result(True, result=outcome)
        if not worker.emit("tool_result", payload_out):
            return "truncated"
        return "ok"
    if cmd_type == "set_fault":
        name, params = payload.get("name"), payload.get("params")
        if type(name) is not str or type(params) is not dict:
            worker.emit("failed", {"error": "ProtocolViolation",
                                   "message": "set_fault needs name+params"})
            return "failed"
        ok, outcome = worker._adapter_call("set_fault", name, params)
        if not ok:
            err_name, message = outcome
            result: dict = worker._tool_result(False, error=err_name,
                                               message=message)
        else:
            result = worker._tool_result(True)
        if not worker.emit("tool_result", result):
            return "truncated"
        return "ok"
    if cmd_type == "clear_faults":
        ok, outcome = worker._adapter_call("clear_faults")
        if not ok:
            err_name, message = outcome
            result = worker._tool_result(False, error=err_name,
                                         message=message)
        else:
            result = worker._tool_result(True)
        if not worker.emit("tool_result", result):
            return "truncated"
        return "ok"
    if cmd_type == "evidence":
        ok, outcome = worker._adapter_call("evidence")
        if not ok:
            err_name, message = outcome
            if not worker.emit("tool_result", worker._tool_result(
                    False, error=err_name, message=message)):
                return "truncated"
            return "ok"
        if not worker.emit("evidence_chunk", {"evidence": outcome}):
            return "truncated"
        return "ok"
    if cmd_type == "snapshot":
        ok, outcome = worker._adapter_call("snapshot")
        if not ok:
            err_name, message = outcome
            result = worker._tool_result(False, error=err_name,
                                         message=message)
        else:
            result = worker._tool_result(True, result=outcome)
        if not worker.emit("tool_result", result):
            return "truncated"
        return "ok"
    if cmd_type == "close":
        worker.emit("completed", {})
        worker.closed = True
        return "close"
    worker.emit("failed", {"error": "UnknownCommand",
                           "message": f"unknown command '{cmd_type}'"})
    return "failed"


def _read_config() -> tuple:
    job_id = os.environ.get("GHOSTLAB_JOB_ID", "")
    adapter_ref = os.environ.get("GHOSTLAB_ADAPTER_REF", "")
    prefixes = os.environ.get("GHOSTLAB_ALLOWED_PREFIXES", "ghostlab.")
    cap = os.environ.get("GHOSTLAB_MAX_OUTPUT_BYTES", "")
    if not job_id or not adapter_ref:
        raise ValueError("GHOSTLAB_JOB_ID and GHOSTLAB_ADAPTER_REF required")
    allowed = tuple(p.strip() for p in prefixes.split(",") if p.strip())
    if not allowed:
        raise ValueError("GHOSTLAB_ALLOWED_PREFIXES must not be empty")
    try:
        max_bytes = int(cap)
    except (TypeError, ValueError):
        raise ValueError("GHOSTLAB_MAX_OUTPUT_BYTES must be an integer")
    if max_bytes < 1:
        raise ValueError("GHOSTLAB_MAX_OUTPUT_BYTES must be positive")
    return job_id, adapter_ref, allowed, max_bytes


def main() -> int:
    try:
        cwd = os.getcwd()
        sys.path = [entry for entry in sys.path
                    if os.path.abspath(entry or ".") != cwd]
    except OSError:
        pass
    try:
        job_id, adapter_ref, allowed, max_bytes = _read_config()
    except ValueError as error:
        return _fail(str(error))
    worker = Worker(job_id, adapter_ref, allowed, max_bytes)
    stdin = sys.stdin.buffer
    while True:
        try:
            line = stdin.readline(_protocol.MAX_MESSAGE_BYTES + 2)
        except OSError as error:
            worker.emit("failed", {"error": "StdinError",
                                   "message": str(error)[:200]})
            return 1
        if not line:
            return 0 if worker.closed else 1
        if not line.strip():
            continue
        try:
            command = _protocol.decode_message(
                line.strip(), expected_job=job_id,
                expected_seq=worker.request_seq)
        except ProtocolError as error:
            worker.emit("failed", {"error": "ProtocolViolation",
                                   "message": str(error)[:500]})
            return 1
        worker.request_seq += 1
        if command["type"] not in _COMMANDS:
            worker.emit("failed", {
                "error": "UnknownCommand",
                "message": f"unknown command '{command['type']}'"})
            return 1
        if type(command["payload"]) is not dict:
            worker.emit("failed", {"error": "ProtocolViolation",
                                   "message": "command payload must be an object"})
            return 1
        outcome = _handle_command(worker, command["type"], command["payload"])
        if outcome == "close":
            return 0
        if outcome == "failed":
            return 1
        if outcome == "truncated":
            return worker.emit_truncated_and_exit()
    # unreachable


if __name__ == "__main__":
    sys.exit(main())
