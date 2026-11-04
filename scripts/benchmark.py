"""Benchmark: random vs fixed vs Muse-guided at an equal experiment budget (fair version).

    set -a; . ./.env; set +a
    PYTHONPATH=src:. .venv/bin/python scripts/benchmark.py --budget 10 --random-trials 30 --muse-trials 5

Two environments (refund-env, booking-env), each with deterministic agents that carry
seeded bugs. Agents are shown to every strategy only as opaque ids (agent-01, ...),
assigned by a seeded shuffle; the mapping is stored in the raw JSON. Muse gets a
neutral system prompt (adversary.NEUTRAL_SYSTEM) that names no fault and no timing.
Space per env = agents x trigger tool (* + every discovered tool) x occurrence 1-4,
one fault per schedule (the worlds fire timeout_after_commit at most once per run, so
a second fault adds nothing; max 1 keeps every strategy on the same space).
Ground truth comes from running every cell. Raw results go to docs/benchmark.json.
"""

import argparse
import itertools
import json
import random
import sys
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from examples.equipment_booking import driver_booking  # noqa: E402
from examples.equipment_booking import e2e_proof as BP  # noqa: E402
from ghostlab.envs.qualification import run_qualification  # noqa: E402
from ghostlab.envs.registry import EnvRegistry  # noqa: E402
from ghostlab.experiments import plan  # noqa: E402
from ghostlab.experiments.store import ExperimentStore  # noqa: E402
from ghostlab.llm import adversary as ADV  # noqa: E402
from ghostlab.llm import agent as A  # noqa: E402
from ghostlab.llm import capsules  # noqa: E402
from ghostlab.llm.client import client_for  # noqa: E402

ID_SEED = 2026
REFUND_AGENTS = ["vulnerable", "repaired", "status-ignored", "retry-nokey",
                 "split-key-per-attempt"]
BOOKING_AGENTS = list(driver_booking.AGENTS)


def booking_requested(variant, faults, name=None):
    return {"config_version": 1, "name": name or f"{variant} run", "env_id": BP.ENV_ID,
            "agent": variant, "fixture": dict(BP.BOOKING_FIXTURE),
            "faults": [dict(f) for f in faults], "rules": list(BP.BOOKING_RULES),
            "limits": dict(BP.BOOKING_LIMITS), "repetitions": {"count": 1}}


def booking_diff(baseline_run, faulted_run):
    """Booking counterpart of adversary.compact_diff (first divergence + counts)."""
    def trace(run):
        return (run.get("evidence") or {}).get("trace") or []

    def total(run):
        return ((run.get("evidence") or {}).get("snapshot") or {}).get("total_bookings")
    left, right = trace(baseline_run), trace(faulted_run)
    first = None
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a != b:
            side = (lambda e: None if not e else {"tool": e["request"]["tool"],
                                                  "outcome": e["outcome"]})
            first = {"event_id": index + 1, "baseline": side(a), "faulted": side(b)}
            break
    return {"first_divergence": first, "baseline_total_bookings": total(baseline_run),
            "faulted_total_bookings": total(faulted_run),
            "violated_invariants": [k for k, v in (faulted_run.get("checks") or {}).items()
                                    if v is False]}


def booking_profile():
    return {"env_id": BP.ENV_ID, "fixture": BP.BOOKING_FIXTURE, "rules": BP.BOOKING_RULES,
            "requested": booking_requested, "policy": A.policy,
            "adapter": BP.BookingReplayAdapter, "violated": BP.booking_prefix_violated,
            "diff": booking_diff, "plan_agent": "vulnerable"}


def fresh(env_id, tmp):
    if env_id == "refund-env":
        return capsules.fresh_env(tmp)
    registry = EnvRegistry(Path(tmp) / "env.sqlite", allowed_adapter_prefixes=BP.ALLOWLIST)
    registry.register(env_id=BP.ENV_ID, env_version=BP.ENV_VERSION, adapter_ref=BP.BOOKING_REF,
                      display_name="Synthetic equipment booking",
                      declared_tools=deepcopy(BP.BOOKING_TOOLS),
                      declared_modes=list(BP.BOOKING_MODES), registered_by="benchmark")
    BP._register_driver_and_checks()
    run_qualification(registry, BP.ENV_ID, fixture=dict(BP.BOOKING_FIXTURE))
    return registry, ExperimentStore(Path(tmp) / "experiments.sqlite")


ENVS = {"refund-env": (REFUND_AGENTS, ADV.refund_profile),
        "booking-env": (BOOKING_AGENTS, booking_profile)}


def opaque_ids():
    """Global opaque ids: shuffled per env with a fixed seed, numbered across envs."""
    out, n = {}, 0
    for env_id, (agents, _) in ENVS.items():
        order = list(agents)
        random.Random(f"{ID_SEED}-{env_id}").shuffle(order)
        out[env_id] = {}
        for real in order:
            n += 1
            out[env_id][f"agent-{n:02d}"] = real
    return out


def run_cell(profile, registry, store, project, aliases, cell, index):
    opaque, tool, occ = cell
    cat = {"hooks": ["timeout_after_commit"], "trigger_tools": [tool], "occurrences": [occ]}
    faults = ADV.validate_schedule(
        [{"hook": "timeout_after_commit", "trigger_tool": tool, "occurrence": occ}], cat)
    agent = aliases[opaque]
    body = profile["requested"](agent, faults, name=f"bench-{index}")
    result = plan.execute_plan(store, registry, plan.build_plan(
        profile["env_id"], profile["fixture"], agent, "benchmark", profile["rules"], None,
        body["limits"], 1, "fixed", [0], requested_configs=[body], policy=profile["policy"]()),
        project)
    row = result["per_run"][0]
    return row["verdict"] == "fail" and bool(row["rules_failed"])


