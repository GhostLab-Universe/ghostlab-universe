"""A real local stack with mocked inference, kept apart from M05's test harness.

Real: M04 SQLite stores, M01 adapter, M03 context, M05 controller, M06 policy, M02 worlds,
verifiers and capsules. Mocked: only the HTTP transport. Every provider reply here is a
labeled fixture, no network request is made and no model is called.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ghostlab.contracts import ApprovalSubmission, CampaignSpec, Principal
from ghostlab.contracts.world import WorldCampaignSpec
from ghostlab.orchestration import (DEFAULT_WORLDS, CampaignController, JourneyDriver, Services,
                                    TrustedSession)
from ghostlab.policy import (ApprovalService, ProjectApproverAllowlist, StaticPrincipalBinding,
                             StaticWorldBinding)
from ghostlab.providers import Credential, ProviderConfig, RetryPolicy, Runtime, adapter_for
from ghostlab.skills import SkillRegistry
from ghostlab.storage.approval_store import SqliteApprovalStore
from ghostlab.storage.artifacts import FileArtifactStore
from ghostlab.storage.budgets import SqliteBudgetLedger
from ghostlab.storage.checkpoints import graph_checkpointer
from ghostlab.storage.database import StoragePaths, open_database
from ghostlab.storage.memory_store import SqliteMemoryStore

FIXTURE_LABEL = "httpx.MockTransport fixture; no provider was contacted"
PROJECT = "project-eval"
OTHER_PROJECT = "project-other"
SECRET = "sk-eval-fixture-credential-0123456789"
# Secret-shaped text a fixture model echoes back. Nothing may store it.
SYNTHETIC_SECRET = "sk-model-echoed-token-abcdefghijklmnop"
SECRETS = (SECRET, SYNTHETIC_SECRET)
# Secret-shaped text a fixture model echoes back. Nothing may store it.
SYNTHETIC_SECRET = "sk-model-echoed-token-abcdefghijklmnop"
SECRETS = (SECRET, SYNTHETIC_SECRET)
MODEL = "fixture-model"
OPERATOR = Principal(principal_id="eval-operator", kind="human")
REVIEWER = Principal(principal_id="eval-reviewer", kind="human")
STRANGER = Principal(principal_id="eval-stranger", kind="human")
START = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
REFUND = "refund-world/1"
JOURNEY = "group-journey/1"
ORGANIZER = "traveller-a"
VIEWER = "participant-c"
NAIVE, CHECKED = "journey-naive", "journey-version-checked"
# The version-checked adapter's last permission check before it delivers. Derived from a
# fault-free run in ``ghostlab.worlds.journey_run.race_scenario``; pinned by expected.json.
RACE_AFTER_EVENT = 17


class SimulatedCrash(BaseException):
    """Stands in for the process dying. Not an Exception, so nothing handles it."""


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class Timer:
    """Retry back-off without real sleeping."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Transport:
    """Scripted provider fixtures. Logs each attempt; bodies stay in memory only."""

    def __init__(self, *replies):
        self.replies, self.attempts, self.bodies = list(replies), [], []

    def add(self, *replies):
        self.replies.extend(replies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content or b"{}"))
        if not self.replies:
            self.attempts.append({"http_status": None, "outcome": "unscripted_request"})
            raise AssertionError("Unexpected extra provider request")
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            self.attempts.append({"http_status": None, "outcome": "transport_raised"})
            raise item
        status, body = item
        self.attempts.append({"http_status": status,
                              "outcome": "ok" if status < 400 else "error"})
        return httpx.Response(status, content=body if isinstance(body, bytes)
                              else json.dumps(body).encode())


def meta_reply(text: str, *, usage: bool = True):
    body = {"id": "resp_fixture", "object": "response", "status": "completed", "model": MODEL,
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}]}
    if usage:
        body["usage"] = {"input_tokens": 700, "output_tokens": 120}
    return (200, body)


BUSY = (503, {"error": {"code": "server_error", "message": "busy"}})
RATE_LIMITED = (429, {"error": {"code": "rate_limit", "message": "slow down"}})
UNAUTHORIZED = (401, {"error": {"code": "invalid_api_key", "message": "bad key"}})


def refund_proposal(**changes) -> dict:
    return {"schema_version": 1,
            "hypothesis": "A retry after a post-commit timeout issues a second refund",
            "adapter": "vulnerable",
            "scenario": {"timeout_after_commit": True, "max_tool_calls": 20},
            "expected_signal": "total_refund_within_payment fails", **changes}


def revocation(after_event=None, recipient=VIEWER) -> dict:
    return {"fault_id": "sharing_revoked_while_queued", "after_event": after_event,
            "parameters": [{"name": "recipient_id", "value": recipient}]}


def delayed_update(events=3) -> dict:
    return {"fault_id": "delayed_itinerary_update", "after_event": None,
            "parameters": [{"name": "delay_events", "value": events}]}


