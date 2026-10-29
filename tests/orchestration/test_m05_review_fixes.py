"""Regressions for the PR #7 probe findings (offline; real SQLite stores; no network)."""

import json

import httpx
import pytest

from ghostlab.orchestration import (REQUIRED_COMPATIBILITY_CHECKS, LiveInferenceAuthorization,
                                    LiveInferenceRefused, ledger_key)
from ghostlab.providers import Credential, ProviderConfig, RetryPolicy

from m05_harness import BUSY, MODEL, PROJECT, Lab, Script, proposal, spec

CID = "campaign-1"
KEY = ledger_key(PROJECT, CID)


class CountingTransport(httpx.BaseTransport):
    """A non-mock transport that never leaves the process: it counts what would be sent."""

    def __init__(self, script):
        self.script = script

    def handle_request(self, request):
        return httpx.MockTransport(self.script).handle_request(request)


def full_report(path, **changes):
    data = {"live": True, "mode": "live", "transport": "network", "model": MODEL,
            "provider": "meta_responses", "endpoint": "https://api.meta.ai/v1",
            "checks": [{"check": name, "status": "passed"}
                       for name in sorted(REQUIRED_COMPATIBILITY_CHECKS)], **changes}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def crash_before_propose():
    crashed = []

    def hook(name, state):
        if name == "propose" and not crashed:
            crashed.append(name)
            raise RuntimeError("simulated crash before propose")
    return hook


# --- finding 1: recovery and resume re-run the inference gate ----------------------

def test_recover_refuses_an_unauthorized_non_mock_transport(tmp_path):
    lab = Lab(tmp_path, Script(proposal()), on_node=crash_before_propose())
    with lab.controller() as controller, pytest.raises(RuntimeError):
        controller.start(CID, spec())
    swapped = Script(proposal())
    restarted = Lab(tmp_path, swapped, real_transport=CountingTransport(swapped))
    with restarted.controller() as controller:
        with pytest.raises(LiveInferenceRefused):
            controller.recover(CID)
        with pytest.raises(LiveInferenceRefused):
            controller.resume(CID)
    assert swapped.requests == [] and lab.script.requests == []


def test_recover_refuses_a_changed_model_or_endpoint(tmp_path):
    lab = Lab(tmp_path, Script(proposal()), on_node=crash_before_propose())
    with lab.controller() as controller, pytest.raises(RuntimeError):
        controller.start(CID, spec())
    for change in ({"model": "another-model"}, {"base_url": "https://example.invalid/v1"}):
        other = Lab(tmp_path, Script(proposal()))
        other.config = ProviderConfig(**{"provider": "meta_responses", "model": MODEL,
                                         "base_url": "https://api.meta.ai/v1",
                                         "credential": other.config.credential, **change})
        with other.controller() as controller, pytest.raises(LiveInferenceRefused):
            controller.recover(CID)
        assert other.script.requests == []


def test_authorized_recovery_uses_the_smaller_live_cap(tmp_path, monkeypatch):
    lab = Lab(tmp_path, Script(proposal()), on_node=crash_before_propose())
    with lab.controller() as controller, pytest.raises(RuntimeError):
        controller.start(CID, spec(max_model_calls=10))
    monkeypatch.setenv("MODEL_API_KEY", "sk-fixture-live-env-000")
    script = Script(*([BUSY] * 6))
    live = LiveInferenceAuthorization(model=MODEL, max_model_calls=2, confirm_paid_calls=True,
                                      compatibility_report=full_report(tmp_path / "r.json"))
    authorized = Lab(tmp_path, script, real_transport=CountingTransport(script), live=live)
    authorized.config = ProviderConfig(provider="meta_responses", model=MODEL,
                                       base_url="https://api.meta.ai/v1",
                                       credential=Credential.from_env("MODEL_API_KEY"),
                                       retry=RetryPolicy(max_attempts=3))
    with authorized.controller() as controller:
        state = controller.recover(CID)
    assert len(script.requests) == 2  # min(campaign 10, live cap 2); nothing past the cap
    assert state["status"] == "budget_exhausted" and state["outcome"] == "model_calls"


# --- finding 2: Explorer task usage survives retries and recovery --------------------

def test_explorer_ceiling_holds_across_task_retries(tmp_path):
    lab = Lab(tmp_path, Script(*([BUSY] * 6)), retry_attempts=3)
    with lab.controller() as controller:
        state = controller.start(CID, spec(max_model_calls=20))
    assert len(lab.script.requests) == 5  # Explorer ceiling: 5 per task, across both attempts
    assert state["status"] == "budget_exhausted" and state["outcome"] == "model_calls"
    assert lab.ledger.committed(KEY, "model_calls")[0] == 5


def test_explorer_ceiling_holds_across_crash_recovery(tmp_path):
    lab = Lab(tmp_path, Script(BUSY, BUSY, RuntimeError("crash during the third attempt")),
              retry_attempts=3)
    with lab.controller() as controller, pytest.raises(RuntimeError):
        controller.start(CID, spec(max_model_calls=20))
    assert len(lab.script.requests) == 3
    lab.script.add(*([BUSY] * 5))
    with lab.controller() as controller:
        state = controller.recover(CID)
    assert len(lab.script.requests) == 5  # 3 before the crash + 2 left in the task ceiling
    assert state["status"] == "budget_exhausted"


# --- finding 3: the compatibility report must be complete and bound -----------------

@pytest.mark.parametrize("changes", [
    {"checks": [{"check": "invented", "status": "passed"}]},
    {"checks": [{"check": c, "status": "passed"} for c in
                sorted(REQUIRED_COMPATIBILITY_CHECKS - {"usage_capture"})]},
    {"provider": "baseten_chat_completions"},
    {"endpoint": "https://example.invalid/v1"},
    {"mode": "dry_run"},
    {"checks": "passed"},
], ids=["invented-only", "missing-one", "provider", "endpoint", "dry-run", "not-a-list"])
def test_incomplete_or_unbound_report_is_refused(tmp_path, monkeypatch, changes):
    monkeypatch.setenv("MODEL_API_KEY", "sk-fixture-live-env-000")
    script = Script(proposal())
    live = LiveInferenceAuthorization(model=MODEL, max_model_calls=2, confirm_paid_calls=True,
                                      compatibility_report=full_report(tmp_path / "r.json",
                                                                       **changes))
    lab = Lab(tmp_path / "lab", script, real_transport=CountingTransport(script), live=live)
    lab.config = ProviderConfig(provider="meta_responses", model=MODEL,
                                base_url="https://api.meta.ai/v1",
                                credential=Credential.from_env("MODEL_API_KEY"))
    with lab.controller() as controller, pytest.raises(LiveInferenceRefused):
        controller.start(CID, spec())
    assert script.requests == []


# --- finding 4: no HTTP attempt after the campaign deadline --------------------------

def test_retries_stop_at_the_campaign_deadline(tmp_path):
    lab = Lab(tmp_path, Script(), retry_attempts=3)

    def slow_busy(request):
        lab.clock.advance(seconds=1)  # this attempt alone outlives the 1 ms campaign
        return BUSY

    lab.script.add(slow_busy, BUSY, BUSY)
    with lab.controller() as controller:
        state = controller.start(CID, spec(max_wall_clock_ms=1))
    assert len(lab.script.requests) == 1  # no retry was dispatched after the deadline
    assert state["status"] == "budget_exhausted" and state["outcome"] == "wall_clock_ms"
