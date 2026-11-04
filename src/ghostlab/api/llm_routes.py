"""Live-model routes: explorations, calibration, capsule replay, model comparison, spend.

Explorations run in a background thread on their own qualified refund env.
Without a key the default mode is keyless replay of a committed recording.
Shapes are documented in docs/api.md. Exploration state is process memory: a
service restart drops it (same as run groups).
"""

import json
import tempfile
from datetime import UTC, datetime
import threading
import uuid
from pathlib import Path

from fastapi import Request

from ..llm import adversary as ADV
from ..llm import agent as llm_agent
from ..llm import capsules
from ..llm.client import (PROVIDERS, ProviderUnavailable, RecordingClient, client_for,
                          provider_status)
from ..llm.spend import CAPS, PRICES, PRICES_FETCHED, SpendLedger

ROOT = Path(__file__).resolve().parents[3]
RECORDED = ROOT / "demo" / "recorded"
DOCS = ROOT / "docs"
LLM_VARIANTS = ("llm-naive", "llm-hardened", "llm-naive-noread", "llm-hardened-noread")


def recorded_names() -> list:
    if not RECORDED.is_dir():
        return []
    return sorted(str(p.relative_to(RECORDED)) for p in RECORDED.rglob("*.json"))


def _load_recorded(name):
    if name not in recorded_names():  # allowlist: never an arbitrary path
        return None
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def _section(path, heading_prefix):
    """Verbatim markdown of one ``## `` section (caveats/notes for the evidence page)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].startswith(heading_prefix)
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip() or None


def _annotate(experiments, turns, rejections, replayed):
    """Attach the adversary's stated hypothesis and proposing model call to each experiment.

    Accepted run_experiment/fork calls map, in order, onto executed experiments;
    calls that were rejected locally (listed in ``rejections``) are skipped.
    """
    pending = [(r["tool"], r["arguments"]) for r in rejections]
    proposals = []
    for record in turns or []:
        turn = record.get("turn") or {}
        for call in turn.get("tool_calls") or []:
            if call.get("name") not in ("run_experiment", "fork"):
                continue
            key = (call.get("name"), call.get("raw"))
            if key in pending:
                pending.remove(key)
                continue
            usage = turn.get("usage") or {}
            proposals.append({"hypothesis": turn.get("text"), "proposed_by": {
                "model": turn.get("model"), "provider": turn.get("provider"),
                "request_id": turn.get("request_id"), "latency_ms": turn.get("latency_ms"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_tokens": usage.get("reasoning_tokens"),
                "usd": usage.get("usd"), "replayed": replayed}})
    for experiment, extra in zip(experiments, proposals):
        experiment.update(extra)
    return experiments


def _branch(run):
    evidence = run.get("evidence") or {}
    snapshot = evidence.get("snapshot") or {}
    return {"run_id": run.get("run_id"), "verdict": run.get("verdict"),
            "rules_failed": run.get("rules_failed"), "checks": run.get("checks"),
            "ledger": snapshot.get("ledger") or [], "order": snapshot.get("order"),
            "total_refunded_minor": snapshot.get("total_refunded_minor"),
            "trace": _trace(evidence.get("trace"))}


def _trace(trace):
    """World events with the ledger total after each one (the stage animates this)."""
    return [{"event_id": e["event_id"], "tool": e["request"]["tool"],
             "arguments": e["request"]["arguments"], "outcome": e["outcome"],
             "ledger_total_after": (e.get("state") or {}).get("total_refunded_minor"),
             "ledger_rows_after": len((e.get("state") or {}).get("ledger") or [])}
            for e in trace or []]


def _claims(state):
    """run_id -> agent claim, matching the agent client's drive-order log to run order."""
    explorer = state["explorer"]
    log = getattr(explorer.llm_client, "run_log", None) if explorer.llm_client else None
    if not log:
        return {}
    agents = {rid: body["agent"] for rid, body in explorer.bodies.items()}
    agents.update({run["run_id"]: name for name, run in state.get("baselines", {}).items()})
    order = sorted(agents, key=lambda rid: int(rid.rsplit("-", 1)[-1]))
    if len(order) != len(log):
        return {}
    out = {}
    for rid, item in zip(order, log):
        if item["agent"] != agents[rid]:
            return {}  # order assumption broken: show no claim rather than a wrong one
        out[rid] = item["claim"]
    return out


