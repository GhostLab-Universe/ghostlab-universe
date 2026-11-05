"""Recorded LLM-run capsules and keyless replay.

A capsule holds the requested config, the recorded model turns (with the digest
of the input each turn answered) and the expected outcome. Replay re-executes the
run through the same controller path with a ``ReplayClient``: zero network, zero
keys. It is reproduced only when evidence, verdict and the claimed-vs-actual
result all match. Integrity uses the existing capsule sha256 envelope.

    python -m ghostlab.llm.capsules demo/recorded/*.json
"""

import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from ..capsule import _digest
from ..envs.adapters import REFUND_MODES, REFUND_TOOLS
from ..envs.qualification import run_qualification
from ..envs.registry import EnvRegistry
from ..experiments.store import ExperimentStore
from . import agent as llm_agent
from .client import ReplayClient, ReplayDiverged

CAPSULE_KIND = "ghostlab-llm-run/1"
REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
PROJECT = "replay"


def fresh_env(directory):
    """Temp registry + store with refund-env registered and qualified."""
    registry = EnvRegistry(Path(directory) / "env.sqlite", allowed_adapter_prefixes=("ghostlab.",))
    registry.register(env_id="refund-env", env_version="refund-env/1", adapter_ref=REFUND_REF,
                      display_name="Synthetic refund", declared_tools=deepcopy(REFUND_TOOLS),
                      declared_modes=list(REFUND_MODES))
    run_qualification(registry, "refund-env")
    return registry, ExperimentStore(Path(directory) / "experiments.sqlite")


def _expected(summary, record):
    return {"evidence": record["run"]["evidence"], "verdict": summary["verdict"],
            "claim_check": summary["claim_check"], "status": summary["status"]}


def build(result, records, *, provider, model, label) -> dict:
    payload = {"kind": CAPSULE_KIND, "label": label, "provider": provider, "model": model,
               "prompt_version": llm_agent.PROMPT_VERSION, "body": result["body"],
               "turns": records, "summary": result["summary"],
               "expected": _expected(result["summary"], result["record"])}
    return {"payload": payload, "sha256": _digest(payload)}


