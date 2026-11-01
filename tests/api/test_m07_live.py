"""Live enablement at the API boundary: mapped errors, live_config gates, CLI checks.

The compatibility reports here are hand-typed to pin gate LOGIC (which field
refuses, which passes); they are not live evidence and never leave tmp_path.
"""

import json

import httpx
import pytest

from ghostlab.api import create_app
from ghostlab.api.__main__ import main
from ghostlab.api.errors import describe
from ghostlab.api.live import QUALIFIED_LIVE_MODEL, live_config
from ghostlab.api.plane import ControlPlane
from ghostlab.api.sessions import LocalDevSessions
from ghostlab.contracts import Principal
from ghostlab.orchestration.inference import LiveInferenceRefused
from ghostlab.providers import OutboundRefused, ProviderConfigError, ProviderRequestError
from ghostlab.security.redaction import Redactor

FAKE_KEY = "test-live-key-value-not-a-real-credential"
CHECKS = ("auth_minimal", "tool_roundtrip", "continuation_after_tool_result",
          "structured_proposal", "usage_capture")


def passing_report(path, **changes):
    report = {"live": True, "transport": "network", "mode": "live",
              "provider": "meta_responses", "endpoint": "https://api.meta.ai/v1",
              "model": QUALIFIED_LIVE_MODEL,
              "checks": [{"check": name, "status": "passed", "detail": "",
                          "attempts": []} for name in CHECKS]}
    report.update(changes)
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def sessions():
    return LocalDevSessions(Principal(principal_id="operator-1", kind="human"),
                            "project-live-test")


def live_kwargs(report, **changes):
    options = {"bind_host": "127.0.0.1", "model": QUALIFIED_LIVE_MODEL, "max_calls": 4,
               "compat_report": report, "confirm_paid_calls": True,
               "allow_server_retention": False,
               "environ": {"MODEL_API_KEY": FAKE_KEY}}
    options.update(changes)
    return options


# -- mapped errors --------------------------------------------------------------


@pytest.mark.parametrize("reason,status,code,retryable", [
    ("retention_not_authorized", 403, "forbidden", False),
    ("prohibited_content", 403, "forbidden", False),
    ("payload_too_large", 413, "validation_failed", False),
    ("concurrency_exhausted", 429, "rate_limited", True),
], ids=["retention", "content", "too-large", "concurrency"])
def test_outbound_refusal_maps_off_500(reason, status, code, retryable):
    observed, body, _ = describe(OutboundRefused(reason), Redactor())
    assert observed == status
    assert body.error.code == code and body.reason == reason
    assert body.error.retryable is retryable
    assert body.error.side_effect == "none"  # nothing was sent


def test_malformed_model_request_maps_422_without_echo():
    observed, body, _ = describe(ProviderRequestError(f"boom {FAKE_KEY}"), Redactor())
    assert observed == 422
    assert body.error.code == "validation_failed" and body.reason == "malformed_model_request"
    assert body.error.message == "The model request was malformed; nothing was sent"
    assert FAKE_KEY not in body.error.message


def test_handlers_installed_for_outbound_errors():
    handlers = create_app().exception_handlers
    assert OutboundRefused in handlers and ProviderRequestError in handlers


# -- live_config gates ----------------------------------------------------------


def test_live_config_wires_qualified_path(tmp_path):
    report = passing_report(tmp_path / "compat.json")
    config = live_config(tmp_path / "root", sessions(), **live_kwargs(report))
    assert config.scripted is False
    assert config.services.live.model == QUALIFIED_LIVE_MODEL
    assert config.services.live.max_model_calls == 4
    assert config.services.runtime.transport is None  # the network, gated
    assert config.services.runtime.outbound.allow_server_retention is False
    probed = config.services.adapter_factory(config.services.runtime).config
    assert (probed.provider, probed.model, probed.base_url) == (
        "meta_responses", QUALIFIED_LIVE_MODEL, "https://api.meta.ai/v1")
    assert probed.credential.source == "env:MODEL_API_KEY"
    assert ControlPlane(config, saver=None)._inference() == "live"


def test_live_config_honors_explicit_retention(tmp_path):
    report = passing_report(tmp_path / "compat.json")
    config = live_config(tmp_path / "root", sessions(),
                         **live_kwargs(report, allow_server_retention=True))
    assert config.services.runtime.outbound.allow_server_retention is True


@pytest.mark.parametrize("model", [
    "meta-models/muse-spark-open", "meta-models/muse-spark-open-1026", "gpt-9",
], ids=["baseten-untested", "hackathon-unchecked", "invented"])
def test_live_config_refuses_unqualified_models(tmp_path, model):
    report = passing_report(tmp_path / "compat.json", model=model)
    with pytest.raises(LiveInferenceRefused):
        live_config(tmp_path / "root", sessions(), **live_kwargs(report, model=model))


@pytest.mark.parametrize("kwargs", [
    {"confirm_paid_calls": False},
    {"max_calls": 0},
    {"max_calls": 51},
    {"max_calls": "4"},
    {"environ": {}},
    {"environ": {"MODEL_API_KEY": FAKE_KEY, "GHOSTLAB_META_BASE_URL": "http://x"}},
], ids=["unconfirmed", "zero-cap", "over-ceiling", "non-int-cap", "no-key", "http-base"])
def test_live_config_refuses_bad_gates(tmp_path, kwargs):
    report = passing_report(tmp_path / "compat.json")
    with pytest.raises((LiveInferenceRefused, ProviderConfigError)):
        live_config(tmp_path / "root", sessions(), **live_kwargs(report, **kwargs))


@pytest.mark.parametrize("changes", [
    {"live": False},
    {"transport": "mock"},
    {"model": "other-model"},
    {"endpoint": "https://api.meta.ai.evil/v1"},
    {"checks": [{"check": "auth_minimal", "status": "passed", "detail": "",
                 "attempts": []}]},
], ids=["not-live", "mock", "other-model", "other-endpoint", "missing-checks"])
def test_live_config_refuses_bad_reports(tmp_path, changes):
    report = passing_report(tmp_path / "compat.json", **changes)
    with pytest.raises(LiveInferenceRefused):
        live_config(tmp_path / "root", sessions(), **live_kwargs(report))


def test_live_config_refuses_missing_report(tmp_path):
    with pytest.raises(LiveInferenceRefused):
        live_config(tmp_path / "root", sessions(),
                    **live_kwargs(tmp_path / "absent.json"))


# -- CLI validation (returns before any server starts) --------------------------


def test_cli_refuses_live_with_scripted(capsys):
    assert main(["--live", "--scripted"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--live"],
    ["--live", "--live-model", QUALIFIED_LIVE_MODEL],
    ["--live", "--live-model", QUALIFIED_LIVE_MODEL, "--live-max-calls", "4",
     "--live-compat-report", "r.json"],
], ids=["bare", "model-only", "no-confirm"])
def test_cli_refuses_live_without_every_flag(argv, capsys):
    assert main(argv) == 2
    assert "--live also needs" in capsys.readouterr().err
