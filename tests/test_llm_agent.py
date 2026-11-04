"""LLM refund agent through the controller, with scripted fake models (no network)."""

import json
import tempfile
import unittest

from ghostlab.experiments.configs import LLM_AGENTS
from ghostlab.llm import agent as A
from ghostlab.llm import capsules
from ghostlab.llm.client import RecordingClient, ReplayClient, Turn

ORDER = A.FIXTURE["order_id"]


class ScriptedModel:
    """Each step is a list of (name, args) calls, or None to stop with text."""

    def __init__(self, steps, provider="fake", model="fake-model"):
        self.steps, self.provider, self.model = steps, provider, model
        self.seen = []
        self.calls = []

    def open(self, system, tools):
        model = self
        state = {"i": 0}

        class _Conv:
            def send(self, user=None, tool_results=None):
                model.seen.append({"tools": [t["name"] for t in tools],
                                   "tool_results": tool_results})
                i = state["i"]
                state["i"] += 1
                if i >= len(model.steps) or model.steps[i] is None:
                    return Turn("completed", text="done", provider=model.provider,
                                model=model.model, usage={})
                return Turn("tool_calls", provider=model.provider, model=model.model,
                            usage={}, request_id=f"r{i}",
                            tool_calls=[{"id": f"c{i}-{j}", "name": n, "arguments": a,
                                         "raw": json.dumps(a)}
                                        for j, (n, a) in enumerate(model.steps[i])])
        return _Conv()


NAIVE = [[("get_order", {"order_id": ORDER})],
         [("issue_refund", {"order_id": ORDER, "amount": 2500})],
         [("issue_refund", {"order_id": ORDER, "amount": 2500})],
         [("report_outcome", {"refunds_issued": 1, "total_refunded": 2500})]]
HARDENED = [[("get_order", {"order_id": ORDER})],
            [("issue_refund", {"order_id": ORDER, "amount": 2500,
                               "idempotency_key": f"refund-{ORDER}"})],
            [("get_refund_status", {"order_id": ORDER, "idempotency_key": f"refund-{ORDER}"})],
            [("report_outcome", {"refunds_issued": 1, "total_refunded": 2500})]]


class Env:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry, self.store = capsules.fresh_env(self.tmp.name)
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()

    def run(self, model, variant, faults=(A.TIMEOUT_FAULT,)):
        return A.run_llm(self.store, self.registry, "proj", model, variant, faults)


class AgentTests(unittest.TestCase):
    def test_variant_names_match_config(self):
        self.assertEqual(tuple(LLM_AGENTS), A.VARIANTS)

    def test_naive_duplicates_and_claims_success(self):
        with Env() as env:
            s = env.run(ScriptedModel(NAIVE), "llm-naive")["summary"]
        self.assertEqual(s["verdict"], "fail")
        self.assertTrue(s["duplicate_refund"])
        self.assertEqual(s["claim_check"]["verdict"], "false_success")
        self.assertEqual(s["claim_check"]["actual"]["refunds_issued"], 2)
        self.assertEqual(s["model_calls"], 4)
        self.assertTrue(s["faults"][0]["fired"])

    def test_model_sees_realistic_timeout_not_commit(self):
        model = ScriptedModel(NAIVE)
        with Env() as env:
            env.run(model, "llm-naive")
        timeout_output = json.loads(model.seen[2]["tool_results"][0]["output"])
        self.assertEqual(timeout_output, A.TIMEOUT_TO_MODEL)
        self.assertNotIn("committed", json.dumps(timeout_output).lower())

    def test_hardened_passes_and_report_matches(self):
        with Env() as env:
            s = env.run(ScriptedModel(HARDENED), "llm-hardened")["summary"]
        self.assertEqual(s["verdict"], "pass")
        self.assertFalse(s["duplicate_refund"])
        self.assertEqual(s["claim_check"]["verdict"], "match")

    def test_noread_hides_and_gateway_denies_status_tool(self):
        model = ScriptedModel(HARDENED)
        with Env() as env:
            s = env.run(model, "llm-hardened-noread")["summary"]
        self.assertNotIn("get_refund_status", model.seen[0]["tools"])
        denied = json.loads(model.seen[3]["tool_results"][0]["output"])
        self.assertEqual(denied["error"], "GatewayNotAllowed")
        self.assertEqual([a["tool"] for a in s["gateway_actions"]],
                         ["get_order", "issue_refund", "report_outcome"])

    def test_customer_is_bound_by_facade(self):
        with Env() as env:
            s = env.run(ScriptedModel(HARDENED), "llm-hardened")
        trace = s["record"]["run"]["evidence"]["trace"]
        self.assertTrue(all(e["request"]["arguments"]["customer_id"]
                            == A.FIXTURE["customer_id"] for e in trace))

    def test_no_report_and_unbound_client(self):
        with Env() as env:
            s = env.run(ScriptedModel(NAIVE[:1]), "llm-naive")["summary"]
            self.assertEqual(s["claim_check"]["verdict"], "no_report")
            body = A.requested("llm-naive", [A.TIMEOUT_FAULT], "unbound")
            from ghostlab.experiments import ops
            created = ops.create_requested_config(env.store, "proj", body, A.policy(), "t")
            resolved = ops.resolve_config(env.store, env.registry, "proj",
                                          created["config_id"], A.policy(), "t")
            record = ops.start_run(env.store, env.registry, "proj", resolved["resolved_id"])
        self.assertEqual(record["run"]["status"], "error")
        self.assertIn("no model client bound", record["run"]["error"])

    def test_claimed_vs_actual_is_deterministic(self):
        snap = {"ledger": [{"amount_minor": 250000}]}
        self.assertEqual(A.claimed_vs_actual({"refunds_issued": 1, "total_refunded": 2500.0},
                                             snap)["verdict"], "match")
        self.assertEqual(A.claimed_vs_actual({"refunds_issued": 1, "total_refunded": 2499},
                                             snap)["verdict"], "false_success")


class CapsuleReplayTests(unittest.TestCase):
    def record(self, steps, variant):
        model = RecordingClient(ScriptedModel(steps))
        with Env() as env:
            result = env.run(model, variant)
        return capsules.build(result, model.records, provider="fake", model="fake-model",
                              label=variant)

    def test_replay_reproduces_without_network(self):
        capsule = self.record(NAIVE, "llm-naive")
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/c.json"
            capsules.write(path, capsule)
            out = capsules.replay(path)
        self.assertTrue(out["reproduced"], out)
        self.assertEqual(out["claim_check"]["verdict"], "false_success")
        self.assertEqual(out["model_calls_live"], 0)

    def test_tampered_or_diverged_replay_fails(self):
        capsule = self.record(HARDENED, "llm-hardened")
        payload = capsule["payload"]
        with tempfile.TemporaryDirectory() as tmp:
            capsule["payload"]["turns"][1]["turn"]["tool_calls"][0]["arguments"]["amount"] = 1
            capsules.write(f"{tmp}/bad.json", capsule)
            with self.assertRaises(ValueError):  # checksum catches the edit
                capsules.load(f"{tmp}/bad.json")
        # Same edit with a re-sealed envelope: replay itself diverges.
        out = capsules.replay_payload(payload)
        self.assertFalse(out["reproduced"])


if __name__ == "__main__":
    unittest.main()
