"""Opt-in provider capability probe (M01). Dry run unless every live gate is passed.

    python scripts/probe_provider.py --provider meta_responses
    python scripts/probe_provider.py --provider meta_responses --live \
        --model <exact enabled model> --max-calls 4 --confirm-paid-calls

Live mode needs, together: --live, an exact --model, a --max-calls cap (1-10),
--confirm-paid-calls, and the provider key in its environment variable
(MODEL_API_KEY or BASETEN_API_KEY). --max-calls caps every outbound HTTP attempt,
retries included, because each attempt may be billed; the report shows model
calls and HTTP attempts separately. Each call is capped at 512 output tokens.
Tool results sent back are labeled synthetic fixtures; no tool is dispatched.
The report is redacted and written under artifacts/local/ (git-ignored).
"""

import argparse
import dataclasses
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from ghostlab.contracts import ContextBundle, ModelRequest, ToolDefinition  # noqa: E402
from ghostlab.providers import (PROVIDER_INTERFACE_VERSION, ProviderConfig,  # noqa: E402
                                ProviderConfigError, Runtime, adapter_for, redact)
from ghostlab.providers.base import ENV  # noqa: E402

DEFAULT_REPORT = ROOT / "artifacts" / "local" / "compatibility-report.json"
CALLS_NEEDED = {"auth_minimal": 1, "tool_roundtrip": 2, "structured_proposal": 1}
NOT_PROBED = {
    "invalid_structured_rejected": "Local validation is covered by offline fixture tests; "
                                   "a live model cannot be made to fail on demand.",
    "streaming_partial_arguments": "Streaming stays disabled until its own gate is built.",
    "timeout_cancel_rate_limit": "Covered offline; not provoked against a paid endpoint.",
}


def _example(name):
    return json.loads((ROOT / "examples" / "contracts" / f"{name}.json").read_text("utf-8"))


def _bundle(objective, tools):
    data = _example("context_bundle.explorer")
    data["objective"] = objective
    data["scope"]["allowed_tools"] = tools
    data["items"] = [{**data["items"][0], "content": objective}]
    return ContextBundle.model_validate(data)


def _request(config, request_id, *, tools=(), schema=None, continuation=None, results=()):
    data = _example("model_request.explorer")
    data.update(request_id=request_id, provider=config.provider, model=config.model,
                tools=list(tools), output_schema=schema, continuation=continuation,
                tool_results=list(results), max_output_tokens=512, timeout_ms=60_000)
    return ModelRequest.model_validate(data)


def plan():
    return [{"check": name, "calls": calls} for name, calls in CALLS_NEEDED.items()]


