"""Public live limits and the LIVE_MODE kill switch. No network: explore is a no-op."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from ghostlab.api import llm_routes
from ghostlab.api.app import create_app
from ghostlab.llm.client import ProviderUnavailable, client_for, provider_status
from ghostlab.llm.spend import CappedLedger, SpendLedger, SpendRefused

PUBLIC = {"META_API_KEY_DEV": "dev-not-real", "META_API_KEY_PUBLIC": "public-not-real",
          "GLIMMER_BASE_URL": "", "LIVE_MODE": "1"}


class _Base(unittest.TestCase):
    env: dict = {}
    stub_explore = True  # live runs must not reach the network; replay needs the real one

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [mock.patch.dict(os.environ, {**self.env,
                                                "GHOSTLAB_WEB_DIST": self.tmp.name})]
        if self.stub_explore:
            patches.append(mock.patch.object(llm_routes.ADV, "explore", lambda state: None))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (Path(self.tmp.name) / "index.html").write_text("<!doctype html>spa", encoding="utf-8")
        self.client = TestClient(create_app(Path(self.tmp.name) / "r.sqlite",
                                            Path(self.tmp.name) / "s.sqlite"))
        self.addCleanup(self.tmp.cleanup)

    def live(self, ip="1.2.3.4", **body):
        return self.client.post("/v1/explorations", json={"mode": "live", **body},
                                headers={"x-forwarded-for": f"9.9.9.9, {ip}"})


class PublicLimitTests(_Base):
    env = PUBLIC

    def test_only_public_key_is_live(self):
        self.assertEqual(provider_status("meta-dev")["status"], "unavailable")
        self.assertEqual(provider_status("meta-public")["status"], "available")
        with self.assertRaises(ProviderUnavailable):
            client_for("meta-dev")
        r = self.live(provider="meta-dev")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.live().json()["provider"], "meta-public")

    def test_three_per_ip_per_day_then_429(self):
        for _ in range(3):
            self.assertEqual(self.live().status_code, 200)
        r = self.live()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"], "rate_limited")
        self.assertEqual(self.live(ip="5.6.7.8").status_code, 200)
        # replay is never limited
        self.assertEqual(self.client.post("/v1/explorations", json={"mode": "replay"},
                                          headers={"x-forwarded-for": "1.2.3.4"}).status_code,
                         200)

    def test_process_cap_then_429(self):
        with mock.patch.object(llm_routes, "PUBLIC_PER_PROCESS", 2):
            self.assertEqual(self.live(ip="1.1.1.1").status_code, 200)
            self.assertEqual(self.live(ip="2.2.2.2").status_code, 200)
            self.assertEqual(self.live(ip="3.3.3.3").status_code, 429)

    def test_body_limits_are_clamped(self):
        r = self.live(max_usd=3, max_experiments=25, llm_targets=True)
        self.assertEqual(r.status_code, 200, r.text)
        entry = self.client.app.state.explorations[r.json()["exploration_id"]]
        self.assertLessEqual(entry["state"]["limits"]["max_usd"], 0.10)
        self.assertLessEqual(entry["state"]["explorer"].max_experiments, 6)
        self.assertIsInstance(entry["ledger"], CappedLedger)
        self.assertEqual(entry["ledger"].cap, 0.10)

    def test_spa_fallback_and_v1_404(self):
        self.assertIn("spa", self.client.get("/lab").text)
        self.assertIn("spa", self.client.get("/guide/1-environment").text)
        r = self.client.get("/v1/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "not_found")
        self.assertEqual(self.client.get("/v1/health").json(), {"status": "ok"})
        self.assertNotIn("build-system", self.client.get("/..%2F..%2Fpyproject.toml").text)


class KillSwitchTests(_Base):
    env = {**PUBLIC, "LIVE_MODE": "0"}
    stub_explore = False  # live is refused before any client exists

    def test_live_off_everywhere_replay_still_works(self):
        rows = self.client.get("/v1/providers").json()["providers"]
        self.assertTrue(all(r["status"] == "unavailable" for r in rows))
        self.assertEqual(self.live().status_code, 503)
        self.assertEqual(self.live(provider="meta-dev").status_code, 503)
        if "adversary-exploration-llm.json" not in llm_routes.recorded_names():
            self.skipTest("no committed llm exploration recording")
        start = self.client.post("/v1/explorations", json={"llm_targets": True}).json()
        self.assertEqual(start["mode"], "replay")
        for _ in range(200):
            view = self.client.get(f"/v1/explorations/{start['exploration_id']}").json()
            if view["status"] == "stopped":
                break
            time.sleep(0.05)
        self.assertEqual(view["stop_status"], "found_failure", view)


class CappedLedgerTests(unittest.TestCase):
    def test_refuses_over_cap_and_counts_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CappedLedger(SpendLedger(Path(tmp) / "spend.json"), 0.10)
            ledger.check("public", 0.05)
            ledger.record(label="public", provider="meta-public", model="muse-spark-1.3",
                          request_id="r1", input_tokens=10_000, output_tokens=20_000)
            self.assertGreater(ledger.spent, 0.09)
            with self.assertRaises(SpendRefused):
                ledger.check("public", 0.01)


if __name__ == "__main__":
    unittest.main()
