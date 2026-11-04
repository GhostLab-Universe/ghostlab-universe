"""Probe gates and report honesty. The 'live' path here runs on a MOCK transport only."""

import importlib.util
import json

import httpx
import pytest

from m01_fixtures import (PROPOSAL, ROOT, SECRET, FakeTime, Recorder, meta_call,
                          meta_response, meta_text)
from ghostlab.providers import Runtime

spec = importlib.util.spec_from_file_location("probe_provider", ROOT / "scripts" / "probe_provider.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

LIVE = ["--provider", "meta_responses", "--live", "--model", "fixture-model",
        "--confirm-paid-calls"]


def mock_runtime(*responses):
    recorder = Recorder(*responses)
    fake = FakeTime()
    return Runtime(transport=httpx.MockTransport(recorder), sleep=fake.sleep,
                   monotonic=fake.monotonic, jitter=lambda: 0.0), recorder


def test_dry_run_is_default_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    runtime, recorder = mock_runtime()
    report_path = tmp_path / "report.json"
    assert probe.main(["--provider", "meta_responses", "--report", str(report_path)],
                      runtime=runtime) == 0
    report = json.loads(report_path.read_text())
    assert report["mode"] == "dry_run" and report["live"] is False
    assert report["calls_made"] == 0 and recorder.requests == []


@pytest.mark.parametrize("argv", [
    ["--provider", "meta_responses", "--live"],
    ["--provider", "meta_responses", "--live", "--model", "m", "--max-calls", "4"],
    ["--provider", "meta_responses", "--live", "--model", "m", "--confirm-paid-calls"],
    [*LIVE, "--max-calls", "0"],
    [*LIVE, "--max-calls", "11"],
], ids=["flags-missing", "no-confirm", "no-cap", "cap-zero", "cap-too-high"])
def test_live_mode_refuses_without_every_gate(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    runtime, recorder = mock_runtime()
    with pytest.raises(SystemExit):
        probe.main([*argv, "--report", str(tmp_path / "r.json")], runtime=runtime)
    assert recorder.requests == []


def test_live_mode_refuses_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    runtime, recorder = mock_runtime()
    with pytest.raises(SystemExit):
        probe.main([*LIVE, "--max-calls", "4", "--report", str(tmp_path / "r.json")],
                   runtime=runtime)
    assert recorder.requests == []


def test_mock_probe_respects_call_cap_and_is_not_reported_live(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    usage = {"input_tokens": 10, "output_tokens": 3}
    runtime, recorder = mock_runtime(
        (200, meta_response([meta_text("ready")], usage=usage)),
        (200, meta_response([meta_call("call_1")], response_id="resp_t1")),
        (200, meta_response([meta_text("done")], response_id="resp_t2")),
        (200, meta_response([meta_text(PROPOSAL)], usage=usage)))
    path = tmp_path / "r.json"
    assert probe.main([*LIVE, "--max-calls", "4", "--report", str(path)], runtime=runtime) == 0
    text = path.read_text()
    report = json.loads(text)
    assert report["transport"] == "mock" and report["live"] is False
    assert report["calls_made"] == 4 == len(recorder.requests)
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert statuses == {"auth_minimal": "passed", "tool_roundtrip": "passed",
                        "continuation_after_tool_result": "passed",
                        "structured_proposal": "passed", "usage_capture": "passed"}
    sent = json.loads(recorder.requests[2].content)["input"][0]
    assert json.loads(sent["output"])["synthetic_fixture"] is True
    assert SECRET not in text


def test_call_cap_skips_checks_instead_of_overspending(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_API_KEY", SECRET)
    runtime, recorder = mock_runtime((200, meta_response([meta_text("ready")])))
    path = tmp_path / "r.json"
    probe.main([*LIVE, "--max-calls", "1", "--report", str(path)], runtime=runtime)
    report = json.loads(path.read_text())
    assert len(recorder.requests) == 1 == report["calls_made"]
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert statuses["tool_roundtrip"] == statuses["structured_proposal"] == "skipped_call_cap"
    assert statuses["usage_capture"] == "failed"  # fixture omitted usage: recorded, not faked
