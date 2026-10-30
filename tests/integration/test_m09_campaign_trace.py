"""A complete campaign can be traced, and the trace holds no secret."""

import json

from evals.cases import approve_and_resume, plant_memory, start
from evals.evidence import scan_stores
from evals.harness import (CHECKED, JOURNEY, REFUND, SECRET, SYNTHETIC_SECRET, Stack, Transport,
                           journey_proposal, refund_proposal, reply, revocation)
from ghostlab.observability import (FanOutSink, FileSink, InMemorySink, Telemetry, Usage, Versions,
                                    emit_campaign_trace, find_secrets, read_records, to_otlp,
                                    validate_record)
from ghostlab.orchestration import ORCHESTRATION_VERSION
from ghostlab.security.redaction import Redactor

CID = "campaign-eval"
ECHOED = SYNTHETIC_SECRET


def traced(stack, state, tmp_path, *, usage=Usage(input_tokens=700, output_tokens=120)):
    memory, path = InMemorySink(), tmp_path / "logs" / "trace.jsonl"
    telemetry = Telemetry(FanOutSink(memory, FileSink(path)), clock=stack.clock, strict=True,
                          redactor=Redactor([SECRET]))
    root = emit_campaign_trace(
        telemetry, state, usage=usage, attempts=stack.transport.attempts,
        approval_status=(stack.approval_store.get("project-eval", state["approval"]["approval_id"])
                         .status if state.get("approval") else None),
        versions=Versions(role="supervisor", skill_id="propose-refund-scenario",
                          skill_version="1", workflow_version=ORCHESTRATION_VERSION))
    return root, memory, path


def by_name(sink, name):
    return [record for record in sink.records if record.name == name]


def test_a_complete_refund_campaign_is_traced_without_secret_leakage(tmp_path):
    proposal = refund_proposal(hypothesis=f"Retry doubles refunds, Bearer {ECHOED} api_key={ECHOED}")
    stack = Stack(tmp_path / "store", Transport(reply(proposal)))
    plant_memory(stack, REFUND)
    paused = start(stack, REFUND)
    done = approve_and_resume(stack, paused)
    assert done["outcome"] == "repair_verified"
    # The fixture really held the secret-shaped text. M05 scrubs it before storing anything.
    assert ECHOED in json.dumps(proposal)
    assert ECHOED not in json.dumps(paused) and ECHOED not in json.dumps(done)
    assert scan_stores(stack.root)["files_with_secret"] == []

    root, memory, path = traced(stack, done, tmp_path)
    stored, problems = read_records(path)
    otlp = json.dumps(to_otlp(memory.records))
    dumps = {"memory": memory.dump(), "file": path.read_text("utf-8"), "otlp": otlp}
    assert problems == []
    for name, dump in dumps.items():
        assert find_secrets(dump, [SECRET, ECHOED]) == [], name
        assert "SYSTEM OVERRIDE" not in dump and "bearer" not in dump.lower()
        assert "Authorization" not in dump

    # the trace is complete and hangs together
    assert all(validate_record(record) == [] for record in stored)
    (span,) = by_name(memory, "campaign.run")
    assert span.span_id == root and span.parent_span_id is None
    assert {record.trace_id for record in memory.records} == {done["trace_id"]}
    assert {r.parent_span_id for r in memory.records if r.span_id != root} == {root}
    assert (span.outcome, span.approval_status) == ("repair_verified", "consumed")
    assert (span.provider, span.model) == ("meta_responses", "fixture-model")
    assert span.workflow_version == ORCHESTRATION_VERSION and span.world_version == REFUND
    assert span.usage == Usage(input_tokens=700, output_tokens=120)
    assert span.cancelled is False and span.validation_failures == 0
    assert by_name(memory, "approval.requested")[0].approval_id == done["approval"]["approval_id"]
    assert len(by_name(memory, "run.completed")) == 1
    assert len(by_name(memory, "finding.recorded")) == 1
    assert [r.attributes["http_status"] for r in by_name(memory, "model.attempt")] == [200]
    tasks = {r.task_id: r for r in by_name(memory, "task.status") if r.outcome == "succeeded"}
    assert {record.role for record in tasks.values()} >= {"explorer", "target", "verifier",
                                                          "supervisor"}


def test_a_complete_journey_campaign_is_traced_without_secret_leakage(tmp_path):
    proposal = journey_proposal(CHECKED, faults=[revocation(17)],
                                hypothesis=f"Race, password={ECHOED}")
    stack = Stack(tmp_path / "store", Transport(reply(proposal)))
    done = approve_and_resume(stack, start(stack, JOURNEY))
    assert done["outcome"] == "repair_verified" and ECHOED in json.dumps(proposal)
    assert ECHOED not in json.dumps(done)
    assert scan_stores(stack.root)["files_with_secret"] == []
    _, memory, path = traced(stack, done, tmp_path)
    assert find_secrets(memory.dump() + path.read_text("utf-8"), [SECRET, ECHOED]) == []
    (span,) = by_name(memory, "campaign.run")
    assert span.world_version == JOURNEY and span.outcome == "repair_verified"


def test_telemetry_doesnt_rely_on_the_controller_having_redacted(tmp_path):
    """Second layer: even state holding raw text gives a clean trace."""
    stack = Stack(tmp_path / "store", Transport(reply(refund_proposal())))
    state = start(stack, REFUND)
    raw = f"Bearer {ECHOED} api_key={ECHOED}"
    dirty = {**state,
             "events": [{**event, "summary": raw} for event in state["events"]],
             "errors": [{"code": "internal", "message": raw, "retryable": False,
                         "side_effect": "none", "ref": None}],
             "proposal": {**state["proposal"], "hypothesis": raw}}
    assert ECHOED in json.dumps(dirty)
    _, memory, path = traced(stack, dirty, tmp_path)
    assert find_secrets(memory.dump() + path.read_text("utf-8"), [SECRET, ECHOED]) == []


def test_unknown_usage_stays_unknown_in_the_trace(tmp_path):
    stack = Stack(tmp_path / "store", Transport(reply(refund_proposal(), usage=False)))
    state = start(stack, REFUND)
    _, memory, _ = traced(stack, state, tmp_path, usage=None)
    (span,) = by_name(memory, "campaign.run")
    assert span.usage == Usage() and span.outcome == "awaiting_approval"
    assert "gen_ai.usage" not in json.dumps(to_otlp([span]))


def test_a_rebuilt_trace_has_stable_ids(tmp_path):
    stack = Stack(tmp_path / "store", Transport(reply(refund_proposal())))
    state = start(stack, REFUND)
    assert traced(stack, state, tmp_path / "a")[0] == traced(stack, state, tmp_path / "b")[0]
