"""M05 units: engine fidelity, budget atomicity, task DAG and the live-inference gate."""

import json
import threading
from pathlib import Path

import pytest

from ghostlab.contracts import InvariantFinding, Principal
from ghostlab.core import Fixture, Scenario, run
from ghostlab.orchestration import (LiveInferenceAuthorization, LiveInferenceRefused,
                                    derive_findings, execute_run, ledger_change_events)
from ghostlab.orchestration import tasks as board
from ghostlab.budgets import BudgetAccountant, BudgetExhausted
from ghostlab.policy import StaticPrincipalBinding, ceiling_for, delegate, root_scope
from ghostlab.storage.budgets import SqliteBudgetLedger
from ghostlab.storage.database import open_database

from m05_harness import MODEL, OPERATOR, PROJECT, Clock, Lab, Script, spec

BINDING = StaticPrincipalBinding({(PROJECT, OPERATOR.principal_id): "synthetic-customer-1"})
TARGET = delegate(root_scope(PROJECT, OPERATOR), "target",
                  {"get_order", "issue_refund", "get_refund_status"})


# --- engine ------------------------------------------------------------------------

@pytest.mark.parametrize("adapter", ["vulnerable", "repaired"])
@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize("cap", [20, 1])
def test_policy_enforced_run_equals_core_run_when_allowed(adapter, fault, cap):
    scenario = Scenario(timeout_after_commit=fault, max_tool_calls=cap)
    outcome = execute_run(Fixture(), scenario, adapter, scope=TARGET, project_id=PROJECT,
                          binding=BINDING)
    assert outcome.denial is None
    assert outcome.run == run(Fixture(), scenario, adapter)


@pytest.mark.parametrize("scope,binding,code", [
    (TARGET, StaticPrincipalBinding({(PROJECT, OPERATOR.principal_id): "someone-else"}),
     "identity_mismatch"),
    (delegate(root_scope(PROJECT, OPERATOR), "target", {"get_order"}), BINDING,
     "tool_not_in_scope"),
    (delegate(root_scope(PROJECT, OPERATOR), "analyst", {"get_order"}), BINDING,
     "tool_not_in_scope"),
])
def test_denied_calls_leave_no_trace_or_ledger_change(scope, binding, code):
    outcome = execute_run(Fixture(), Scenario(timeout_after_commit=True), "vulnerable",
                          scope=scope, project_id=PROJECT, binding=binding)
    assert outcome.run is None and outcome.denial.code == code
    assert outcome.tool_calls <= 1  # at most get_order ran before the denied call


def test_findings_cite_ledger_evidence_and_validate():
    faulted = run(Fixture(), Scenario(timeout_after_commit=True), "vulnerable")
    assert ledger_change_events(faulted) == [2, 3]
    findings = derive_findings("exp-1-fork", faulted, recorded_at="2026-10-29T12:00:00Z")
    by_name = {f["invariant"]: f for f in findings}
    total = by_name["total_refund_within_payment"]
    assert total["verdict"] == "failed" and total["evidence_event_ids"] == [2, 3]
    assert (total["observed_minor"], total["limit_minor"]) == (500_000, 250_000)
    for finding in findings:
        InvariantFinding.model_validate(finding)


# --- budgets ------------------------------------------------------------------------

def accountant(tmp_path, **limits):
    ledger = SqliteBudgetLedger(open_database(tmp_path / "ghostlab.sqlite"))
    return BudgetAccountant(ledger, "campaign-1", spec(**limits).budget, clock=Clock())


def test_concurrent_reservations_never_overspend(tmp_path):
    books = accountant(tmp_path, max_tool_calls=7)
    barrier, wins, losses = threading.Barrier(24), [], []

    def worker(i):
        barrier.wait()
        try:
            wins.append(books.reserve(f"task-{i}", "tool_calls", 1))
        except BudgetExhausted:
            losses.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(wins) == 7 and len(losses) == 17
    assert books.ledger.committed("campaign-1", "tool_calls")[0] == 7


def test_attempt_gate_never_raises_and_releases_partial_reservations(tmp_path):
    books = accountant(tmp_path, max_model_calls=1)
    gate = books.model_attempt_gate("task-1", max_output_tokens=500, input_tokens=100,
                                    task_limits=ceiling_for("explorer"))
    assert gate() is True and gate() is False  # second attempt over the campaign cap
    assert gate.exhausted.resource == "model_calls"
    assert books.ledger.committed("campaign-1", "output_tokens")[0] == 500  # one attempt only

    class Broken:
        def reserve(self, *a, **k):
            raise RuntimeError("database unavailable")

    broken = BudgetAccountant(Broken(), "c", spec().budget, clock=Clock())
    gate = broken.model_attempt_gate("t", max_output_tokens=1, input_tokens=1,
                                     task_limits=ceiling_for("explorer"))
    assert gate() is False and gate.exhausted.scope == "ledger"