def register(app, *, error, service_error, read_json, strict_keys, map_failure):
    explorations: dict = {}
    lock = threading.Lock()
    app.state.explorations = explorations

    def view(entry):
        state = entry.get("state")
        out = {"exploration_id": entry["id"], "mode": entry["mode"],
               "provider": entry["provider"], "status": entry["status"],
               "recording": entry.get("recording"), "error": entry.get("error"),
               "created_at": entry["created_at"]}
        if state is not None:
            s = ADV.summary(state)
            experiments = _annotate(s["experiments"], entry.get("turns"),
                                    s["local_rejections"], entry["mode"] == "replay")
            out.update(stop_status=s["stop_status"], experiments=experiments,
                       budget=s["budget"], model_calls=s["model_calls"],
                       local_rejections=s["local_rejections"],
                       ops_rejections=s["ops_rejections"], failure=s["failure"],
                       final_text=s["final_text"])
            if state.get("error") and not out["error"]:
                out["error"] = state["error"]
        out["recorded_budget"] = entry.get("recorded_budget")
        out["diagnosis"] = entry.get("diagnosis")
        out["reproduced"] = entry.get("reproduced")
        return out

    def run_live(entry):
        try:
            ADV.explore(entry["state"])
            state = entry["state"]
            if entry["diagnose"] and state["failure"] and entry.get("diagnosis") is None:
                wonder = client_for(entry["provider"], "muse-wonder-1.0", usd_limit=0.5,
                                    purpose="diagnosis")
                entry["diagnosis"] = ADV.diagnose(wonder, state)
        except Exception as exc:  # never leak internals
            entry["error"] = f"internal: {type(exc).__name__}"
        with lock:
            entry["status"] = "stopped"

    def run_replay(entry, payload):
        try:
            out = ADV.replay_exploration(payload, entry["dir"])
            entry["state"], entry["diagnosis"] = out["state"], out["diagnosis"]
            entry["reproduced"] = out["reproduced"]
        except Exception as exc:
            entry["error"] = f"replay failed: {type(exc).__name__}: {exc}"[:300]
        with lock:
            entry["status"] = "stopped"

    @app.get("/v1/providers")
    async def providers():
        return {"providers": [provider_status(name) for name in PROVIDERS]}

    @app.post("/v1/explorations")
    async def start_exploration(request: Request):
        try:
            body = await read_json(request, allow_empty=True)
            strict_keys(body, {"mode", "provider", "max_experiments", "max_usd",
                               "llm_targets", "diagnose", "recording"}, "explorations")
            provider = body.get("provider", "meta-dev")
            if provider not in ("meta-dev", "meta-public"):
                raise service_error(400, "invalid_request",
                                    "provider must be meta-dev or meta-public")
            available = provider_status(provider)["status"] == "available"
            mode = body.get("mode") or ("live" if available else "replay")
            if mode not in ("live", "replay"):
                raise service_error(400, "invalid_request", "mode must be live or replay")
            max_experiments = body.get("max_experiments", 12)
            max_usd = body.get("max_usd", 1.0)
            if type(max_experiments) is not int or not 1 <= max_experiments <= 25:
                raise service_error(400, "invalid_request", "max_experiments must be 1-25")
            if not isinstance(max_usd, (int, float)) or not 0 < max_usd <= 3:
                raise service_error(400, "invalid_request", "max_usd must be in (0, 3]")
            llm_targets = body.get("llm_targets", False) is True
            entry = {"id": "exp-" + uuid.uuid4().hex[:12], "mode": mode,
                     "provider": provider if mode == "live" else "replay",
                     "status": "running", "dir": tempfile.mkdtemp(prefix="ghostlab-exp-"),
                     "diagnose": body.get("diagnose", True) is True,
                     "created_at": datetime.now(UTC).isoformat()}
            if mode == "replay":
                name = body.get("recording") or ("adversary-exploration-llm.json"
                                                 if llm_targets else
                                                 "adversary-exploration.json")
                recorded = _load_recorded(name)
                if recorded is None or recorded.get("payload", {}).get("kind") \
                        != ADV.EXPLORATION_KIND:
                    raise service_error(404, "not_found", f"no recorded exploration {name!r}")
                entry["recording"] = name
                entry["turns"] = recorded["payload"].get("adversary_turns")
                # what the recorded live run spent; the replay itself spends 0
                entry["recorded_budget"] = (recorded["payload"].get("summary") or {}).get("budget")
                target, args = run_replay, (entry, recorded["payload"])
            else:
                try:
                    variants = ADV.DETERMINISTIC
                    agent = None
                    if llm_targets:
                        variants, max_experiments = LLM_VARIANTS, min(max_experiments, 6)
                        agent = client_for(provider, "muse-spark-1.3", usd_limit=max_usd,
                                           purpose="adversary-target")
                    adversary = RecordingClient(client_for(
                        provider, "muse-spark-1.3", reasoning_effort="medium",
                        usd_limit=max_usd, purpose="adversary"))
                    entry["turns"] = adversary.records  # hypotheses for the UI feed
                except ProviderUnavailable as exc:
                    raise service_error(503, "provider_unavailable", str(exc)) from None
                registry, store = capsules.fresh_env(entry["dir"])
                explorer = ADV.ModelExplorer(adversary, store=store, registry=registry,
                                             project_id="explore",
                                             max_experiments=max_experiments,
                                             variants=variants, llm_client=agent)
                entry["state"] = ADV.new_state(explorer, max_usd=max_usd)
                target, args = run_live, (entry,)
            with lock:
                explorations[entry["id"]] = entry
            threading.Thread(target=target, args=args, daemon=True).start()
            return {"exploration_id": entry["id"], "mode": mode, "status": "running",
                    "provider": entry["provider"], "recording": entry.get("recording")}
        except Exception as exc:
            return map_failure(exc)

    def _get(exploration_id):
        with lock:
            entry = explorations.get(exploration_id)
        if entry is None:
            raise service_error(404, "not_found", f"unknown exploration {exploration_id!r}")
        return entry

    @app.get("/v1/explorations/{exploration_id}")
    async def get_exploration(exploration_id: str):
        try:
            return view(_get(exploration_id))
        except Exception as exc:
            return map_failure(exc)

    @app.get("/v1/explorations/{exploration_id}/runs/{run_id}")
    async def exploration_run(exploration_id: str, run_id: str):
        """Fork Reality detail: the no-fault baseline and the faulted branch of one run."""
        try:
            entry = _get(exploration_id)
            state = entry.get("state")
            if entry["status"] != "stopped" or state is None:
                raise service_error(409, "not_ready", "exploration is still running")
            explorer = state["explorer"]
            body = explorer.bodies.get(run_id)
            record = explorer._run(run_id) if body is not None else None
            if record is None:
                raise service_error(404, "not_found", f"unknown run {run_id!r}")
            baseline = state.get("baselines", {}).get(body["agent"])
            faulted = _branch(record["run"])
            claim_check = None
            if body["agent"] in LLM_VARIANTS:
                claims = _claims(state)
                snapshot = (record["run"].get("evidence") or {}).get("snapshot")
                if run_id in claims and isinstance(snapshot, dict):
                    claim_check = llm_agent.claimed_vs_actual(claims[run_id], snapshot)
            diff = next((e.get("reality_diff") for e in state["experiments"]
                         if e["run_id"] == run_id), None) or {}
            first = diff.get("first_divergence") or {}
            return {"run_id": run_id, "agent": body["agent"], "faults": body["faults"],
                    "baseline": _branch(baseline) if baseline else None, "faulted": faulted,
                    "first_divergence_event_id": first.get("event_id"),
                    "violated_invariants": diff.get("violated_invariants", []),
                    "claim_check": claim_check}
        except Exception as exc:
            return map_failure(exc)

    @app.get("/v1/evidence")
    async def evidence():
        """Read-only evidence page: calibration + benchmark tables with their caveats."""
        calib = _load_recorded("calibration.json")
        try:
            bench = json.loads((DOCS / "benchmark.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bench = None
        return {
            "calibration": None if calib is None else {
                k: calib.get(k) for k in ("generated_at", "model", "provider", "fault",
                                          "usd_spent", "model_calls", "table")},
            "calibration_notes": _section(DOCS / "calibration.md", "What the runs show"),
            "benchmark": None if bench is None else {
                "generated_at": bench.get("generated_at"), "budget": bench.get("budget"),
                "space_cells": bench.get("space_cells"),
                "failing_cells": bench.get("failing_cells"),
                "strategies": {k: v["stats"] for k, v in bench.items()
                               if isinstance(v, dict) and isinstance(v.get("stats"), dict)}},
            "benchmark_caveats": _section(DOCS / "benchmark.md", "How to read this")}

    @app.post("/v1/explorations/{exploration_id}/resume")
    async def resume_exploration(exploration_id: str):
        try:
            entry = _get(exploration_id)
            with lock:
                state = entry.get("state")
                if entry["status"] != "stopped" or state is None \
                        or state["stop_status"] != "paused_on_error":
                    raise service_error(409, "not_resumable",
                                        "only a stopped exploration paused_on_error resumes")
                entry["status"] = "running"
            threading.Thread(target=run_live, args=(entry,), daemon=True).start()
            return {"exploration_id": exploration_id, "status": "running"}
        except Exception as exc:
            return map_failure(exc)

    @app.get("/v1/calibration")
    async def calibration():
        data = _load_recorded("calibration.json")
        if data is None:
            return error(404, "not_found", "no calibration recorded")
        return {k: data[k] for k in ("generated_at", "model", "provider", "fault",
                                     "usd_spent", "model_calls", "table", "runs")}

    @app.get("/v1/capsules")
    async def list_capsules():
        return {"capsules": recorded_names()}

    @app.post("/v1/capsules/replay")
    async def replay_capsule(request: Request):
        try:
            body = await read_json(request)
            strict_keys(body, {"name"}, "replay")
            name = body.get("name")
            recorded = _load_recorded(name) if isinstance(name, str) else None
            if recorded is None:
                raise service_error(404, "not_found", "unknown recorded capsule")
            kind = recorded.get("payload", {}).get("kind")
            if kind == capsules.CAPSULE_KIND:
                out = capsules.replay(RECORDED / name)
                # replayed evidence matched the recording, so the recorded trace is the replay's
                if (out.get("matches") or {}).get("evidence"):
                    expected = recorded["payload"].get("expected") or {}
                    out["trace"] = _trace((expected.get("evidence") or {}).get("trace"))
                    out["agent"] = recorded["payload"]["body"]["agent"]
            elif kind == ADV.EXPLORATION_KIND:
                with tempfile.TemporaryDirectory() as tmp:
                    out = ADV.replay_exploration(recorded["payload"], tmp)
                state = out.pop("state")
                out["summary"] = ADV.summary(state)
            else:
                raise service_error(400, "invalid_request", "not a replayable capsule")
            return {"name": name, "kind": kind, **out}
        except Exception as exc:
            return map_failure(exc)

    @app.get("/v1/models/compare")
    async def compare_models():
        calib = _load_recorded("calibration.json")
        glimmer = _load_recorded("glimmer-run.json")
        spark_runs = [r for r in (calib or {}).get("runs", []) if r["variant"] == "llm-naive"]
        spark_calls = [c for c in (calib or {}).get("calls", [])]
        return {"task": "llm-naive under timeout-after-commit",
                "spark": {"model": "muse-spark-1.3", "provider": "meta-dev",
                          "runs": spark_runs,
                          "median_latency_ms": _median([c["latency_ms"] for c in spark_calls]),
                          "usd_per_call": (sum(c["usd"] for c in spark_calls) / len(spark_calls)
                                           if spark_calls else None)},
                "glimmer": {"status": provider_status("glimmer-local"),
                            "run": glimmer, "usd_per_call": 0.0}}

    @app.get("/v1/spend")
    async def spend():
        return {"totals": SpendLedger().totals(), "caps_usd": CAPS,
                "prices_usd_per_mtok": PRICES, "prices_fetched": PRICES_FETCHED}


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[len(values) // 2]
