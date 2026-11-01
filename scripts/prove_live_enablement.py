"""Live-enablement proof: one refund campaign through the controller live path.

Dry run unless every live gate passes. Live mode needs, together: --live, an
exact --model, a --max-calls cap (1-10), --confirm-paid-calls, a passing live
M01 compatibility report (--compat-report), and the provider key in its
environment variable. The campaign runs in a throwaway root with the same
``live_config`` the ``--live`` server uses, plus a counting transport that
stops past the cap. The report is redacted and written under artifacts/local/
(git-ignored). It carries counts and statuses only, never prompt or reply text.
"""

import argparse
import dataclasses
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from ghostlab.api.live import QUALIFIED_LIVE_MODEL, live_config  # noqa: E402
from ghostlab.api.sessions import LocalDevSessions  # noqa: E402
from ghostlab.contracts import BudgetLimits, CampaignSpec, Principal  # noqa: E402
from ghostlab.orchestration import CampaignController, TrustedSession  # noqa: E402
from ghostlab.orchestration.controller import ledger_key  # noqa: E402
from ghostlab.orchestration.inference import LiveInferenceRefused  # noqa: E402
from ghostlab.providers import ProviderConfigError, redact  # noqa: E402
from ghostlab.storage.checkpoints import graph_checkpointer  # noqa: E402
from ghostlab.storage.database import StoragePaths  # noqa: E402

DEFAULT_REPORT = ROOT / "artifacts" / "local" / "live-enablement-report.json"
PROJECT = "project-live-proof"
PRINCIPAL = Principal(principal_id="live-proof-operator", kind="human")


class _CapExceeded(Exception):
    """Past the transport cap. Not a TransportError, so never retried."""


class _CountingTransport(httpx.BaseTransport):
    """The real network with a hard attempt cap and wire observations."""

    def __init__(self, cap: int):
        self._inner = httpx.HTTPTransport()
        self.cap, self.sent = cap, 0
        self.stores: list = []
        self.body_sizes: list[int] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.sent >= self.cap:
            raise _CapExceeded()
        self.sent += 1
        self.body_sizes.append(len(request.content))
        try:
            self.stores.append(json.loads(request.content).get("store", "<absent>"))
        except ValueError:
            self.stores.append("<unparsable>")
        return self._inner.handle_request(request)


def plan():
    return [{"check": "live_config_gates", "calls": 0},
            {"check": "controller_live_campaign", "calls": "1..cap"},
            {"check": "budgets_reconciled", "calls": 0},
            {"check": "outbound_wire", "calls": 0}]


