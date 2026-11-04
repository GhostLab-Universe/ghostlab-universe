"""Phase 2B fairness: seeded-bug variants, opaque ids, neutral prompt, key behaviour (no network)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import benchmark as B  # noqa: E402
import calibrate as CAL  # noqa: E402
from ghostlab.llm import adversary as ADV  # noqa: E402
from tests.test_llm_adversary import Script, sched  # noqa: E402

EXPECTED = {  # real agent -> failing (tool, occurrence) cells, one timeout_after_commit
    "refund-env": {"vulnerable": {("*", 2), ("issue_refund", 1)},
                   "repaired": set(),
                   "status-ignored": {("*", 2), ("issue_refund", 1)},
                   "retry-nokey": {("*", 3), ("issue_refund", 1)},
                   "split-key-per-attempt": {("*", 2), ("*", 3), ("issue_refund", 1),
                                             ("issue_refund", 2)}},
    "booking-env": {"vulnerable": {("*", 2), ("book_equipment", 1)},
                    "repaired": set(),
                    "status-ignored": {("*", 2), ("book_equipment", 1)},
                    "double-submit": {("*", 3), ("book_equipment", 1)}},
}


class SeededVariantTests(unittest.TestCase):
    def test_each_variant_fails_exactly_at_its_cells(self):
        for env_id, agents in EXPECTED.items():
            profile = B.ENVS[env_id][1]()
            aliases = {a: a for a in agents}
            with tempfile.TemporaryDirectory() as tmp:
                registry, store = B.fresh(env_id, tmp)
                cat = ADV.catalog(registry, list(aliases), env_id, max_faults=1)
                for agent, cells in agents.items():
                    got = {(t, o) for t in cat["trigger_tools"] for o in cat["occurrences"]
                           if B.run_cell(profile, registry, store, "t", aliases, (agent, t, o), 0)}
                    self.assertEqual(got, cells, (env_id, agent))

    def test_opaque_ids_hide_real_names(self):
        ids = B.opaque_ids()
        self.assertEqual(sorted(ids["refund-env"].values()), sorted(B.REFUND_AGENTS))
        for env_id, aliases in ids.items():
            for opaque in aliases:
                self.assertRegex(opaque, r"^agent-\d\d$")


class NeutralExplorerTests(unittest.TestCase):
    def test_model_sees_only_opaque_ids_and_neutral_prompt(self):
        for word in ("timeout", "commit", "idempoten", "duplicate", "refund", "retry"):
            self.assertNotIn(word, ADV.NEUTRAL_SYSTEM.lower())
        aliases = B.opaque_ids()["booking-env"]
        buggy = next(o for o, real in aliases.items() if real == "vulnerable")
        with tempfile.TemporaryDirectory() as tmp:
            registry, store = B.fresh("booking-env", tmp)
            model = Script([[("list_fault_catalog", {})],
                            [("run_experiment", {"fault_schedule": sched("book_equipment"),
                                                 "agent_variant": "vulnerable"})],
                            [("run_experiment", {"fault_schedule": sched("book_equipment"),
                                                 "agent_variant": buggy})]])
            explorer = ADV.ModelExplorer(model, store=store, registry=registry, project_id="t",
                                         max_experiments=5, system=ADV.NEUTRAL_SYSTEM,
                                         aliases=aliases, env=B.booking_profile(), max_faults=1)
            state = ADV.explore(ADV.new_state(explorer))
        self.assertEqual(state["stop_status"], "found_failure")
        self.assertEqual(len(explorer.rejected), 1)  # a real name is not a valid id
        seen = json.dumps([s for s in model.sent if s])
        for real in set(aliases.values()):
            self.assertNotIn(f'\\"{real}\\"', seen)
        self.assertEqual(state["failure"]["agent_variant"], buggy)


class KeyBehaviourTests(unittest.TestCase):
    def test_showcase_capsules(self):
        from ghostlab.llm import capsules
        dup = capsules.load(ROOT / "demo/recorded/naive-spark-duplicate.json")
        ok = capsules.load(ROOT / "demo/recorded/hardened-spark-pass.json")
        self.assertEqual(CAL.key_behaviour(dup["summary"]["gateway_actions"])["first_key"],
                         "omitted")
        k = CAL.key_behaviour(ok["summary"]["gateway_actions"])
        self.assertEqual((k["first_key"], k["read_path_used"]), ("supplied", True))
        self.assertEqual(CAL.key_behaviour([
            {"tool": "issue_refund", "arguments": {"idempotency_key": "a"}},
            {"tool": "issue_refund", "arguments": {"idempotency_key": "a"}}])["retry_key"], "same")


if __name__ == "__main__":
    unittest.main()
