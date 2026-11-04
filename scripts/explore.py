"""Muse adversary exploration: live (recorded) or keyless replay.

    set -a; . ./.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/explore.py --live [--diagnose] [--llm-targets]
    PYTHONPATH=src .venv/bin/python scripts/explore.py --replay demo/recorded/adversary-exploration.json
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostlab.llm import adversary as ADV  # noqa: E402
from ghostlab.llm import capsules  # noqa: E402
from ghostlab.llm.client import RecordingClient, client_for  # noqa: E402

OUT = ROOT / "demo" / "recorded"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("--replay")
    p.add_argument("--max-experiments", type=int, default=12)
    p.add_argument("--max-usd", type=float, default=1.0)
    p.add_argument("--diagnose", action="store_true")
    p.add_argument("--llm-targets", action="store_true")
    p.add_argument("--out")
    a = p.parse_args()
    if a.replay:
        payload = json.loads(Path(a.replay).read_text())["payload"]
        with tempfile.TemporaryDirectory() as tmp:
            out = ADV.replay_exploration(payload, tmp)
        out.pop("state")
        print(json.dumps({"reproduced": out["reproduced"], "matches": out["matches"],
                          "stop_status": out["summary"]["stop_status"],
                          "model_calls_live": 0}, indent=1))
        return 0 if out["reproduced"] else 1
    if not a.live:
        p.error("pass --live or --replay")
    variants = ADV.DETERMINISTIC
    max_experiments, max_usd = a.max_experiments, a.max_usd
    agent = None
    if a.llm_targets:
        variants = ("llm-naive", "llm-hardened", "llm-naive-noread", "llm-hardened-noread")
        max_experiments = min(max_experiments, 6)
        max_usd = min(max_usd, 3.0)
        agent = RecordingClient(client_for("meta-dev", "muse-spark-1.3", usd_limit=max_usd,
                                           purpose="adversary-target"))
    adversary = RecordingClient(client_for("meta-dev", "muse-spark-1.3",
                                           reasoning_effort="medium", usd_limit=max_usd,
                                           purpose="adversary"))
    with tempfile.TemporaryDirectory() as tmp:
        registry, store = capsules.fresh_env(tmp)
        explorer = ADV.ModelExplorer(adversary, store=store, registry=registry,
                                     project_id="explore", max_experiments=max_experiments,
                                     variants=variants, llm_client=agent)
        state = ADV.explore(ADV.new_state(explorer, max_usd=max_usd))
        diagnosis, diag_records = None, []
        if a.diagnose and state["failure"]:
            wonder = RecordingClient(client_for("meta-dev", "muse-wonder-1.0", usd_limit=0.5,
                                                purpose="diagnosis"))
            diagnosis = ADV.diagnose(wonder, state)
            diag_records = wonder.records
        capsule = ADV.record_payload(state, adversary.records,
                                     agent.records if agent else [], diagnosis, diag_records)
    name = a.out or ("adversary-exploration-llm.json" if a.llm_targets
                     else "adversary-exploration.json")
    (OUT / name).write_text(json.dumps(capsule, indent=1) + "\n")
    s = capsule["payload"]["summary"]
    print(json.dumps({"stop_status": s["stop_status"], "error": s["error"],
                      "experiments": [(e["agent"], [(f["trigger"]["tool"],
                                                     f["trigger"]["occurrence"])
                                                    for f in e["faults"]], e["verdict"],
                                       e["rules_failed"]) for e in s["experiments"]],
                      "local_rejections": len(s["local_rejections"]),
                      "budget": s["budget"], "diagnosis": diagnosis,
                      "agent_usd": agent.session_usd if agent else 0}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
