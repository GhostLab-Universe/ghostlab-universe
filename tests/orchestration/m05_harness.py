"""M05 test harness: REAL M04 SQLite stores, REAL M01 adapter, MOCKED HTTP transport.

Every provider response here is a labeled test fixture (httpx.MockTransport);
no network request is made and no model is called.
"""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ghostlab.contracts import ApprovalSubmission, CampaignSpec, Principal
from ghostlab.orchestration import (CampaignController, Services, TrustedSession,
                                    execute_run)
from ghostlab.policy import (ApprovalService, ProjectApproverAllowlist, StaticPrincipalBinding)
from ghostlab.providers import Credential, ProviderConfig, RetryPolicy, Runtime, adapter_for
from ghostlab.skills import SkillRegistry
from ghostlab.storage.approval_store import SqliteApprovalStore
from ghostlab.storage.artifacts import FileArtifactStore
from ghostlab.storage.budgets import SqliteBudgetLedger
from ghostlab.storage.checkpoints import graph_checkpointer
from ghostlab.storage.database import StoragePaths, open_database
from ghostlab.storage.memory_store import SqliteMemoryStore

PROJECT = "project-local"
SECRET = "sk-fixture-orchestration-0123456789"
MODEL = "fixture-model"
OPERATOR = Principal(principal_id="operator-1", kind="human")
REVIEWER = Principal(principal_id="reviewer-1", kind="human")
STRANGER = Principal(principal_id="stranger-1", kind="human")
START = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

VULNERABLE = {"schema_version": 1,
              "hypothesis": "A retry after a post-commit timeout issues a second refund",
              "adapter": "vulnerable",
              "scenario": {"timeout_after_commit": True, "max_tool_calls": 20},
              "expected_signal": "total_refund_within_payment fails"}


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class FakeSleep:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Script:
    """Scripted provider replies (fixtures). Records every outbound HTTP request."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def add(self, *responses):
        self.responses.extend(responses)

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra provider request")
        status, body = self.responses.pop(0)
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=content)


def meta_reply(text, *, usage=True, response_id="resp_fixture"):
    body = {"id": response_id, "object": "response", "status": "completed", "model": MODEL,
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text,
                                     "annotations": []}]}]}
    if usage:
        body["usage"] = {"input_tokens": 700, "output_tokens": 120}
    return (200, body)


def proposal(**changes):
    data = json.loads(json.dumps(VULNERABLE))
    for key, value in changes.items():
        if key in ("timeout_after_commit", "max_tool_calls"):
            data["scenario"][key] = value
        else:
            data[key] = value
    return meta_reply(json.dumps(data))


BUSY = (503, {"error": {"code": "server_error", "message": "busy"}})


class EngineSpy:
    def __init__(self, engine=execute_run):
        self.engine, self.calls = engine, []

    def __call__(self, fixture, scenario, adapter, **kwargs):
        self.calls.append((scenario.timeout_after_commit, adapter))
        return self.engine(fixture, scenario, adapter, **kwargs)


def spec(**budget):
    limits = {"max_model_calls": 10, "max_tool_calls": 100, "max_input_tokens": 100_000,
              "max_output_tokens": 20_000, "max_cost_minor": None, "cost_currency": None,
              "max_wall_clock_ms": 20 * 60_000, "max_parallel_runs": 2, **budget}
    return CampaignSpec.model_validate({
        "schema_version": 1,
        "objective": "Find duplicate-refund failures after a payment timeout",
        "success_criteria": ["A failing invariant is reproduced and a repair is verified"],
        "world_version": "refund-world/1", "budget": limits})


class Lab:
    """One developer's local storage plus everything a controller needs."""

    def __init__(self, root: Path, script: Script, *, retry_attempts=3, binding=None,
                 approvers=None, transport=True, live=None, on_node=None):
        self.paths = StoragePaths(root)
        app = open_database(self.paths.app_db)
        self.script, self.clock, self.timer = script, Clock(), FakeSleep()
        self.memory_store = SqliteMemoryStore(app)
        self.approval_store = SqliteApprovalStore(app)
        self.ledger = SqliteBudgetLedger(app)
        self.artifacts = FileArtifactStore(app, self.paths.artifacts)
        self.approvals = ApprovalService(
            self.approval_store,
            approvers=approvers or ProjectApproverAllowlist({PROJECT: {REVIEWER.principal_id}}))
        self.binding = binding or StaticPrincipalBinding(
            {(PROJECT, OPERATOR.principal_id): "synthetic-customer-1"})
        self.engine = EngineSpy()
        self.runtime = Runtime(transport=httpx.MockTransport(script) if transport else None,
                               sleep=self.timer.sleep, monotonic=self.timer.monotonic,
                               jitter=lambda: 0.0)
        self.config = ProviderConfig(provider="meta_responses", model=MODEL,
                                     base_url="https://api.meta.ai/v1",
                                     credential=Credential(SECRET, "test-fixture"),
                                     retry=RetryPolicy(max_attempts=retry_attempts))
        self.live, self.on_node = live, on_node

    def services(self):
        return Services(memory_store=self.memory_store, approval_store=self.approval_store,
                        approvals=self.approvals, ledger=self.ledger, artifacts=self.artifacts,
                        skills=SkillRegistry.load(), binding=self.binding,
                        adapter_factory=lambda runtime: adapter_for(self.config, runtime),
                        runtime=self.runtime, clock=self.clock, live=self.live,
                        engine=self.engine,
                        on_node=self.on_node or (lambda name, state: None))

    @contextmanager
    def controller(self, session=None, **options):
        with graph_checkpointer(self.paths.graph_db, app_db=self.paths.app_db) as saver:
            yield CampaignController(session or TrustedSession(OPERATOR, PROJECT),
                                     self.services(), saver=saver, **options)

    def approve(self, state, *, principal=REVIEWER, decision="approve", digest=None):
        approval = state["approval"]
        submission = ApprovalSubmission(schema_version=1, approval_id=approval["approval_id"],
                                        action_digest=digest or approval["action_digest"],
                                        decision=decision, reason=None)
        return self.approvals.decide(session_principal=principal, project_id=PROJECT,
                                     submission=submission, decision_id=f"d-{decision}",
                                     now=self.clock())
