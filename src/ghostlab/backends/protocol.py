"""Strict structured job protocol (version 1, JSON only).

Every message is exactly ``{v, job_id, seq, type, payload}`` encoded as one
JSON line. No pickle, no eval, no executable encodings anywhere on this
path. Both directions enforce the same envelope: 64KB per message, payload
nesting depth <= 8, strict field types, explicit sequence checks.
"""

from __future__ import annotations

import json
from typing import Any

from .interface import ProtocolError

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_JOB_ID_LEN = 128
MAX_TYPE_LEN = 64

_MESSAGE_KEYS = frozenset({"v", "job_id", "seq", "type", "payload"})

# Controller -> worker commands implemented by the job worker loop.
WORKER_COMMANDS = frozenset({
    "hello", "reset", "invoke", "set_fault", "clear_faults",
    "evidence", "snapshot", "close",
})

# Worker -> controller events; job output is untrusted until validated here.
WORKER_EVENTS = frozenset({
    "started", "tool_result", "evidence_chunk",
    "completed", "failed", "truncated",
})


def _depth(value: Any) -> int:
    if type(value) is dict:
        if not value:
            return 1
        return 1 + max(_depth(item) for item in value.values())
    if type(value) is list:
        if not value:
            return 1
        return 1 + max(_depth(item) for item in value)
    return 0


def _check_depth(payload: Any) -> None:
    if _depth(payload) > MAX_DEPTH:
        raise ProtocolError(
            f"payload nesting exceeds depth {MAX_DEPTH}")


def encode_message(job_id: str, seq: int, msg_type: str, payload: dict) -> bytes:
    """Encode one validated message line (no trailing newline)."""
    if type(job_id) is not str or not job_id or len(job_id) > MAX_JOB_ID_LEN:
        raise ProtocolError("invalid job_id")
    if type(seq) is not int or seq < 0:
        raise ProtocolError("seq must be a non-negative integer")
    if type(msg_type) is not str or not msg_type or len(msg_type) > MAX_TYPE_LEN:
        raise ProtocolError("invalid message type")
    if type(payload) is not dict:
        raise ProtocolError("payload must be an object")
    _check_depth(payload)
    try:
        text = json.dumps(
            {"v": PROTOCOL_VERSION, "job_id": job_id, "seq": seq,
             "type": msg_type, "payload": payload},
            ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ProtocolError(f"payload is not strict JSON: {error}") from error
    raw = text.encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            f"message exceeds {MAX_MESSAGE_BYTES} bytes")
    return raw


def _reject_constant(value: str) -> Any:
    raise ProtocolError(f"non-strict JSON constant '{value}'")


def decode_message(raw: Any, *, expected_job: str | None = None,
                   expected_seq: int | None = None) -> dict:
    """Decode and strictly validate one message line.

    ``expected_job``/``expected_seq`` turn identity/ordering mismatches into
    ProtocolError; callers map job-id mismatches to CrossJobError.
    """
    if type(raw) is bytes:
        data = raw
    elif type(raw) is bytearray:
        data = bytes(raw)
    elif type(raw) is str:
        data = raw.encode("utf-8")
    else:
        raise ProtocolError("message must be bytes or str")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            f"message exceeds {MAX_MESSAGE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("message is not valid UTF-8") from error
    try:
        message = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ProtocolError) as error:
        raise ProtocolError(f"message is not strict JSON: {error}") from error
    if type(message) is not dict or set(message) != set(_MESSAGE_KEYS):
        raise ProtocolError("message must be exactly {v,job_id,seq,type,payload}")
    if type(message["v"]) is not int or message["v"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    job_id, seq, msg_type, payload = (
        message["job_id"], message["seq"], message["type"], message["payload"])
    if type(job_id) is not str or not job_id or len(job_id) > MAX_JOB_ID_LEN:
        raise ProtocolError("invalid job_id")
    if type(seq) is not int or seq < 0:
        raise ProtocolError("seq must be a non-negative integer")
    if type(msg_type) is not str or not msg_type or len(msg_type) > MAX_TYPE_LEN:
        raise ProtocolError("invalid message type")
    if type(payload) is not dict:
        raise ProtocolError("payload must be an object")
    _check_depth(payload)
    if expected_job is not None and job_id != expected_job:
        raise ProtocolError("job_id mismatch")
    if expected_seq is not None and seq != expected_seq:
        raise ProtocolError(f"seq mismatch: expected {expected_seq}, got {seq}")
    return message
