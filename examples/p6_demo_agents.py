"""Plain-Python demo target agents (P6): dicts in, dicts out.

Deliberately zero imports: these classes prove an external developer agent
needs no GhostLab dependency to be tested. The demo wraps them with
``ExternalTargetAdapter``; proposals stay untrusted data until the gateway
and the world validate them.
"""


class BlindBooker:
    """Books 2 seats without checking availability (intentionally defective)."""

    agent_id = "demo-booker"
    agent_version = "demo/1"
    model = None
    provider = None
    endpoint = None

    def __init__(self):
        self._done = False

    def start(self, task, observation):
        self._done = False

    def propose(self, observation):
        if self._done:
            return None
        self._done = True
        return {"tool": "reserve", "arguments": {"holder": "demo", "seats": 2}}

    def notify_result(self, action, outcome):
        pass

    def close(self, reason):
        pass


class CarefulBooker:
    """Checks availability, books within it, abstains on stale reads (fixed)."""

    agent_id = "demo-booker"
    agent_version = "demo/2"
    model = None
    provider = None
    endpoint = None

    def __init__(self):
        self._phase = 0
        self._available = 0
        self._stale = False

    def start(self, task, observation):
        self._phase = 0
        self._available = 0
        self._stale = False

    def propose(self, observation):
        if self._phase == 0:
            return {"tool": "check_availability", "arguments": {}}
        if self._phase == 1:
            self._phase = 2
            if self._stale or self._available < 1:
                return None
            seats = min(2, self._available)
            return {"tool": "reserve", "arguments": {"holder": "demo", "seats": seats}}
        return None

    def notify_result(self, action, outcome):
        if self._phase != 0 or not isinstance(action, dict):
            return
        if action.get("tool") != "check_availability" or not isinstance(outcome, dict):
            return
        result = outcome.get("result")
        if not isinstance(result, dict):
            return
        available = result.get("available", 0)
        self._available = available if type(available) is int else 0
        self._stale = result.get("stale", False) is True
        self._phase = 1

    def close(self, reason):
        pass
