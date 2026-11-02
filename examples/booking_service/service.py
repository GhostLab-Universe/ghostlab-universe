"""Deterministic loopback equipment-booking service (Track E1).

Stdlib ``ThreadingHTTPServer`` mirroring the ``examples/equipment_booking``
world tools over JSON: POST ``/get_equipment``, ``/get_booking_status`` and
``/book_equipment``. Test-only endpoints: GET ``/state`` (authoritative
snapshot) and POST ``/reset`` (fixture reset). Reservation ids are
deterministic (``booking-N`` per reset generation). Binds 127.0.0.1 on an
ephemeral port.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL_SCHEMAS = {
    "get_equipment": ("equipment_id", "user_id"),
    "get_booking_status": ("equipment_id", "user_id", "idempotency_key"),
    "book_equipment": ("equipment_id", "user_id", "slot", "idempotency_key"),
}

FIXTURE_KEYS = ("equipment_id", "user_id", "slot")

DEFAULT_FIXTURE = {
    "equipment_id": "synthetic-microscope-1",
    "user_id": "synthetic-user-1",
    "slot": "2026-11-03T09:00Z",
}

_MAX_BODY = 1 << 20


def is_valid_id(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 100


def check_fixture(fixture: dict) -> dict:
    if type(fixture) is not dict or set(fixture) != set(FIXTURE_KEYS):
        raise ValueError("fixture must carry exactly the booking fields")
    for value in fixture.values():
        if not is_valid_id(value):
            raise ValueError("Invalid fixture identifier")
    return {key: fixture[key] for key in FIXTURE_KEYS}


def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError:
        pass


def _handler_for(service: "BookingService") -> type[BaseHTTPRequestHandler]:
    class _ServiceHandler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def log_error(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            service._serve(self)

        def do_POST(self) -> None:
            service._serve(self)

    return _ServiceHandler


class BookingService:
    """Authoritative reservations behind loopback HTTP."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 fixture: dict | None = None) -> None:
        if host != "127.0.0.1":
            raise ValueError("service binds loopback only")
        self._lock = threading.Lock()
        self._fixture = check_fixture(deepcopy(DEFAULT_FIXTURE if fixture is None
                                               else fixture))
        self._reservations: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bind_host = host
        self._bind_port = port

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("service is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        if self._server is not None:
            return self.base_url
        server = ThreadingHTTPServer((self._bind_host, self._bind_port),
                                     _handler_for(self))
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

    def __enter__(self) -> "BookingService":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _snapshot_locked(self) -> dict:
        return {
            "equipment": deepcopy(self._fixture),
            "reservations": deepcopy(self._reservations),
            "total_bookings": len(self._reservations),
        }

    def _dispatch(self, tool: str, arguments: dict) -> tuple[int, dict]:
        if type(arguments) is not dict or set(arguments) != set(TOOL_SCHEMAS[tool]):
            return 400, {"error": "SchemaMismatch",
                         "message": "Arguments do not match tool schema"}
        for value in arguments.values():
            if not is_valid_id(value):
                return 400, {"error": "InvalidArgument",
                             "message": "Invalid string argument"}
        if arguments["user_id"] != self._fixture["user_id"]:
            return 403, {"error": "NotAuthorized",
                         "message": "User is not authorized for this equipment"}
        if arguments["equipment_id"] != self._fixture["equipment_id"]:
            return 404, {"error": "NoSuchEquipment",
                         "message": "Equipment does not exist"}
        if tool == "get_equipment":
            return 200, {**deepcopy(self._fixture), "available": True}
        existing = next((row for row in self._reservations
                         if row["idempotency_key"] == arguments["idempotency_key"]),
                        None)
        if tool == "get_booking_status":
            return 200, {"status": "committed" if existing else "not_found",
                         "booking": deepcopy(existing)}
        if existing is not None:
            if existing["slot"] != arguments["slot"]:
                return 409, {"error": "KeySlotConflict",
                             "message": "Idempotency key reused with different slot"}
            return 200, deepcopy(existing)
        row = {
            "reservation_id": f"booking-{len(self._reservations) + 1}",
            "equipment_id": self._fixture["equipment_id"],
            "user_id": arguments["user_id"],
            "slot": arguments["slot"],
            "idempotency_key": arguments["idempotency_key"],
        }
        self._reservations.append(row)
        return 200, deepcopy(row)

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        path = handler.path.split("?", 1)[0]
        method = handler.command
        if length < 0 or length > _MAX_BODY:
            _send(handler, 413, {"error": "BodyTooLarge",
                                 "message": "request body exceeds 1 MiB"})
            return
        try:
            raw = handler.rfile.read(length) if length else b""
        except OSError:
            return
        if method == "GET" and path == "/state":
            with self._lock:
                _send(handler, 200, self._snapshot_locked())
            return
        if method != "POST":
            _send(handler, 405, {"error": "MethodNotAllowed",
                                 "message": f"{method} is not allowed for {path}"})
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            _send(handler, 400, {"error": "InvalidJson",
                                 "message": "request body is not valid JSON"})
            return
        with self._lock:
            if path == "/reset":
                try:
                    self._fixture = check_fixture(payload)
                except (ValueError, TypeError) as error:
                    _send(handler, 400, {"error": "InvalidFixture",
                                         "message": str(error)})
                    return
                self._reservations = []
                _send(handler, 200, {"reset": True,
                                     "equipment": deepcopy(self._fixture)})
                return
            tool = path.lstrip("/")
            if tool not in TOOL_SCHEMAS:
                _send(handler, 404, {"error": "UnknownTool",
                                     "message": f"Unknown tool path {path}"})
                return
            status, body = self._dispatch(tool, payload)
            _send(handler, status, body)