def journey_proposal(adapter=NAIVE, *, faults=None, cap=20, **changes) -> dict:
    return {"schema_version": 1, "world_version": JOURNEY,
            "hypothesis": "A message queued before revocation is still delivered afterwards",
            "adapter_id": adapter,
            "scenario": {"schema_version": 1, "world_version": JOURNEY, "max_tool_calls": cap,
                         "faults": [revocation()] if faults is None else faults},
            "expected_signal": "delivered_messages_permitted fails", **changes}


def reply(proposal: dict, **options):
    return meta_reply(json.dumps(proposal), **options)


def _budget(**changes) -> dict:
    return {"max_model_calls": 10, "max_tool_calls": 100, "max_input_tokens": 100_000,
            "max_output_tokens": 20_000, "max_cost_minor": None, "cost_currency": None,
            "max_wall_clock_ms": 20 * 60_000, "max_parallel_runs": 2, **changes}


def spec_for(world: str, **budget):
    model = CampaignSpec if world == REFUND else WorldCampaignSpec
    return model.model_validate({
        "schema_version": 1, "objective": f"Find a recorded failure in {world}",
        "success_criteria": ["A failing invariant is reproduced and a repair is verified"],
        "world_version": world, "budget": _budget(**budget)})


class CappedRepair(JourneyDriver):
    """Stands in for a fixture whose repaired adapter needs more than the target limit."""

    def __init__(self, cap: int):
        self.repair_cap = cap

    def repair_action(self, experiment_id, proposal):
        action = super().repair_action(experiment_id, proposal)
        return {**action, "scenario": {**action["scenario"], "max_tool_calls": self.repair_cap}}


class BrokenLedger:
    """A ledger that can't answer, as with a disk error. It's not an exhausted budget."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def reserve(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")


class Stack:
    """One private local storage root plus everything a controller needs."""

    def __init__(self, root: Path, transport: Transport, *, retry_attempts=3, binding=None,
                 world_binding=None, on_node=None, worlds=None):
        self.root, self.transport = Path(root), transport
        self.paths = StoragePaths(self.root)
        app = open_database(self.paths.app_db)
        self.clock, self.timer = Clock(), Timer()
        self.memory_store = SqliteMemoryStore(app)
        self.approval_store = SqliteApprovalStore(app)
        self.ledger = SqliteBudgetLedger(app)
        self.artifacts = FileArtifactStore(app, self.paths.artifacts)
        self.approvals = ApprovalService(
            self.approval_store,
            approvers=ProjectApproverAllowlist({PROJECT: {REVIEWER.principal_id}}))
        self.binding = binding or StaticPrincipalBinding(
            {(PROJECT, OPERATOR.principal_id): "synthetic-customer-1"})
        self.world_binding = world_binding or StaticWorldBinding(
            {(PROJECT, OPERATOR.principal_id, JOURNEY): ORGANIZER})
        self.on_node = on_node or (lambda name, state: None)
        self.worlds = {**DEFAULT_WORLDS, **(worlds or {})}
        self.runtime = Runtime(transport=httpx.MockTransport(transport), sleep=self.timer.sleep,
                               monotonic=self.timer.monotonic, jitter=lambda: 0.0)
        self.config = ProviderConfig(provider="meta_responses", model=MODEL,
                                     base_url="https://api.meta.ai/v1",
                                     credential=Credential(SECRET, "test-fixture"),
                                     retry=RetryPolicy(max_attempts=retry_attempts))
        self.wall_ms = 0.0

    def services(self) -> Services:
        return Services(memory_store=self.memory_store, approval_store=self.approval_store,
                        approvals=self.approvals, ledger=self.ledger, artifacts=self.artifacts,
                        skills=SkillRegistry.load(), binding=self.binding,
                        adapter_factory=lambda runtime: adapter_for(self.config, runtime),
                        runtime=self.runtime, clock=self.clock, on_node=self.on_node,
                        world_binding=self.world_binding, worlds=self.worlds)

    @contextmanager
    def controller(self, session=None, **options):
        with graph_checkpointer(self.paths.graph_db, app_db=self.paths.app_db) as saver:
            yield CampaignController(session or TrustedSession(OPERATOR, PROJECT),
                                     self.services(), saver=saver, **options)

    def timed(self, call):
        """Local wall time of a call. With mocked inference this isn't provider latency."""
        began = time.perf_counter()
        try:
            return call()
        finally:
            self.wall_ms += (time.perf_counter() - began) * 1000

    def decide(self, state, *, principal=REVIEWER, decision="approve", digest=None):
        approval = state["approval"]
        return self.approvals.decide(
            session_principal=principal, project_id=PROJECT,
            submission=ApprovalSubmission(
                schema_version=1, approval_id=approval["approval_id"],
                action_digest=digest or approval["action_digest"], decision=decision,
                reason=None),
            decision_id=f"d-{decision}", now=self.clock())


def without_world_binding(stack: Stack) -> None:
    stack.world_binding = StaticWorldBinding({})
    stack.binding = StaticPrincipalBinding({})
