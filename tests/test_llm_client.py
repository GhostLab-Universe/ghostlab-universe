"""Spend ledger, provider clients and replay: fake transports only, no network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from ghostlab.llm import client as C
from ghostlab.llm.spend import SpendLedger, SpendRefused, usd
from ghostlab.providers.base import Credential, ProviderConfig, ProviderConfigError


def ledger(tmp, **caps):
    return SpendLedger(Path(tmp) / "spend.json", caps=caps or {"dev": 1.0, "glimmer": None})


def responses_body(rid, *, call=None, text=None):
    output = []
    if call:
        output.append({"type": "function_call", "call_id": call[0], "name": call[1],
                       "arguments": json.dumps(call[2])})
    if text:
        output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
    return {"id": rid, "status": "completed", "model": "muse-spark-1.3", "output": output,
            "usage": {"input_tokens": 1000, "output_tokens": 200,
                      "output_tokens_details": {"reasoning_tokens": 150},
                      "input_tokens_details": {"cached_tokens": 0}}}


def meta_client(tmp, handler, **opts):
    config = ProviderConfig(provider="meta_responses", model="muse-spark-1.3",
                            credential=Credential("sk-test-not-real-0000", "test"),
                            base_url="https://api.meta.ai/v1")
    return C.ResponsesClient("meta-dev", config, label="dev", ledger=ledger(tmp),
                             transport=httpx.MockTransport(handler), **opts)


class SpendTests(unittest.TestCase):
    def test_env_cap_only_lowers(self):
        from ghostlab.llm.spend import _env_cap
        with mock.patch.dict(os.environ, {"GHOSTLAB_DEV_CAP_USD": "8.25"}):
            self.assertEqual(_env_cap(120.0), 8.25)
        with mock.patch.dict(os.environ, {"GHOSTLAB_DEV_CAP_USD": "500"}):
            self.assertEqual(_env_cap(120.0), 120.0)
        with mock.patch.dict(os.environ, {"GHOSTLAB_DEV_CAP_USD": ""}):
            self.assertEqual(_env_cap(120.0), 120.0)

    def test_price_and_cap(self):
        self.assertAlmostEqual(usd("muse-spark-1.3", 1_000_000, 1_000_000), 5.5)
        self.assertEqual(usd("some-local-model", 10**6, 10**6), 0.0)
        with tempfile.TemporaryDirectory() as tmp:
            book = ledger(tmp)
            book.record(label="dev", provider="meta-dev", model="muse-spark-1.3",
                        request_id="r1", input_tokens=100_000, output_tokens=100_000)
            self.assertAlmostEqual(book.spent("dev"), 0.55)
            book.check("dev", 0.4)
            with self.assertRaises(SpendRefused):
                book.check("dev", 0.5)
            book.check("glimmer", 1e9)  # uncapped
            with self.assertRaises(SpendRefused):
                book.check("nope", 0.0)
            text = (Path(tmp) / "spend.json").read_text()
            self.assertNotIn("sk-", text)
            self.assertEqual(book.totals()["dev"]["calls"], 1)


class ResponsesClientTests(unittest.TestCase):
    def test_tool_roundtrip_records_spend_and_continuation(self):
        seen = []

        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            if len(seen) == 1:
                return httpx.Response(200, json=responses_body(
                    "resp_1", call=("c1", "get_order", {"order_id": "o"})))
            return httpx.Response(200, json=responses_body("resp_2", text="done"))

        with tempfile.TemporaryDirectory() as tmp:
            client = meta_client(tmp, handler, reasoning_effort="medium")
            conv = client.open("sys", [{"name": "get_order", "description": "d",
                                        "parameters": {"type": "object"}}])
            first = conv.send(user="hi")
            self.assertEqual(first.status, "tool_calls")
            self.assertEqual(first.tool_calls[0]["arguments"], {"order_id": "o"})
            self.assertEqual(first.usage["reasoning_tokens"], 150)
            second = conv.send(tool_results=[{"id": "c1", "output": "{}"}])
            self.assertEqual(second.text, "done")
            self.assertEqual(seen[1]["previous_response_id"], "resp_1")
            self.assertEqual(seen[1]["input"][0]["call_id"], "c1")
            self.assertEqual(seen[0]["reasoning"], {"effort": "medium"})
            self.assertEqual(len(client.calls), 2)
            self.assertGreater(client.session_usd, 0)
            self.assertEqual(client.ledger.totals()["dev"]["calls"], 2)

    def test_session_cap_refuses_before_send(self):
        sent = []

        def handler(request):
            sent.append(1)
            return httpx.Response(200, json=responses_body("r", text="x"))

        with tempfile.TemporaryDirectory() as tmp:
            client = meta_client(tmp, handler, usd_limit=0.0001)
            turn = client.open("s", []).send(user="u")
            self.assertEqual(turn.status, "failed")
            self.assertIn("session cap", turn.error)
            self.assertEqual(sent, [])

    def test_key_cap_refuses_before_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = meta_client(tmp, lambda r: httpx.Response(200, json={}))
            client.ledger.caps["dev"] = 0.0
            turn = client.open("s", []).send(user="u")
            self.assertEqual(turn.status, "failed")
            self.assertIn("cap", turn.error)


class ChatClientTests(unittest.TestCase):
    def test_glimmer_chat_tool_calls(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["messages"][0]["role"] == "system"
            return httpx.Response(200, json={"id": "chat-1", "choices": [{
                "finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
                    {"id": "t1", "function": {"name": "get_order",
                                              "arguments": "{\"order_id\": \"o\"}"}}]}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5}})

        with tempfile.TemporaryDirectory() as tmp:
            config = ProviderConfig(provider="glimmer_local", model=C.GLIMMER_MODEL,
                                    credential=Credential("ollama-local-no-auth", "none"),
                                    base_url="http://127.0.0.1:11434/v1")
            client = C.ChatClient("glimmer-local", config, label="glimmer", ledger=ledger(tmp),
                                  transport=httpx.MockTransport(handler))
            conv = client.open("sys", [{"name": "get_order", "description": "d",
                                        "parameters": {"type": "object"}}])
            turn = conv.send(user="hi")
            self.assertEqual(turn.tool_calls[0]["name"], "get_order")
            self.assertEqual(client.calls[0]["usd"], 0.0)
            self.assertEqual(conv.messages[-1]["tool_calls"][0]["id"], "t1")


class GlimmerHttpExceptionTests(unittest.TestCase):
    def cfg(self, provider, url):
        return ProviderConfig(provider=provider, model="m",
                              credential=Credential("x" * 8, "t"), base_url=url)

    def test_http_only_for_glimmer_on_local_hosts(self):
        self.cfg("glimmer_local", "http://127.0.0.1:11434/v1")
        self.cfg("glimmer_local", "http://localhost:11434/v1")
        with mock.patch("ghostlab.providers.base.default_gateway_ip", return_value="10.9.8.7"):
            self.cfg("glimmer_local", "http://10.9.8.7:11434/v1")
        for provider, url in [("glimmer_local", "http://example.com/v1"),
                              ("glimmer_local", "http://192.168.1.50:11434/v1"),
                              ("meta_responses", "http://127.0.0.1:11434/v1"),
                              ("glimmer_local", "ftp://127.0.0.1/v1")]:
            with self.subTest(url=url, provider=provider), \
                    mock.patch("ghostlab.providers.base.default_gateway_ip", return_value=None):
                with self.assertRaises(ProviderConfigError):
                    self.cfg(provider, url)


class AvailabilityTests(unittest.TestCase):
    def test_missing_key_is_unavailable_not_substituted(self):
        with mock.patch.dict(os.environ, {"META_API_KEY_DEV": "", "GLIMMER_BASE_URL": ""}):
            self.assertEqual(C.provider_status("meta-dev")["status"], "unavailable")
            self.assertIn("GLIMMER_BASE_URL", C.provider_status("glimmer-local")["reason"])
            with self.assertRaises(C.ProviderUnavailable):
                C.client_for("meta-dev")
            with self.assertRaises(C.ProviderUnavailable):
                C.client_for("openai")

    def test_key_label_mapping(self):
        with mock.patch.dict(os.environ, {"META_API_KEY_PUBLIC": "k" * 12}):
            client = C.client_for("meta-public")
            self.assertEqual(client.label, "public")
            self.assertEqual(client.config.credential.source, "env:META_API_KEY_PUBLIC")


class ReplayTests(unittest.TestCase):
    def test_record_then_replay_and_diverge(self):
        def handler(request):
            return httpx.Response(200, json=responses_body("r1", text="ok"))

        with tempfile.TemporaryDirectory() as tmp:
            rec = C.RecordingClient(meta_client(tmp, handler))
            rec.open("s", []).send(user="u")
            replay = C.ReplayClient(rec.records)
            self.assertEqual(replay.open("s", []).send(user="u").text, "ok")
            self.assertTrue(replay.exhausted)
            with self.assertRaises(C.ReplayDiverged):
                replay.open("s", []).send(user="u")  # more turns than recorded
            with self.assertRaises(C.ReplayDiverged):
                C.ReplayClient(rec.records).open("s", []).send(user="changed")


if __name__ == "__main__":
    unittest.main()
