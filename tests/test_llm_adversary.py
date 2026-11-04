"""Muse adversary bridge and explore loop, with a scripted fake model (no network)."""

import tempfile
import unittest

from ghostlab.llm import adversary as ADV
from ghostlab.llm import capsules
from ghostlab.llm.client import Turn


class Script:
    """steps: list of call lists [(name, args)], None = stop, "fail" = provider error."""

    def __init__(self, steps):
        self.steps, self.calls, self.session_usd, self.sent = list(steps), [], 0.0, []

    def open(self, system, tools):
        model = self

        class _Conv:
            def send(self, user=None, tool_results=None):
                model.sent.append(tool_results)
                step = model.steps.pop(0) if model.steps else None
                model.calls.append({"input_tokens": 10, "output_tokens": 5})
                if step == "fail":
                    return Turn("failed", error="provider_transient: HTTP 503")
                if step is None:
                    return Turn("completed", text="summary")
                return Turn("tool_calls", tool_calls=[
                    {"id": f"c{len(model.calls)}-{i}", "name": n, "arguments": a, "raw": a}
                    for i, (n, a) in enumerate(step)])
        return _Conv()


def sched(tool="issue_refund", occ=1, hook="timeout_after_commit"):
    return [{"hook": hook, "trigger_tool": tool, "occurrence": occ}]


CATALOG = [("list_fault_catalog", {})]


class Env:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.store = capsules.fresh_env(self.tmp.name)
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()

    def explore(self, steps, **kw):
        model = Script(steps)
        explorer = ADV.ModelExplorer(model, store=self.store, registry=self.registry,
                                     project_id="adv", **kw)
        return ADV.explore(ADV.new_state(explorer)), model


class AdversaryTests(unittest.TestCase):
    def test_finds_and_minimizes_failure(self):
        with Env() as env:
            state, model = env.explore([
                CATALOG,
                [("run_experiment", {"fault_schedule": sched(), "agent_variant": "repaired"})],
                [("run_experiment", {"fault_schedule": sched(), "agent_variant": "vulnerable"})],
            ])
        self.assertEqual(state["stop_status"], "found_failure")
        failure = state["failure"]
        self.assertIn("no_duplicate_full_refund", failure["rules_failed"])
        self.assertTrue(failure["minimized"]["stable"])
        self.assertEqual(failure["minimized"]["required_prefix"],
                         ["get_order", "issue_refund", "issue_refund"])
        self.assertEqual(failure["reality_diff"]["faulted_refunded_minor"], 500000)
        self.assertEqual(state["experiments"][0]["verdict"], "pass")
        # the repaired run's finding went back to the model as a tool result
        self.assertIn("pass", model.sent[2][0]["output"])
        out = ADV.summary(state)
        self.assertEqual(out["budget"]["experiments"], 2)

    def test_invalid_proposals_rejected_never_executed(self):
        with Env() as env:
            state, model = env.explore([
                [("run_experiment", {"fault_schedule": sched(hook="drop_database"),
                                     "agent_variant": "vulnerable"}),
                 ("run_experiment", {"fault_schedule": sched(occ=99),
                                     "agent_variant": "vulnerable"}),
                 ("run_experiment", {"fault_schedule": sched(), "agent_variant": "root"}),
                 ("fork", {"run_id": "run-nope", "fault_override": sched()})],
                None])
            self.assertEqual(env.store.list_runs("adv"), [])
        self.assertEqual(len(state["explorer"].rejected), 4)
        self.assertEqual(state["experiments"], [])
        self.assertEqual(state["stop_status"], "model_stopped")

    def test_budget_exhausted(self):
        with Env() as env:
            state, _ = env.explore([
                [("run_experiment", {"fault_schedule": sched(), "agent_variant": "repaired"}),
                 ("run_experiment", {"fault_schedule": sched("get_order"),
                                     "agent_variant": "repaired"})]],
                max_experiments=1)
        self.assertEqual(state["stop_status"], "budget_exhausted")
        self.assertEqual(len(state["experiments"]), 1)

    def test_paused_on_error_then_resume(self):
        with Env() as env:
            state, model = env.explore([
                [("run_experiment", {"fault_schedule": sched(), "agent_variant": "repaired"})],
                "fail",
                [("run_experiment", {"fault_schedule": sched(), "agent_variant": "vulnerable"})],
            ])
            self.assertEqual(state["stop_status"], "paused_on_error")
            ADV.explore(state)
        self.assertEqual(state["stop_status"], "found_failure")
        # the result the failed turn carried was resent, not lost
        self.assertEqual(model.sent[1], model.sent[2])

    def test_fork_reuses_variant(self):
        with Env() as env:
            state, _ = env.explore([
                [("run_experiment", {"fault_schedule": sched("get_order"),
                                     "agent_variant": "vulnerable"})],
                [("fork", {"run_id": "run-000001", "fault_override": sched()})],
            ])
        self.assertEqual(state["experiments"][1]["body"]["agent"], "vulnerable")
        self.assertEqual(state["stop_status"], "found_failure")


if __name__ == "__main__":
    unittest.main()
