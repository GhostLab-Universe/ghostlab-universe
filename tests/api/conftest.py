"""Shared M07 test stack: the real API over REAL M04 stores and the REAL M01 adapter.

Every provider response is a labeled fixture on an httpx.MockTransport (see
``tests/orchestration/m05_harness.py``). No network request is made and no model is
called. HTTP goes through the real middleware, handlers and background workers.
"""

import sys
import time
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestration"))

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette prefers httpx2; the lock pins httpx
    from starlette.testclient import TestClient

from ghostlab.api import create_app  # noqa: E402
from ghostlab.api.plane import ApiConfig  # noqa: E402
from ghostlab.api.sessions import TokenSessions  # noqa: E402
from ghostlab.contracts import Principal  # noqa: E402
from ghostlab.policy import (ProjectApproverAllowlist, StaticPrincipalBinding,  # noqa: E402
                             StaticWorldBinding)
from ghostlab.security.redaction import Redactor  # noqa: E402
from m05_harness import OPERATOR, PROJECT, REVIEWER, SECRET, STRANGER, Script  # noqa: E402
from m05_journey_harness import J, ORGANIZER, JourneyLab  # noqa: E402

OTHER_PROJECT = "project-other"
OUTSIDER = Principal(principal_id="outsider-1", kind="human")
SERVICE = Principal(principal_id="service-1", kind="service")
TOKENS = {"operator": (OPERATOR, PROJECT), "reviewer": (REVIEWER, PROJECT),
          "stranger": (STRANGER, PROJECT), "service": (SERVICE, PROJECT),
          "outsider": (OUTSIDER, OTHER_PROJECT)}
LOOPBACK = {"base_url": "http://127.0.0.1", "client": ("127.0.0.1", 50000)}
DONE = {"completed", "failed", "cancelled", "budget_exhausted"}
POLL_S = 0.01
WAIT_S = 30.0


def spec_json(world="refund-world/1", **budget):
    limits = {"max_model_calls": 10, "max_tool_calls": 100, "max_input_tokens": 100_000,
              "max_output_tokens": 20_000, "max_cost_minor": None, "cost_currency": None,
              "max_wall_clock_ms": 20 * 60_000, "max_parallel_runs": 2, **budget}
    return {"schema_version": 1, "objective": "Find a failure after a fault",
            "success_criteria": ["A failing invariant is reproduced and a repair is verified"],
            "world_version": world, "budget": limits}


class Api:
    """One server. Named identities are bearer tokens resolved on the server."""

    def __init__(self, client, lab):
        self.client, self.lab, self.script = client, lab, lab.script

    def call(self, method, path, *, who="operator", key=None, **options):
        headers = dict(options.pop("headers", {}))
        if who is not None:
            headers["Authorization"] = f"Bearer {who}"
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.request(method, path, headers=headers, **options)

    def get(self, path, **options):
        return self.call("GET", path, **options)

    def post(self, path, **options):
        return self.call("POST", path, **options)

    def create(self, world="refund-world/1", *, key="key-1", who="operator", **budget):
        return self.post("/v1/campaigns", key=key, who=who, json=spec_json(world, **budget))

    def view(self, campaign_id, who="operator"):
        response = self.get(f"/v1/campaigns/{campaign_id}", who=who)
        assert response.status_code == 200, response.text
        return response.json()

    def wait(self, campaign_id, *statuses, who="operator"):
        """Wait until a worker has finished and the campaign reached a status."""
        wanted = set(statuses) or DONE
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            view = self.view(campaign_id, who=who)
            if view["record"]["status"] in wanted and view["job"]["state"] != "active":
                return view
            time.sleep(POLL_S)
        raise AssertionError(f"timed out in {view['record']['status']}: {view['job']}")

    def paused(self, world="refund-world/1", **options):
        created = self.create(world, **options)
        assert created.status_code == 202, created.text
        return self.wait(created.json()["record"]["campaign_id"], "awaiting_approval")

    def decide(self, view, decision="approve", *, who="reviewer", key="decision-1", digest=None,
               reason=None):
        approval = view["approval"]
        return self.post(f"/v1/approvals/{approval['approval_id']}/decision", who=who, key=key,
                         json={"schema_version": 1, "approval_id": approval["approval_id"],
                               "action_digest": digest or approval["action_digest"],
                               "decision": decision, "reason": reason})


def build_lab(root, script, **options):
    lab = JourneyLab(
        root, script,
        approvers=ProjectApproverAllowlist({PROJECT: {REVIEWER.principal_id},
                                            OTHER_PROJECT: {OUTSIDER.principal_id}}),
        binding=StaticPrincipalBinding({(PROJECT, OPERATOR.principal_id): "synthetic-customer-1",
                                        (OTHER_PROJECT, OUTSIDER.principal_id):
                                            "synthetic-customer-1"}),
        world_binding=StaticWorldBinding({(PROJECT, OPERATOR.principal_id, J): ORGANIZER,
                                          (OTHER_PROJECT, OUTSIDER.principal_id, J): ORGANIZER}),
        **options)
    return lab


def build_app(lab, *, sessions=None, approvers=None, **options):
    return create_app(ApiConfig(
        services=lab.services(), sessions=sessions or TokenSessions(TOKENS),
        approvers=approvers or lab.approvals._approvers, graph_db=lab.paths.graph_db,
        app_db=lab.paths.app_db, redactor=Redactor([SECRET]), **options))


@pytest.fixture
def stack(tmp_path):
    """Yields ``start(*scripted_replies, **options) -> Api``; closes every server."""
    opened = []

    def start(*replies, lab_options=None, **options):
        lab = build_lab(tmp_path / f"lab-{len(opened)}", Script(*replies), **(lab_options or {}))
        client = TestClient(build_app(lab, **options), **LOOPBACK)
        client.__enter__()
        opened.append(client)
        return Api(client, lab)

    yield start
    for client in opened:
        client.__exit__(None, None, None)
