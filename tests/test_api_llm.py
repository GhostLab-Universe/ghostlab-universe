"""Live-model API routes, keyless: replay recordings, allowlisted names, no network."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from ghostlab.api.app import create_app
from ghostlab.api import llm_routes

NO_KEYS = {"META_API_KEY_DEV": "", "META_API_KEY_PUBLIC": "", "GLIMMER_BASE_URL": ""}


class LlmRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, NO_KEYS)
        self.env.start()
        self.client = TestClient(create_app(Path(self.tmp.name) / "r.sqlite",
                                            Path(self.tmp.name) / "s.sqlite"))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_providers_report_unavailable_without_keys(self):
        rows = self.client.get("/v1/providers").json()["providers"]
        self.assertTrue(all(r["status"] == "unavailable" for r in rows))

    def test_live_without_key_is_503_and_default_is_replay(self):
        r = self.client.post("/v1/explorations", json={"mode": "live"})
        self.assertEqual(r.status_code, 503)
        if "adversary-exploration.json" not in llm_routes.recorded_names():
            self.skipTest("no committed exploration recording")
        start = self.client.post("/v1/explorations", json={}).json()
        self.assertEqual(start["mode"], "replay")
        for _ in range(100):
            view = self.client.get(f"/v1/explorations/{start['exploration_id']}").json()
            if view["status"] == "stopped":
                break
            time.sleep(0.05)
        self.assertEqual(view["status"], "stopped", view)
        self.assertTrue(view["reproduced"], view)
        self.assertEqual(view["stop_status"], "found_failure")
        self.assertTrue(all(c.get("replayed") for c in view["model_calls"]))

    def test_llm_replay_feeds_fork_reality_and_claim(self):
        if "adversary-exploration-llm.json" not in llm_routes.recorded_names():
            self.skipTest("no committed llm exploration recording")
        start = self.client.post("/v1/explorations", json={"llm_targets": True}).json()
        for _ in range(200):
            view = self.client.get(f"/v1/explorations/{start['exploration_id']}").json()
            if view["status"] == "stopped":
                break
            time.sleep(0.05)
        self.assertEqual(view["stop_status"], "found_failure", view)
        self.assertTrue(all(e.get("hypothesis") for e in view["experiments"]), view)
        self.assertTrue(all(e["proposed_by"]["replayed"] for e in view["experiments"]))
        run = self.client.get(f"/v1/explorations/{start['exploration_id']}"
                              f"/runs/{view['failure']['run_id']}").json()
        self.assertEqual(len(run["baseline"]["ledger"]), 1, run)
        self.assertEqual(len(run["faulted"]["ledger"]), 2, run)
        self.assertEqual(run["claim_check"]["verdict"], "false_success", run)
        self.assertEqual(self.client.get(f"/v1/explorations/{start['exploration_id']}"
                                         "/runs/run-999999").status_code, 404)

    def test_evidence_route_has_tables_and_caveats(self):
        out = self.client.get("/v1/evidence").json()
        if out["calibration"] is not None:
            self.assertTrue(all("runs" in row for row in out["calibration"]["table"]))
        if out["calibration"] is not None and out["calibration"]["by_first_key"]:
            omitted = out["calibration"]["by_first_key"].get("omitted")
            if omitted:
                self.assertLessEqual(omitted["duplicates"], omitted["n"])
        if out["benchmark"] is not None:
            envs = out["benchmark"]["envs"]
            self.assertTrue(envs)
            for env in envs.values():
                self.assertTrue(all("trials" in v for v in env["strategies"].values()))
            self.assertTrue(out["benchmark_caveats"])

    def test_replay_names_are_allowlisted(self):
        for name in ("../../pyproject.toml", "/etc/passwd", "nope.json"):
            r = self.client.post("/v1/capsules/replay", json={"name": name})
            self.assertEqual(r.status_code, 404, name)

    def test_capsule_replay_keyless(self):
        names = [n for n in llm_routes.recorded_names() if n.startswith("calibration/")]
        if not names:
            self.skipTest("no committed calibration capsules")
        out = self.client.post("/v1/capsules/replay", json={"name": names[0]}).json()
        self.assertTrue(out["reproduced"], out)
        self.assertEqual(out["model_calls_live"], 0)

    def test_spend_and_bad_bodies(self):
        self.assertIn("caps_usd", self.client.get("/v1/spend").json())
        r = self.client.post("/v1/explorations", json={"max_usd": 50})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/v1/explorations", json={"evil": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/v1/explorations/exp-none").status_code, 404)


if __name__ == "__main__":
    unittest.main()
