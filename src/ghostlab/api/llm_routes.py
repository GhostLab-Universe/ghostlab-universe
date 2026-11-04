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
from ..llm import capsules
from ..llm.client import (PROVIDERS, ProviderUnavailable, RecordingClient, client_for,
                          provider_status)
from ..llm.spend import CAPS, PRICES, PRICES_FETCHED, SpendLedger

RECORDED = Path(__file__).resolve().parents[3] / "demo" / "recorded"
LLM_VARIANTS = ("llm-naive", "llm-hardened", "llm-naive-noread", "llm-hardened-noread")


def recorded_names() -> list:
    if not RECORDED.is_dir():
        return []
    return sorted(str(p.relative_to(RECORDED)) for p in RECORDED.rglob("*.json"))


def _load_recorded(name):
    if name not in recorded_names():  # allowlist: never an arbitrary path
        return None
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


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
            out.update(stop_status=s["stop_status"], experiments=s["experiments"],
                       budget=s["budget"], model_calls=s["model_calls"],
                       local_rejections=s["local_rejections"],
                       ops_rejections=s["ops_rejections"], failure=s["failure"],
                       final_text=s["final_text"])
            if state.get("error") and not out["error"]:
                out["error"] = state["error"]
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
                target, args = run_replay, (entry, recorded["payload"])
            else:
                try:
                    variants = ADV.DETERMINISTIC
                    agent = None
                    if llm_targets:
                        variants, max_experiments = LLM_VARIANTS, min(max_experiments, 6)
                        agent = client_for(provider, "muse-spark-1.3", usd_limit=max_usd,
                                           purpose="adversary-target")
                    adversary = client_for(provider, "muse-spark-1.3",
                                           reasoning_effort="medium", usd_limit=max_usd,
                                           purpose="adversary")
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