def scripted(env_id, profile, aliases, order, budget):
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = fresh(env_id, tmp)
        for index, cell in enumerate(order[:budget], start=1):
            if run_cell(profile, registry, store, "bench", aliases, cell, index):
                return {"found": True, "experiments_to_first_failure": index,
                        "experiments_run": index, "usd": 0.0, "first_failure_cell": list(cell)}
    return {"found": False, "experiments_to_first_failure": None,
            "experiments_run": min(budget, len(order)), "usd": 0.0}


def muse_trial(env_id, profile, aliases, budget, max_usd):
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = fresh(env_id, tmp)
        client = client_for("meta-dev", "muse-spark-1.3", reasoning_effort="medium",
                            usd_limit=max_usd, purpose="benchmark-2b")
        explorer = ADV.ModelExplorer(client, store=store, registry=registry,
                                     project_id="bench", max_experiments=budget,
                                     system=ADV.NEUTRAL_SYSTEM, aliases=aliases, env=profile,
                                     max_faults=1)
        state = ADV.explore(ADV.new_state(explorer, max_usd=max_usd))
        cells = []
        for e in state["experiments"]:
            trig = [(f["trigger"]["tool"], f["trigger"]["occurrence"]) for f in e["body"]["faults"]]
            cells.append({"agent": e["agent_variant"], "real_agent": e["body"]["agent"],
                          "triggers": trig, "outcome": e["outcome"], "verdict": e["verdict"],
                          "rules_failed": e["rules_failed"]})
        failing = [i for i, e in enumerate(state["experiments"], start=1)
                   if e["outcome"] == "executed" and e["verdict"] == "fail"
                   and e["rules_failed"]]
        return {"found": bool(failing),
                "experiments_to_first_failure": failing[0] if failing else None,
                "experiments_run": len(state["experiments"]), "usd": client.session_usd,
                "model_calls": len(client.calls), "stop_status": state["stop_status"],
                "local_rejections": explorer.rejected, "error": state["error"],
                "cells": cells, "final_text": explorer.final_text}


def stats(trials):
    found = [t for t in trials if t["found"]]
    steps = [t["experiments_to_first_failure"] for t in found]
    return {"trials": len(trials), "discovered": len(found),
            "mean_experiments_to_first_failure": sum(steps) / len(steps) if steps else None,
            "max_experiments_to_first_failure": max(steps) if steps else None,
            "usd_total": sum(t["usd"] for t in trials)}


def random_expectation(n, f, budget):
    """Exact P(random order without replacement hits a failing cell within budget)."""
    return 1 - comb(n - f, budget) / comb(n, budget) if budget <= n else 1.0


def bench_env(env_id, aliases, args):
    profile = ENVS[env_id][1]()
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = fresh(env_id, tmp)
        cat = ADV.catalog(registry, list(aliases), env_id, max_faults=1)
        space = list(itertools.product(sorted(aliases), cat["trigger_tools"], cat["occurrences"]))
        failing = [list(c) for i, c in enumerate(space)
                   if run_cell(profile, registry, store, "truth", aliases, c, i)]
    rand = []
    for seed in range(args.random_trials):
        order = list(space)
        random.Random(seed).shuffle(order)
        rand.append({"seed": seed, **scripted(env_id, profile, aliases, order, args.budget)})
    fixed = scripted(env_id, profile, aliases, space, args.budget)
    reversed_space = list(itertools.product(sorted(aliases, reverse=True), cat["trigger_tools"],
                                            cat["occurrences"]))
    fixed_rev = scripted(env_id, profile, aliases, reversed_space, args.budget)
    muse = []
    for _ in range(args.muse_trials):
        muse.append(muse_trial(env_id, profile, aliases, args.budget, args.muse_usd))
        print(json.dumps({k: muse[-1][k] for k in ("found", "experiments_to_first_failure",
                                                   "experiments_run", "usd", "stop_status",
                                                   "error")}), flush=True)
    return {"catalog": cat, "space_cells": len(space), "failing_cells": failing,
            "random_expected_discovery": random_expectation(len(space), len(failing),
                                                            args.budget),
            "random": {"stats": stats(rand), "trials": rand},
            "fixed": {"stats": stats([fixed]), "trials": [fixed]},
            "fixed_reversed": {"stats": stats([fixed_rev]), "trials": [fixed_rev]},
            "muse": {"stats": stats(muse), "trials": muse, "model": "muse-spark-1.3",
                     "reasoning_effort": "medium", "system_prompt": "NEUTRAL_SYSTEM"}}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--budget", type=int, default=10)
    p.add_argument("--random-trials", type=int, default=30)
    p.add_argument("--muse-trials", type=int, default=5)
    p.add_argument("--muse-usd", type=float, default=0.6, help="USD cap per Muse trial")
    p.add_argument("--out", default=str(ROOT / "docs" / "benchmark.json"))
    args = p.parse_args()
    ids = opaque_ids()
    report = {"generated_at": datetime.now(UTC).isoformat(), "budget": args.budget,
              "id_seed": ID_SEED, "opaque_ids": ids, "envs": {}}
    for env_id in ENVS:
        report["envs"][env_id] = bench_env(env_id, ids[env_id], args)
        env = report["envs"][env_id]
        print(env_id, json.dumps({k: env[k]["stats"] for k in
                                  ("random", "fixed", "fixed_reversed", "muse")}), flush=True)
    Path(args.out).write_text(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
