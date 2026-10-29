"""Redaction, local-dev boundary, isolation, budget ceilings and execution gate."""

import io
import logging

import pytest

from ghostlab.contracts import BudgetLimits, Principal
from ghostlab.policy import (REQUIRED_ISOLATION_CHECKS, ROLE_BUDGET_CEILINGS,
                             BackendQualification, authorize_execution, ceiling_for,
                             check_within_ceiling, delegate, root_scope)
from ghostlab.security import (REDACTED, IsolationViolation, LocalDevRefused, RedactingFilter,
                               Redactor, is_production_authenticated, require_local_dev,
                               require_same_project, visible)

FAKE_META_KEY = "fake-meta-key-0123456789abcdef"  # test value, never a real credential
FAKE_BASETEN_KEY = "fake-baseten-key-9876543210"
DIGEST = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
REDACTOR = Redactor.from_env(environ={"MODEL_API_KEY": FAKE_META_KEY,
                                      "BASETEN_API_KEY": FAKE_BASETEN_KEY})


@pytest.mark.parametrize("text", [
    f"calling provider with key {FAKE_META_KEY}",
    f"baseten={FAKE_BASETEN_KEY} failed",
    "Authorization: Bearer abc.def.ghi-1234567890",
    "header basic dXNlcjpwYXNzd29yZA==",
    "token=supersecretvalue",
    'api_key: "another-secret-value"',
    "sk-abcdefghijklmnopqrstuvwx leaked",
])
def test_secrets_are_redacted(text):
    out = REDACTOR.text(text)
    assert REDACTED in out
    for secret in (FAKE_META_KEY, FAKE_BASETEN_KEY, "abc.def.ghi-1234567890", "supersecretvalue",
                   "another-secret-value", "sk-abcdefghijklmnopqrstuvwx", "dXNlcjpwYXNzd29yZA=="):
        assert secret not in out


def test_evidence_digests_and_ordinary_text_survive():
    text = f"capsule sha256 {DIGEST} refunded 250000 minor units"
    assert REDACTOR.text(text) == text


def test_structured_values_redact_secret_keys_and_nested_strings():
    value = {"headers": {"Authorization": "Bearer xyz123456789"}, "MODEL_API_KEY": "x",
             "notes": [f"key {FAKE_META_KEY}"], "amount_minor": 250000}
    out = REDACTOR.value(value)
    assert out["headers"]["Authorization"] == REDACTED and out["MODEL_API_KEY"] == REDACTED
    assert FAKE_META_KEY not in out["notes"][0] and out["amount_minor"] == 250000


def test_usage_and_evidence_keys_are_not_mistaken_for_secrets():
    usage = {"input_tokens": 1200, "output_tokens": 80, "max_output_tokens": 5000,
             "access_token": "opaque-value-123", "session_secret": "s3cr3t-value"}
    out = REDACTOR.value(usage)
    assert out["input_tokens"] == 1200 and out["max_output_tokens"] == 5000
    assert out["access_token"] == REDACTED and out["session_secret"] == REDACTED
    assert REDACTOR.text("max_output_tokens=5000") == "max_output_tokens=5000"


def test_short_or_empty_env_values_are_not_treated_as_secrets():
    redactor = Redactor.from_env(environ={"MODEL_API_KEY": "", "BASETEN_API_KEY": "abc"})
    assert redactor.text("abc order") == "abc order"


def test_logs_are_redacted_including_args_and_exceptions():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter(REDACTOR))
    logger = logging.getLogger("ghostlab.test.m06")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("request failed for key %s", FAKE_META_KEY)
        try:
            raise RuntimeError(f"Authorization: Bearer {FAKE_BASETEN_KEY}")
        except RuntimeError:
            logger.exception("provider error")
    finally:
        logger.removeHandler(handler)
        handler.close()
    output = stream.getvalue()
    assert FAKE_META_KEY not in output and FAKE_BASETEN_KEY not in output
    assert output.count(REDACTED) >= 2