class AttemptCap:
    """Counts every outbound HTTP attempt (retries included) against --max-calls."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def __call__(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def run_live(config, runtime, max_calls):
    cap = AttemptCap(max_calls)
    adapter = adapter_for(config, dataclasses.replace(runtime, attempt_gate=cap))
    status_tool = ToolDefinition.model_validate({
        **_example("tool_definition.issue_refund"), "name": "get_refund_status",
        "description": "Look up a synthetic refund by idempotency key",
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["order_id", "customer_id", "idempotency_key"],
                       "properties": {k: {"type": "string"} for k in
                                      ("order_id", "customer_id", "idempotency_key")}}})
    checks, calls, replies = [], 0, []

    def call(req, bundle):
        nonlocal calls
        calls += 1
        reply = adapter.complete(req, bundle)
        replies.append(reply)
        return reply

    def record(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail,
                       "attempts": [a.__dict__ for a in adapter.last_attempts]})

    for name, needed in CALLS_NEEDED.items():
        if cap.used + needed > max_calls:
            record(name, "skipped_call_cap",
                   f"needs at least {needed} HTTP attempt(s); {cap.used} of {max_calls} used")
            continue
        if name == "auth_minimal":
            reply = call(_request(config, "probe-auth"),
                         _bundle("Reply with the single word: ready.", []))
            ok = reply.status in ("completed", "incomplete")
            record(name, "passed" if ok else "failed",
                   reply.error.message if reply.error else f"status={reply.status}")
        elif name == "tool_roundtrip":
            bundle = _bundle("Call get_refund_status for order synthetic-order-1, customer "
                             "synthetic-customer-1, key refund-intent-1, then summarize.",
                             ["get_refund_status"])
            first = call(_request(config, "probe-tool-1", tools=[status_tool]), bundle)
            if first.status != "tool_calls":
                record(name, "failed", first.error.message if first.error
                       else f"expected tool_calls, got {first.status}")
                continue
            results = [{"provider_call_id": c.provider_call_id, "is_error": False,
                        "output_json": json.dumps({"synthetic_fixture": True,
                                                   "status": "committed"})}
                       for c in first.tool_calls]
            second = call(_request(config, "probe-tool-2", tools=[status_tool],
                                   continuation=first.continuation, results=results), bundle)
            ok = second.status in ("completed", "incomplete")
            record(name, "passed" if ok else "failed",
                   "tool call proposed and continuation accepted" if ok
                   else (second.error.message if second.error else second.status))
            record("continuation_after_tool_result", "passed" if ok else "failed",
                   "previous turn resumed with paired results" if ok else "see tool_roundtrip")
        elif name == "structured_proposal":
            reply = call(_request(config, "probe-structured",
                                  schema={"name": "experiment_proposal", "version": 1}),
                         _bundle("Propose one refund-world experiment.", []))
            ok = reply.status == "completed" and reply.structured_output is not None
            record(name, "passed" if ok else "failed",
                   "validated against experiment_proposal/1" if ok
                   else (reply.error.message if reply.error else reply.status))
    reported = [r for r in replies if r.usage.input_tokens is not None
                or r.usage.output_tokens is not None]
    checks.append({"check": "usage_capture",
                   "status": "passed" if reported else ("failed" if replies else "not_run"),
                   "detail": f"{len(reported)}/{len(replies)} replies reported token usage",
                   "attempts": []})
    return checks, calls, cap.used


def main(argv=None, runtime=None) -> int:
    parser = argparse.ArgumentParser(description="GhostLab provider capability probe")
    parser.add_argument("--provider", choices=sorted(ENV), required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--confirm-paid-calls", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    report = {
        "report_version": 1, "generated_at": datetime.now(UTC).isoformat(),
        "interface": PROVIDER_INTERFACE_VERSION, "provider": args.provider,
        "python": platform.python_version(), "httpx": httpx.__version__,
        "transport": "network" if runtime is None else "mock",
        "not_probed": NOT_PROBED,
    }
    secrets = ()
    if not args.live:
        report.update(mode="dry_run", live=False, model=args.model, calls_made=0,
                      http_attempts=0,
                      planned=plan(), checks=[], note="No request was sent.")
    else:
        missing = [flag for flag, ok in (("--model", args.model), ("--max-calls", args.max_calls),
                                         ("--confirm-paid-calls", args.confirm_paid_calls))
                   if not ok]
        if missing:
            parser.error("live mode also needs " + ", ".join(missing))
        if not 1 <= args.max_calls <= 10:
            parser.error("--max-calls must be between 1 and 10")
        key_var, _, _, default_url = ENV[args.provider]
        try:
            from ghostlab.providers import Credential
            credential = Credential.from_env(key_var)
            config = ProviderConfig(provider=args.provider, model=args.model,
                                    credential=credential,
                                    base_url=args.base_url or default_url)
        except ProviderConfigError as error:
            parser.error(str(error))
        secrets = (credential.reveal(),)
        checks, calls, attempts = run_live(config, runtime or Runtime(), args.max_calls)
        report.update(mode="live", live=runtime is None, model=args.model,
                      endpoint=config.base_url, max_calls=args.max_calls, calls_made=calls,
                      http_attempts=attempts, checks=checks)
    text = redact(json.dumps(report, indent=2, sort_keys=True), *secrets)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    failed = any(c["status"] == "failed" for c in report["checks"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
