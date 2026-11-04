"""Benchmark: random vs fixed vs Muse-guided at an equal experiment budget.

    set -a; . ./.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/benchmark.py --budget 8 --random-trials 20 --muse-trials 3

Targets are the deterministic seeded agents (vulnerable, repaired), so only the
adversary's tokens cost money. Every experiment executes through the controller
via plan.execute_plan. The search space is the adversary's catalog:
agent x trigger tool x occurrence, one timeout_after_commit fault.
Raw results go to docs/benchmark.json; docs/benchmark.md is written from them.
"""

import argparse
import itertools
import json
import random
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.experiments import plan  # noqa: E402
from ghostlab.llm import adversary as ADV  # noqa: E402
from ghostlab.llm import agent as A  # noqa: E402
from ghostlab.llm import capsules  # noqa: E402
from ghostlab.llm.client import client_for  # noqa: E402


def cells(cat):
    return list(itertools.product(cat["agent_variants"], cat["trigger_tools"],
                                  cat["occurrences"]))


def run_cell(registry, store, project, cell, index):
    agent, tool, occ = cell
    body = A.requested(agent, ADV.validate_schedule(
        [{"hook": "timeout_after_commit", "trigger_tool": tool, "occurrence": occ}],
        {"hooks": ["timeout_after_commit"], "trigger_tools": [tool], "occurrences": [occ]}),
        name=f"bench-{index}")
    result = plan.execute_plan(store, registry, plan.build_plan(
        "refund-env", A.FIXTURE, agent, "benchmark", A.RULES, None, body["limits"], 1,
        "fixed", [0], requested_configs=[body], policy=A.policy()), project)
    row = result["per_run"][0]
    return row["verdict"] == "fail" and bool(row["rules_failed"])


def scripted_strategy(order, budget):
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = capsules.fresh_env(tmp)
        for index, cell in enumerate(order[:budget], start=1):
            if run_cell(registry, store, "bench", cell, index):
                return {"found": True, "experiments_to_first_failure": index,
                        "experiments_run": index, "usd": 0.0, "first_failure_cell": list(cell)}
    return {"found": False, "experiments_to_first_failure": None,
            "experiments_run": min(budget, len(order)), "usd": 0.0}


def muse_trial(budget, max_usd):
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = capsules.fresh_env(tmp)
        client = client_for("meta-dev", "muse-spark-1.3", reasoning_effort="medium",
                            usd_limit=max_usd, purpose="benchmark")
        explorer = ADV.ModelExplorer(client, store=store, registry=registry,
                                     project_id="bench", max_experiments=budget)
        state = ADV.explore(ADV.new_state(explorer, max_usd=max_usd))
        failing = [i for i, e in enumerate(state["experiments"], start=1)
                   if e["outcome"] == "executed" and e["verdict"] == "fail"
                   and e["rules_failed"]]
        return {"found": bool(failing),
                "experiments_to_first_failure": failing[0] if failing else None,
                "experiments_run": len(state["experiments"]), "usd": client.session_usd,
                "model_calls": len(client.calls), "stop_status": state["stop_status"],
                "local_rejections": len(explorer.rejected), "error": state["error"]}


def stats(trials):
    found = [t for t in trials if t["found"]]
    steps = [t["experiments_to_first_failure"] for t in found]
    return {"trials": len(trials), "discovered": len(found),
            "discovery_rate": len(found) / len(trials) if trials else None,
            "mean_experiments_to_first_failure": sum(steps) / len(steps) if steps else None,
            "max_experiments_to_first_failure": max(steps) if steps else None,
            "usd_total": sum(t["usd"] for t in trials)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--budget", type=int, default=8)
    p.add_argument("--random-trials", type=int, default=20)
    p.add_argument("--muse-trials", type=int, default=3)
    p.add_argument("--max-usd", type=float, default=10.0)
    a = p.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        registry, _ = capsules.fresh_env(tmp)
        cat = ADV.catalog(registry, ADV.DETERMINISTIC)
    space = cells(cat)
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = capsules.fresh_env(tmp)
        failing = [list(c) for i, c in enumerate(space) if run_cell(registry, store, "x", c, i)]
    rand = []
    for seed in range(a.random_trials):
        order = list(space)
        random.Random(seed).shuffle(order)
        rand.append({"seed": seed, **scripted_strategy(order, a.budget)})
    fixed = [scripted_strategy(space, a.budget)]
    # Same grid with the agent order reversed: shows how order-sensitive "fixed" is.
    reversed_space = list(itertools.product(list(reversed(cat["agent_variants"])),
                                            cat["trigger_tools"], cat["occurrences"]))
    fixed_rev = [scripted_strategy(reversed_space, a.budget)]
    per_trial = a.max_usd / max(a.muse_trials, 1)
    muse = []
    for _ in range(a.muse_trials):
        muse.append(muse_trial(a.budget, per_trial))
        print(json.dumps(muse[-1]), flush=True)
    report = {"generated_at": datetime.now(UTC).isoformat(), "budget": a.budget,
              "space_cells": len(space), "failing_cells": failing, "catalog": cat,
              "fixed_order": "catalog order: agent (vulnerable, repaired) x trigger tool"
                             " (*, get_order, get_refund_status, issue_refund) x occurrence 1-4",
              "random": {"stats": stats(rand), "trials": rand},
              "fixed": {"stats": stats(fixed), "trials": fixed},
              "fixed_reversed": {"stats": stats(fixed_rev), "trials": fixed_rev},
              "muse": {"stats": stats(muse), "trials": muse,
                       "model": "muse-spark-1.3", "reasoning_effort": "medium"}}
    (ROOT / "docs" / "benchmark.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: report[k]["stats"] for k in ("random", "fixed", "fixed_reversed", "muse")}, indent=1))
    print("space", len(space), "failing", failing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
