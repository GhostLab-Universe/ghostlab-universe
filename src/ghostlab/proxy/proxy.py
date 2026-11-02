"""Loopback boundary proxy with deterministic fault injection (Track E1).

The proxy sits between the agent and the real booking service on loopback
HTTP. Every request it receives is forwarded to the upstream service and
recorded in a boundary transcript, except when a fault is armed:

- ``timeout_after_commit``: the k-th matching POST is forwarded (the
  upstream commit really happens) but the caller receives a 504 instead of
  the upstream 200. Fires once, then auto-disarms.
- ``permission_revoked``: while revoked, mutating POSTs receive a 403
  without ever being forwarded (a mid-flow permission change).

Standard library only. Binds 127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMEOUT_AFTER_COMMIT = "timeout_after_commit"
PERMISSION_REVOKED = "permission_revoked"

MUTATING_PATHS = ("/book_equipment",)

_MAX_BODY = 1 << 20
_UPSTREAM_TIMEOUT = 10.0


def _send(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError:
        pass  # client went away; the transcript entry is already recorded


def _handler_for(proxy: "BoundaryProxy") -> type[BaseHTTPRequestHandler]:
    class _ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet test servers
            pass

        def log_error(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            proxy._serve(self)

        def do_POST(self) -> None:
            proxy._serve(self)

    return _ProxyHandler


class BoundaryProxy:
    """Forwarding loopback proxy that records a boundary transcript."""

    def __init__(self, upstream_base: str, faults: dict | None = None) -> None:
        if (type(upstream_base) is not str
                or not upstream_base.startswith("http://127.0.0.1:")):
            raise ValueError("upstream_base must be a loopback http://127.0.0.1 URL")
        self._upstream = upstream_base.rstrip("/")
        self._lock = threading.Lock()
        self._transcript: list[dict] = []
        self._revoked = False
        self._timeout_suffix: str | None = None
        self._timeout_occurrence = 0
        self._timeout_seen = 0
        self._fired_occurrence: int | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        if faults is not None:
            self._apply_initial_faults(faults)

    def _apply_initial_faults(self, faults: dict) -> None:
        if type(faults) is not dict:
            raise ValueError("faults must be an object")
        for name, config in faults.items():
            if name == TIMEOUT_AFTER_COMMIT:
                if type(config) is not dict:
                    raise ValueError("timeout_after_commit config must be an object")
                self.arm_timeout_after_commit(
                    config.get("path_suffix", "/book_equipment"),
                    occurrence=config.get("occurrence", 1),
                )
            elif name == PERMISSION_REVOKED:
                if type(config) is not bool:
                    raise ValueError("permission_revoked config must be boolean")
                self.set_revoked(config)
            else:
                raise ValueError(f"unknown fault '{name}'")

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("proxy is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def transcript(self) -> list[dict]:
        with self._lock:
            return deepcopy(self._transcript)

    @property
    def fired_occurrence(self) -> int | None:
        with self._lock:
            return self._fired_occurrence

    @property
    def revoked(self) -> bool:
        with self._lock:
            return self._revoked

    @property
    def timeout_armed(self) -> bool:
        with self._lock:
            return self._timeout_suffix is not None

    def clear_transcript(self) -> None:
        with self._lock:
            self._transcript = []

    def arm_timeout_after_commit(self, path_suffix: str = "/book_equipment",
                                 occurrence: int = 1) -> None:
        """Drop the response of the k-th matching POST after forwarding it."""
        if type(path_suffix) is not str or not path_suffix.startswith("/"):
            raise ValueError("path_suffix must be a non-empty absolute path suffix")
        if type(occurrence) is not int or occurrence < 1:
            raise ValueError("occurrence must be an integer >= 1")
        with self._lock:
            self._timeout_suffix = path_suffix
            self._timeout_occurrence = occurrence
            self._timeout_seen = 0

    def disarm_timeout(self) -> None:
        with self._lock:
            self._timeout_suffix = None
            self._timeout_seen = 0

    def set_revoked(self, revoked: bool) -> None:
        if type(revoked) is not bool:
            raise ValueError("revoked must be boolean")
        with self._lock:
            self._revoked = revoked

    def start(self) -> str:
        if self._server is not None:
            return self.base_url
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=5)

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> "BoundaryProxy":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _forward(self, path: str, raw: bytes, method: str) -> tuple[int | None, bytes]:
        url = self._upstream + path
        request = urllib.request.Request(
            url, data=raw if raw else None, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=_UPSTREAM_TIMEOUT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read()
            except OSError:
                return error.code, b""
        except (OSError, ValueError):
            return None, b""

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        path = handler.path.split("?", 1)[0]
        method = handler.command
        if length < 0 or length > _MAX_BODY:
            with self._lock:
                self._transcript.append({
                    "seq": len(self._transcript) + 1, "method": method,
                    "path": path, "request": None, "upstream_status": None,
                    "injected": None,
                })
            _send(handler, 413, {"error": "BodyTooLarge",
                                 "message": "request body exceeds 1 MiB"})
            return
        try:
            raw = handler.rfile.read(length) if length else b""
        except OSError:
            return
        try:
            request = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            request = None
        with self._lock:
            seq = len(self._transcript) + 1
            if self._revoked and method == "POST" and path in MUTATING_PATHS:
                self._transcript.append({
                    "seq": seq, "method": method, "path": path,
                    "request": deepcopy(request), "upstream_status": None,
                    "injected": PERMISSION_REVOKED,
                })
                _send(handler, 403, {
                    "error": "PermissionRevoked",
                    "message": "booking permission was revoked mid-flow;"
                               " request was not forwarded upstream",
                })
                return
            status, body = self._forward(path, raw, method)
            if (self._timeout_suffix is not None and method == "POST"
                    and path.endswith(self._timeout_suffix)):
                self._timeout_seen += 1
                if self._timeout_seen == self._timeout_occurrence:
                    self._fired_occurrence = self._timeout_occurrence
                    self._timeout_suffix = None  # fire once, then auto-disarm
                    self._timeout_seen = 0
                    self._transcript.append({
                        "seq": seq, "method": method, "path": path,
                        "request": deepcopy(request),
                        "upstream_status": status,
                        "injected": TIMEOUT_AFTER_COMMIT,
                    })
                    _send(handler, 504, {
                        "error": "TimeoutAfterCommit",
                        "message": "upstream committed; boundary dropped"
                                   " the response (timeout_after_commit)",
                    })
                    return
            self._transcript.append({
                "seq": seq, "method": method, "path": path,
                "request": deepcopy(request), "upstream_status": status,
                "injected": None,
            })
            if status is None:
                _send(handler, 502, {"error": "BadGateway",
                                     "message": "upstream is unreachable"})
            else:
                _send(handler, status, body)