def run_live(model, max_calls, compat_report, allow_retention, transport=None):
    transport = transport or _CountingTransport(max_calls)
    checks = []
    with tempfile.TemporaryDirectory(prefix="live-proof-") as tmp:
        root = Path(tmp) / "root"
        sessions = LocalDevSessions(PRINCIPAL, PROJECT)
        try:
            config = live_config(
                root, sessions, bind_host="127.0.0.1", model=model,
                max_calls=max_calls, compat_report=compat_report,
                confirm_paid_calls=True,
                allow_server_retention=allow_retention)
        except (LiveInferenceRefused, ProviderConfigError) as error:
            checks.append({"check": "live_config_gates", "status": "failed",
                           "detail": f"{type(error).__name__}: {error}"})
            return checks, transport
        checks.append({"check": "live_config_gates", "status": "passed",
                       "detail": f"model {model}, cap {max_calls}, retention "
                                 f"{'allowed' if allow_retention else 'refused'}"})
        services = dataclasses.replace(
            config.services,
            runtime=dataclasses.replace(config.services.runtime, transport=transport))
        spec = CampaignSpec.model_validate({
            "schema_version": 1,
            "objective": "Find duplicate-refund failures after a payment timeout",
            "success_criteria": ["A failing invariant is reproduced"],
            "world_version": "refund-world/1",
            "budget": BudgetLimits.model_validate({
                "max_model_calls": max_calls, "max_tool_calls": 100,
                "max_input_tokens": 100_000, "max_output_tokens": 20_000,
                "max_cost_minor": None, "cost_currency": None,
                "max_wall_clock_ms": 10 * 60_000,
                "max_parallel_runs": 1}).model_dump(mode="json")})
        paths = StoragePaths(root)
        campaign_id = "c-live-proof"
        try:
            with graph_checkpointer(paths.graph_db, app_db=paths.app_db) as saver:
                controller = CampaignController(
                    TrustedSession(PRINCIPAL, PROJECT), services, saver=saver)
                state = controller.start(campaign_id, spec)
        except _CapExceeded:
            checks.append({"check": "controller_live_campaign", "status": "failed",
                           "detail": f"transport cap tripped at {transport.sent} sent"})
            return checks, transport
        experiments = state.get("experiments", {})
        proposals = sum(1 for e in experiments.values() if e.get("proposal"))
        checks.append({
            "check": "controller_live_campaign",
            "status": "passed" if proposals else "failed",
            "detail": f"status={state['status']} outcome={state.get('outcome')} "
                      f"experiments={len(experiments)} proposals={proposals}"})
        used, _ = services.ledger.committed(ledger_key(PROJECT, campaign_id), "model_calls")
        checks.append({
            "check": "budgets_reconciled",
            "status": "passed" if 0 < transport.sent == used <= max_calls else "failed",
            "detail": f"http_attempts={transport.sent} ledger_model_calls={used} cap={max_calls}"})
        expected = True if allow_retention else False
        checks.append({
            "check": "outbound_wire",
            "status": "passed" if transport.stores and all(
                s is expected for s in transport.stores) else "failed",
            "detail": f"store={transport.stores} max_body_bytes="
                      f"{max(transport.body_sizes, default=0)}"})
    return checks, transport


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GhostLab live-enablement proof")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--confirm-paid-calls", action="store_true")
    parser.add_argument("--compat-report", type=Path)
    parser.add_argument("--allow-server-retention", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    report = {"report_version": 1, "generated_at": datetime.now(UTC).isoformat(),
              "proof": "live-enablement", "transport": "network",
              "not_probed": {"streaming": "Streaming stays disabled.",
                             "journey_schema": "No live journey-schema report exists."}}
    secrets: tuple = ()
    if not args.live:
        report.update(mode="dry_run", live=False, model=args.model, calls_made=0,
                      http_attempts=0, planned=plan(), checks=[],
                      note="No request was sent.")
    else:
        missing = [flag for flag, ok in (
            ("--model", args.model), ("--max-calls", args.max_calls),
            ("--confirm-paid-calls", args.confirm_paid_calls),
            ("--compat-report", args.compat_report)) if not ok]
        if missing:
            parser.error("live mode also needs " + ", ".join(missing))
        if not 1 <= args.max_calls <= 10:
            parser.error("--max-calls must be between 1 and 10")
        if args.model != QUALIFIED_LIVE_MODEL:
            parser.error(f"this proof only runs {QUALIFIED_LIVE_MODEL}")
        import os
        secrets = (os.environ.get("MODEL_API_KEY", ""),)
        transport = _CountingTransport(args.max_calls)
        try:
            checks, transport = run_live(
                args.model, args.max_calls, args.compat_report,
                args.allow_server_retention, transport=transport)
        except Exception as error:  # every live run leaves a sanitized report
            checks = [{"check": "controller_live_campaign", "status": "failed",
                       "detail": f"{type(error).__name__} after {transport.sent} sent"}]
        report.update(mode="live", live=True, model=args.model,
                      retention="allowed" if args.allow_server_retention else "refused",
                      max_calls=args.max_calls, calls_made=transport.sent,
                      http_attempts=transport.sent, checks=checks)
    text = redact(json.dumps(report, indent=2, sort_keys=True), *secrets)
    if secrets and secrets[0]:
        assert secrets[0] not in text  # by construction; never printed
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    failed = any(c["status"] == "failed" for c in report["checks"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