def write(path, capsule, *, overwrite=True):
    """``overwrite=False`` refuses an existing file (a batch must never clobber evidence)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w" if overwrite else "x", encoding="utf-8") as handle:
        handle.write(json.dumps(capsule, indent=1) + "\n")


def load(path) -> dict:
    capsule = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(capsule) != {"payload", "sha256"} or _digest(capsule["payload"]) != capsule["sha256"]:
        raise ValueError("capsule checksum mismatch")
    if capsule["payload"].get("kind") != CAPSULE_KIND:
        raise ValueError("not an llm-run capsule")
    return capsule["payload"]


def replay_payload(payload, env=None) -> dict:
    """``env=(registry, store, project)`` replays into an existing store (a batch, in order)."""
    body = payload["body"]
    client = ReplayClient(payload["turns"], provider=payload["provider"],
                          model=payload["model"])
    with tempfile.TemporaryDirectory(prefix="ghostlab-replay-") as directory:
        registry, store, project = env or (*fresh_env(directory), PROJECT)
        try:
            result = llm_agent.run_llm(store, registry, project, client, body["agent"],
                                       body["faults"], name=body["name"])
        except ReplayDiverged as error:
            return {"reproduced": False, "error": str(error), "model_calls_live": 0}
    if result["summary"]["error"] and "ReplayDiverged" in result["summary"]["error"]:
        return {"reproduced": False, "error": result["summary"]["error"],
                "model_calls_live": 0}
    got = _expected(result["summary"], result["record"])
    want = payload["expected"]
    matches = {key: got[key] == want[key] for key in want}
    matches["all_turns_used"] = client.exhausted
    return {"reproduced": all(matches.values()), "matches": matches,
            "verdict": got["verdict"], "claim_check": got["claim_check"],
            "duplicate_refund": result["summary"]["duplicate_refund"],
            "model_calls_replayed": len(client.calls), "model_calls_live": 0,
            "model_log": client.calls, "summary": result["summary"]}


def replay(path) -> dict:
    return replay_payload(load(path))


VERIFY_KIND = "ghostlab-wonder-verify/1"


def key_behaviour(actions) -> dict:
    """What the model did with the optional idempotency key (raw model arguments)."""
    keys = [a["arguments"].get("idempotency_key") for a in actions
            if a["tool"] == "issue_refund" and isinstance(a.get("arguments"), dict)]
    retry = None
    if len(keys) > 1:
        rest = keys[1:]
        if any(k is None for k in rest):
            retry = "omitted"
        elif keys[0] is not None and all(k == keys[0] for k in rest):
            retry = "same"
        else:
            retry = "different"
    return {"issue_refund_calls": len(keys),
            "first_key": None if not keys else ("supplied" if keys[0] else "omitted"),
            "retried": len(keys) > 1, "retry_key": retry,
            "read_path_used": any(a["tool"] == "get_refund_status" for a in actions)}


def verify_row(payload, summary=None) -> dict:
    """Deterministic per-run facts for the Wonder-patch verification, from the gateway trace.

    The window is the timed-out issue_refund up to the next issue_refund (or the end).
    """
    s = summary or payload["summary"]
    actions = s["gateway_actions"]
    timeout = next((i for i, a in enumerate(actions) if a["tool"] == "issue_refund"
                    and (a.get("outcome") or {}).get("error") == "PaymentTimeout"), None)
    window = []
    if timeout is not None:
        for a in actions[timeout + 1:]:
            if a["tool"] == "issue_refund":
                break
            window.append(a["tool"])
    agent = payload["body"]["agent"]
    requested = sorted({c["name"] for t in payload["turns"]
                        for c in (t["turn"].get("tool_calls") or [])})
    return {"agent": agent, "label": payload["label"], "run_id": s["run_id"],
            "verdict": s["verdict"], "error": s["error"], "stop": s["stop"],
            "duplicate": s["duplicate_refund"], "claim": s["claim_check"]["verdict"],
            "timed_out": timeout is not None,
            "status_check_after_timeout": "get_refund_status" in window,
            "get_order_after_timeout": "get_order" in window,
            "key": key_behaviour(actions), "tools": [a["tool"] for a in actions],
            "requested_tools": requested,
            "requested_not_offered": [t for t in requested
                                      if t not in llm_agent.tools_for(agent)],
            "model_calls": s["model_calls"]}


def verify_table(rows) -> list:
    out = []
    for agent in dict.fromkeys(r["agent"] for r in rows):
        cell = [r for r in rows if r["agent"] == agent and not r["error"]]
        out.append({"agent": agent, "runs": len(cell),
                    "errored_runs": sum(1 for r in rows if r["agent"] == agent and r["error"]),
                    "timed_out": sum(r["timed_out"] for r in cell),
                    "status_check_after_timeout": sum(r["status_check_after_timeout"] for r in cell),
                    "get_order_after_timeout": sum(r["get_order_after_timeout"] for r in cell),
                    "keyless_first_refund": sum(r["key"]["first_key"] == "omitted" for r in cell),
                    "retried": sum(r["key"]["retried"] for r in cell),
                    "retry_same_key": sum(r["key"]["retry_key"] == "same" for r in cell),
                    "duplicates": sum(r["duplicate"] for r in cell),
                    "claim_matches_ledger": sum(r["claim"] == "match" for r in cell),
                    "verdict_pass": sum(r["verdict"] == "pass" for r in cell),
                    "requested_not_offered": sorted({t for r in cell
                                                     for t in r["requested_not_offered"]})})
    return out


def _excluded_reason(row, run) -> str:
    """Plain words for a run excluded from the table (it errored outside the agent's control)."""
    turn = next((i + 1 for i, t in enumerate(run["turns"]) if t["turn"]["status"] == "failed"), None)
    if turn and "deadline_exceeded" in (row["error"] or "") and not row["key"]["issue_refund_calls"]:
        who = "Meta API" if run["provider"].startswith("meta") else run["provider"]
        return f"{who} timeout on turn {turn}, before any refund"
    return row["error"]


def replay_verify(payload) -> dict:
    """Replay every run and the hunt of a Wonder-patch verification. Zero network, zero keys."""
    from . import adversary
    rows, failed, hunt = [], [], None
    # One store, recorded order: the live hunt ran after the arms, so its run ids follow theirs.
    with tempfile.TemporaryDirectory(prefix="ghostlab-replay-") as directory:
        registry, store = fresh_env(directory)
        for run in payload["runs"]:
            got = replay_payload(run, env=(registry, store, "wonder-verify"))
            if not got["reproduced"]:
                failed.append(run["label"])
                continue
            rows.append(verify_row(run, got["summary"]))
        if payload.get("hunt"):
            out = adversary.replay_exploration(payload["hunt"], directory,
                                               env=(registry, store), project_id="wonder-hunt")
            out.pop("state")
            hunt = {"reproduced": out["reproduced"], "summary": out["summary"]}
    table = verify_table(rows)
    excluded = [{"label": r["label"], "agent": r["agent"], "error": r["error"],
                 "reason": _excluded_reason(r, run)}
                for r, run in zip(rows, payload["runs"]) if r["error"]] if not failed else []
    reproduced = (not failed and table == payload["table"]
                  and (hunt is None or hunt["reproduced"]))
    return {"reproduced": reproduced, "failed_runs": failed, "table": table, "rows": rows,
            "excluded": excluded, "provider": payload["provider"], "model": payload["model"],
            "patch": payload["patch"], "hunt": hunt, "model_calls_live": 0}


def main(argv=None) -> int:
    failed = 0
    for path in (argv if argv is not None else sys.argv[1:]):
        try:
            if load(path)["kind"] != CAPSULE_KIND:
                continue
        except (ValueError, KeyError):
            continue  # not an llm-run capsule (calibration tables, explorations)
        result = replay(path)
        failed += not result["reproduced"]
        print(f"{path}: reproduced={result['reproduced']} verdict={result.get('verdict')}"
              f" claim={result.get('claim_check', {}).get('verdict')}"
              f" live_model_calls={result['model_calls_live']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