def test_task_ceiling_applies_per_task(tmp_path):
    books = accountant(tmp_path)
    explorer = ceiling_for("explorer")
    gate = books.model_attempt_gate("task-x", max_output_tokens=1_000, input_tokens=10,
                                    task_limits=explorer)
    sent = sum(gate() for _ in range(explorer.max_model_calls + 2))
    assert sent == explorer.max_model_calls and gate.exhausted.scope == "task"


# --- task DAG -----------------------------------------------------------------------

def test_dependent_tasks_wait_and_illegal_transitions_fail():
    tasks = [board.new_task("c", "a", "propose_experiment", "explorer", exit_condition="x"),
             board.new_task("c", "b", "execute_run", "target", depends_on=["a"],
                            exit_condition="y")]
    with pytest.raises(board.TaskTransitionError, match="waits on"):
        board.start(tasks, "b")
    tasks = board.start(tasks, "a")
    with pytest.raises(board.TaskTransitionError, match="illegal"):
        board.finish(tasks, "a", "pending")
    tasks = board.finish(tasks, "a", "failed")
    tasks = board.fail_dependents(tasks)
    assert [t["status"] for t in tasks] == ["failed", "skipped"]
    with pytest.raises(board.TaskTransitionError):
        board.start(tasks, "a")  # terminal


def test_attempts_are_bounded():
    tasks = [board.new_task("c", "a", "propose_experiment", "explorer", max_attempts=2,
                            exit_condition="x")]
    tasks = board.retry(board.start(tasks, "a"), "a")
    tasks = board.start(tasks, "a")
    with pytest.raises(board.TaskTransitionError, match="no attempts left"):
        board.retry(tasks, "a")


# --- live-inference gate ------------------------------------------------------------

def report(path: Path, **changes) -> Path:
    data = {"live": True, "transport": "network", "model": MODEL,
            "checks": [{"check": "auth_minimal", "status": "passed"},
                       {"check": "structured_proposal", "status": "passed"}], **changes}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_network_runtime_is_refused_without_authorization(tmp_path):
    lab = Lab(tmp_path, Script(), transport=False)
    with lab.controller() as controller, pytest.raises(LiveInferenceRefused):
        controller.start("campaign-1", spec())


@pytest.mark.parametrize("changes,auth", [
    ({"live": False, "transport": "mock"}, {}),
    ({"model": "another-model"}, {}),
    ({"checks": [{"check": "auth_minimal", "status": "skipped_call_cap"}]}, {}),
    ({}, {"confirm_paid_calls": False}),
    ({}, {"max_model_calls": 0}),
    ({}, {"model": "another-model"}),
], ids=["mock-report", "report-model", "skipped-check", "unconfirmed", "no-cap", "auth-model"])
def test_live_authorization_requires_every_gate(tmp_path, changes, auth):
    path = report(tmp_path / "compatibility-report.json", **changes)
    live = LiveInferenceAuthorization(**{"model": MODEL, "max_model_calls": 3,
                                         "compatibility_report": path,
                                         "confirm_paid_calls": True, **auth})
    lab = Lab(tmp_path / "lab", Script(), transport=False, live=live)
    with lab.controller() as controller, pytest.raises(LiveInferenceRefused):
        controller.start("campaign-1", spec())


def test_valid_live_authorization_caps_model_calls_without_sending(tmp_path):
    from ghostlab.orchestration import require_inference_allowed
    from ghostlab.providers import Runtime
    live = LiveInferenceAuthorization(model=MODEL, max_model_calls=3, confirm_paid_calls=True,
                                      compatibility_report=report(tmp_path / "r.json"))
    limits = require_inference_allowed(Runtime(transport=None), MODEL, spec().budget, live,
                                       credential_source="env:MODEL_API_KEY")
    assert limits.max_model_calls == 3  # the explicit cap replaces the campaign value
    with pytest.raises(LiveInferenceRefused, match="environment"):
        require_inference_allowed(Runtime(transport=None), MODEL, spec().budget, live,
                                  credential_source="test-fixture")



def test_real_http_transport_is_not_treated_as_a_mock(tmp_path):
    import httpx
    transport = httpx.HTTPTransport()
    try:
        script = Script()
        lab = Lab(tmp_path, script, real_transport=transport)
        with lab.controller() as controller, pytest.raises(LiveInferenceRefused):
            controller.start("campaign-1", spec())
        assert script.requests == []
    finally:
        transport.close()
