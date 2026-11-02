"""Proxy-backed booking adapter over the 9-method env protocol (Track E1).

``ProxyEnvAdapter`` speaks to the real loopback booking service through the
boundary proxy: tool invokes are POSTed via the proxy (so faults inject on a
real HTTP boundary), while ``/reset`` and ``/state`` go directly to the
service so the transcript holds exactly one entry per accepted invoke. Each
instance starts its own service and proxy on ephemeral loopback ports, so
sibling adapters are fully isolated.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from copy import deepcopy

from ghostlab.envs.adapters import EnvCancelled, EnvError
from ghostlab.proxy import BoundaryProxy

from .service import FIXTURE_KEYS, TOOL_SCHEMAS, BookingService, is_valid_id

ENV_ID = "booking-proxy-env"
ENV_VERSION = "booking-proxy-env/1"

BOOKING_TOOLS = [
    {"name": name, "parameters": list(TOOL_SCHEMAS[name])}
    for name in ("get_equipment", "get_booking_status", "book_equipment")
]

BOOKING_MODES = ["recorded-actions", "deterministic-reset"]
BOOKING_FAULT_HOOKS = ["timeout_after_commit", "permission_revoked"]

HOOK_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TIMEOUT_PATH_SUFFIX = "/book_equipment"
_HTTP_TIMEOUT = 10.0


def _post_json(url: str, payload) -> tuple[int | None, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            body = {"error": "HttpError", "message": f"upstream HTTP {error.code}"}
        return error.code, body if isinstance(body, dict) else {"error": "HttpError",
                                                                "message": str(body)[:200]}
    except (OSError, ValueError) as error:
        return None, {"error": "UpstreamUnreachable", "message": str(error)[:200]}


def _get_json(url: str) -> tuple[int | None, dict]:
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, {"error": "HttpError",
                            "message": f"upstream HTTP {error.code}"}
    except (OSError, ValueError) as error:
        return None, {"error": "UpstreamUnreachable", "message": str(error)[:200]}


class ProxyEnvAdapter:
    """The loopback service-plus-proxy world behind the shared protocol."""

    env_id = ENV_ID
    version = ENV_VERSION

    def __init__(self) -> None:
        self._service = BookingService()
        try:
            self._service.start()
            self._proxy = BoundaryProxy(self._service.base_url)
            self._proxy.start()
        except Exception:
            self._service.stop()
            raise
        self._trace: list[dict] = []
        self._armed: set[str] = set()
        self._fixture: dict | None = None

    @property
    def transcript(self) -> list[dict]:
        return self._proxy.transcript

    def describe(self) -> dict:
        return {
            "env_id": self.env_id,
            "version": self.version,
            "tools": deepcopy(BOOKING_TOOLS),
            "reproduction_modes": list(BOOKING_MODES),
            "fault_hooks": list(BOOKING_FAULT_HOOKS),
            "supports_cancellation": True,
            "isolation": "loopback-http-checked-in-local",
        }

    def reset(self, fixture: dict) -> None:
        if type(fixture) is not dict or set(fixture) != set(FIXTURE_KEYS):
            raise ValueError("fixture must carry exactly the booking fields")
        for value in fixture.values():
            if not is_valid_id(value):
                raise ValueError("Invalid fixture identifier")
        status, body = _post_json(self._service.base_url + "/reset", fixture)
        if status != 200:
            raise EnvError(f"reset failed: status={status} body={body!r}"[:300])
        self._fixture = {key: fixture[key] for key in FIXTURE_KEYS}
        self._proxy.clear_transcript()
        self._trace = []
        self._apply_armed()

    def _ready(self) -> None:
        if self._fixture is None:
            raise EnvError("environment has not been reset")

    def _apply_armed(self) -> None:
        if "timeout_after_commit" in self._armed:
            self._proxy.arm_timeout_after_commit(TIMEOUT_PATH_SUFFIX, occurrence=1)
        else:
            self._proxy.disarm_timeout()
        self._proxy.set_revoked("permission_revoked" in self._armed)

    def invoke(self, tool: str, arguments: dict) -> dict:
        self._ready()
        if type(tool) is not str or tool not in TOOL_SCHEMAS:
            raise EnvError(f"unknown tool '{tool}'")
        if type(arguments) is not dict or set(arguments) != set(TOOL_SCHEMAS[tool]):
            raise EnvError(f"arguments do not match tool schema for '{tool}'")
        for value in arguments.values():
            if not is_valid_id(value):
                raise EnvError(f"invalid string argument for '{tool}'")
        status, body = _post_json(f"{self._proxy.base_url}/{tool}", arguments)
        if status == 200 and isinstance(body, dict):
            outcome = {"result": deepcopy(body)}
        else:
            outcome = {
                "error": body.get("error", "UpstreamError") if isinstance(body, dict)
                else "UpstreamError",
                "message": body.get("message", "") if isinstance(body, dict) else "",
                "status": status,
            }
        state = self.snapshot()
        transcript = self._proxy.transcript
        self._trace.append({
            "event_id": len(self._trace) + 1,
            "request": {"tool": tool, "arguments": deepcopy(arguments)},
            "outcome": outcome,
            "state": state,
            "boundary_seq": transcript[-1]["seq"] if transcript else None,
        })
        if "result" in outcome:
            return deepcopy(outcome["result"])
        raise EnvError(f"{tool} failed: status={status}"
                       f" error={outcome['error']} message={outcome['message']}"[:300])

    def invoke_cancellable(self, tool: str, arguments: dict, cancel=None) -> dict:
        if cancel is not None and cancel.is_set():
            raise EnvCancelled("cancelled before dispatch")
        return self.invoke(tool, arguments)

    def snapshot(self) -> dict:
        self._ready()
        status, body = _get_json(self._service.base_url + "/state")
        if status != 200 or not isinstance(body, dict):
            raise EnvError(f"snapshot failed: status={status}")
        return body

    def evidence(self) -> dict:
        return {"snapshot": self.snapshot(), "trace": deepcopy(self._trace)}

    def set_fault(self, name: str, params: dict) -> None:
        if (type(name) is not str or not HOOK_PATTERN.match(name)
                or name not in BOOKING_FAULT_HOOKS):
            raise EnvError(f"unknown fault hook '{name}'")
        if type(params) is not dict or params != {}:
            raise ValueError(f"{name} takes no parameters")
        self._armed.add(name)
        if name == "timeout_after_commit":
            self._proxy.arm_timeout_after_commit(TIMEOUT_PATH_SUFFIX, occurrence=1)
        else:
            self._proxy.set_revoked(True)

    def clear_faults(self) -> None:
        self._armed.clear()
        self._proxy.disarm_timeout()
        self._proxy.set_revoked(False)

    def probe_calls(self, fixture: dict) -> list:
        """Canonical valid sequence the generic runner may execute."""
        if type(fixture) is not dict or not set(FIXTURE_KEYS).issubset(fixture):
            raise ValueError("fixture must carry exactly the booking fields")
        identity = {"equipment_id": fixture["equipment_id"],
                    "user_id": fixture["user_id"]}
        return [
            {"tool": "get_equipment", "arguments": dict(identity)},
            {"tool": "book_equipment", "arguments": {
                **identity, "slot": fixture["slot"],
                "idempotency_key": "qual-probe-1"}},
        ]

    def close(self) -> None:
        try:
            self._proxy.stop()
        except Exception:
            pass
        try:
            self._service.stop()
        except Exception:
            pass

    def __enter__(self) -> "ProxyEnvAdapter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:  # best effort; qualification never calls close()
        try:
            self.close()
        except Exception:
            pass