LOCAL = {"GHOSTLAB_AUTH_MODE": "local_dev"}


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost", "127.0.0.2"])
def test_local_dev_allowed_on_loopback_with_opt_in(host):
    assert require_local_dev(host, LOCAL).kind == "local_dev"


@pytest.mark.parametrize("host,env", [
    ("0.0.0.0", LOCAL), ("192.168.1.10", LOCAL), ("ghostlab.example", LOCAL), ("::", LOCAL),
    ("127.0.0.1", {}), ("127.0.0.1", {"GHOSTLAB_AUTH_MODE": "production"}),
    ("127.0.0.1", {**LOCAL, "GHOSTLAB_DEPLOYMENT": "hosted"}),
])
def test_local_dev_refused_off_loopback_without_opt_in_or_when_hosted(host, env):
    with pytest.raises(LocalDevRefused):
        require_local_dev(host, env)


def test_local_dev_is_never_production_authentication():
    principal = require_local_dev("127.0.0.1", LOCAL)
    assert principal.kind == "local_dev" and not is_production_authenticated(principal)
    human = Principal.model_validate({"principal_id": "reviewer-1", "kind": "human"})
    assert not is_production_authenticated(human)  # no production auth exists yet


def test_isolation_confines_to_scope_project():
    owner = Principal.model_validate({"principal_id": "u", "kind": "human"})
    scope = delegate(root_scope("project-a", owner), "analyst", set())
    require_same_project(scope, "project-a")
    with pytest.raises(IsolationViolation):
        require_same_project(scope, "project-b")
    rows = [{"p": "project-a", "v": 1}, {"p": "project-b", "v": 2}]
    assert visible(scope, rows, lambda row: row["p"]) == [rows[0]]


def _limits(**overrides):
    base = ceiling_for("explorer").model_dump()
    return BudgetLimits.model_validate({**base, **overrides})


def test_budget_ceilings_per_role():
    assert check_within_ceiling("explorer", ceiling_for("explorer")).allowed
    assert not check_within_ceiling("explorer", _limits(max_model_calls=6)).allowed
    assert not check_within_ceiling("explorer", _limits(max_tool_calls=1)).allowed
    assert check_within_ceiling("explorer", _limits(max_cost_minor=100,
                                                    cost_currency="INR")).allowed
    assert not check_within_ceiling("unknown-role", _limits()).allowed
    verifier = ceiling_for("verifier")
    assert verifier.max_model_calls == 0 and verifier.max_tool_calls == 0
    assert all(limit.max_cost_minor is None for limit in ROLE_BUDGET_CEILINGS.values())
    with pytest.raises(TypeError):
        ROLE_BUDGET_CEILINGS["explorer"] = ceiling_for("supervisor")


def test_worker_ceilings_never_exceed_supervisor():
    supervisor = ceiling_for("supervisor")
    for role, limits in ROLE_BUDGET_CEILINGS.items():
        assert check_within_ceiling("supervisor", limits).allowed, role
        assert limits.max_model_calls <= supervisor.max_model_calls


def test_trusted_checked_in_code_runs_locally():
    assert authorize_execution("checked_in", "trusted_local").allowed


@pytest.mark.parametrize("origin", ["uploaded", "model_generated"])
def test_unknown_code_is_disabled_by_default(origin):
    assert not authorize_execution(origin, "trusted_local", unknown_code_enabled=True).allowed
    assert not authorize_execution(origin, "isolated").allowed
    partial = BackendQualification("candidate", {"network_denied": "report-1"})
    decision = authorize_execution(origin, "isolated", qualification=partial,
                                   unknown_code_enabled=True)
    assert not decision.allowed and "filesystem_confined" in decision.reason


def test_unknown_code_requires_every_isolation_check():
    full = BackendQualification("candidate",
                                {check: f"evidence-{check}" for check in REQUIRED_ISOLATION_CHECKS})
    assert authorize_execution("uploaded", "isolated", qualification=full,
                               unknown_code_enabled=True).allowed
    assert not authorize_execution("uploaded", "isolated", qualification=full).allowed
