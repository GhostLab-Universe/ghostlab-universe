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


def replay_payload(payload) -> dict:
    body = payload["body"]
    client = ReplayClient(payload["turns"], provider=payload["provider"],
                          model=payload["model"])
    with tempfile.TemporaryDirectory(prefix="ghostlab-replay-") as directory:
        registry, store = fresh_env(directory)
        try:
            result = llm_agent.run_llm(store, registry, PROJECT, client, body["agent"],
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
