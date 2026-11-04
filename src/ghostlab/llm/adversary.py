"""Muse adversary: the planner's ``model_guided`` explorer, driven by tool calls.

``ModelExplorer.propose(findings)`` is the bridge ``experiments.plan`` expects:
Muse tool calls become **v1 requested-config bodies**. Every call is checked
against the fault catalog in code first; an invalid call is answered
``{"rejected": ...}``, counted, and never executed. Valid bodies still pass
through ``experiments.ops`` validation inside ``plan.execute_plan``, which
records its own rejections. Verdicts come only from the controller's
deterministic checks; the model's text is never evidence.

``explore()`` runs rounds of ``execute_plan`` until a stop status is decided in
code: ``found_failure`` (a violation, minimized with a stable replay),
``coverage_complete`` (every catalog cell tried, no violation),
``budget_exhausted`` (experiments, tokens or USD), ``paused_on_error`` (a
provider error; ``explore(state)`` resumes), or ``model_stopped`` (the model
ended before coverage; reported as is).
"""

import contextlib
import json
from copy import deepcopy

from ..core import reality_diff
from ..envs.adapters import RefundEnvAdapter
from ..envs.capabilities import discover
from ..experiments import ops, plan
from ..experiments.minimize import minimize_prefix, refund_rules_violated
from . import agent as llm_agent
from .client import ModelCallFailed

MAX_OCCURRENCE = 4
MAX_FAULTS = 2
DETERMINISTIC = ("vulnerable", "repaired")
DIAGNOSIS_LABEL = "explanation, not evidence"

SYSTEM = """You are GhostLab's adversary. You search for fault schedules that make a refund
agent break a business invariant (duplicate refund, over-refund, unauthorized refund, reused
idempotency key). You never judge outcomes yourself: every verdict you see comes from
deterministic checks over the payment ledger.
Tools:
- list_fault_catalog: the fault hooks, trigger tools, occurrences and agent variants you may use.
- run_experiment: run one agent variant under a fault schedule. Each call spends one experiment.
- fork: rerun an earlier experiment with a different fault schedule. Spends one experiment.
- minimize: shrink a violating run to its shortest violating action prefix. Free.
Start with list_fault_catalog. Think about where a timeout hurts most: after a write commits
but before the caller learns it did. When a run violates an invariant, minimize it and then
reply with a short summary instead of calling more tools."""

_SCHEDULE = {"type": "array", "minItems": 1, "maxItems": MAX_FAULTS,
             "items": {"type": "object", "additionalProperties": False,
                       "required": ["hook", "trigger_tool", "occurrence"],
                       "properties": {"hook": {"type": "string"},
                                      "trigger_tool": {"type": "string"},
                                      "occurrence": {"type": "integer"}}}}
TOOLS = [
    {"name": "list_fault_catalog", "description": "List hooks, trigger tools, occurrences"
     " and agent variants.", "parameters": {"type": "object", "properties": {},
                                            "additionalProperties": False}},
    {"name": "run_experiment", "description": "Run an agent variant under a fault schedule;"
     " returns the deterministic verdict and a Reality Diff summary.",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["fault_schedule", "agent_variant"],
                    "properties": {"fault_schedule": _SCHEDULE,
                                   "agent_variant": {"type": "string"}}}},
    {"name": "fork", "description": "Rerun an earlier experiment with a different schedule.",
     "parameters": {"type": "object", "additionalProperties": False,
                    "required": ["run_id", "fault_override"],
                    "properties": {"run_id": {"type": "string"},
                                   "fault_override": _SCHEDULE}}},
    {"name": "minimize", "description": "Shortest violating action prefix of a run.",
     "parameters": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                    "properties": {"run_id": {"type": "string"}}}},
]


def catalog(registry, variants) -> dict:
    found = discover(registry, "refund-env")
    return {"env_id": "refund-env", "hooks": sorted(found["fault_hooks"]),
            "trigger_tools": ["*"] + sorted(t["name"] for t in found["discovered_tools"]),
            "occurrences": list(range(1, MAX_OCCURRENCE + 1)),
            "agent_variants": list(variants), "max_faults_per_schedule": MAX_FAULTS}


def validate_schedule(schedule, cat) -> list:
    """Return config faults for a valid schedule; raise ValueError otherwise."""
    if type(schedule) is not list or not 1 <= len(schedule) <= MAX_FAULTS:
        raise ValueError(f"fault_schedule must list 1-{MAX_FAULTS} faults")
    faults = []
    for item in schedule:
        if type(item) is not dict or set(item) != {"hook", "trigger_tool", "occurrence"}:
            raise ValueError("each fault needs exactly hook, trigger_tool, occurrence")
        if item["hook"] not in cat["hooks"]:
            raise ValueError(f"hook {item['hook']!r} is not in the catalog")
        if item["trigger_tool"] not in cat["trigger_tools"]:
            raise ValueError(f"trigger_tool {item['trigger_tool']!r} is not in the catalog")
        if type(item["occurrence"]) is not int or item["occurrence"] not in cat["occurrences"]:
            raise ValueError(f"occurrence must be one of {cat['occurrences']}")
        faults.append({"hook": item["hook"], "params": {}, "required": True,
                       "trigger": {"type": "occurrence", "tool": item["trigger_tool"],
                                   "occurrence": item["occurrence"]}})
    return faults


def cell(body) -> tuple:
    return (body["agent"],) + tuple(sorted((f["trigger"]["tool"], f["trigger"]["occurrence"])
                                           for f in body["faults"]))


def _as_core_run(run):
    evidence = run.get("evidence") or {}
    return {"trace": evidence.get("trace") or [], "final_state": evidence.get("snapshot") or
            {"total_refunded_minor": 0}, "checks": run.get("checks") or {}}


def compact_diff(baseline_run, faulted_run) -> dict:
    """Reality Diff (``core.reality_diff``) reduced to what a model needs."""
    diff = reality_diff(_as_core_run(baseline_run), _as_core_run(faulted_run))
    first = diff["first_divergence"]

    def side(event):
        if not event:
            return None
        return {"tool": event["request"]["tool"], "outcome": event["outcome"]}
    return {"first_divergence": None if not first else {
        "event_id": first["event_id"], "baseline": side(first["baseline"]),
        "faulted": side(first["faulted"])},
        "baseline_refunded_minor": diff["baseline_refunded_minor"],
        "faulted_refunded_minor": diff["faulted_refunded_minor"],
        "violated_invariants": diff["violated_invariants"]}


class ModelExplorer:
    def __init__(self, client, *, store, registry, project_id, max_experiments=12,
                 variants=DETERMINISTIC, llm_client=None):
        self.client, self.store, self.registry = client, store, registry
        self.project_id = project_id
        self.max_experiments = max_experiments
        self.variants = tuple(variants)
        self.llm_client = llm_client  # bound for llm-* experiments (live or replay)
        self.catalog = catalog(registry, self.variants)
        self.conv = None
        self.outbox = None       # tool results to send on the next turn (kept across errors)
        self.awaiting = []       # [(call_id, proposal_index)]
        self.proposed = 0        # experiments handed to the planner
        self.rejected = []       # local schema rejections (never executed)
        self.bodies = {}         # run_id -> requested body
        self.done = False
        self.final_text = None
        self.last_proposals = []

    # -- planner bridge -------------------------------------------------
    def propose(self, findings):
        if type(findings) is not list:
            raise plan.PlanError("explorer findings must be a list")
        if self.outbox is None:
            self.outbox = []
        if self.awaiting:
            by_index = {row["unit_index"]: row for row in findings}
            for call_id, index in self.awaiting:
                self.outbox.append({"id": call_id, "output": json.dumps(
                    by_index.get(index, {"error": "no finding recorded"}))})
            self.awaiting = []
        for _ in range(6):  # local-only turns (catalog/minimize/rejections) per round
            if self.conv is None:
                self.conv = self.client.open(SYSTEM, TOOLS)
                turn = self.conv.send(user=(
                    f"Target environment refund-env. Experiment budget:"
                    f" {self.max_experiments}. Find a failure."))
            else:
                turn = self.conv.send(tool_results=self.outbox)
            if turn.status == "failed":
                raise ModelCallFailed(turn.error or "adversary call failed")
            self.outbox = []
            if not turn.tool_calls:
                self.done, self.final_text = True, turn.text
                return []
            proposals = []
            for call in turn.tool_calls:
                answer = self._handle(call, proposals)
                if answer is not None:
                    self.outbox.append({"id": call["id"], "output": json.dumps(answer)})
            if proposals:
                self.last_proposals = proposals
                return proposals
        self.done = True
        return []

    def _handle(self, call, proposals):
        name, args = call["name"], call["arguments"]
        if args is None:
            return self._reject(call, "arguments must be a JSON object")
        if name == "list_fault_catalog":
            return self.catalog
        if name == "minimize":
            return self.minimize(args.get("run_id"))
        if name not in ("run_experiment", "fork"):
            return self._reject(call, f"unknown tool {name!r}")
        try:
            if name == "run_experiment":
                variant = args.get("agent_variant")
                if variant not in self.variants:
                    raise ValueError(f"agent_variant must be one of {list(self.variants)}")
                faults = validate_schedule(args.get("fault_schedule"), self.catalog)
            else:
                source = self.bodies.get(args.get("run_id"))
                if source is None:
                    raise ValueError("fork needs a run_id from an earlier experiment")
                variant = source["agent"]
                faults = validate_schedule(args.get("fault_override"), self.catalog)
        except ValueError as error:
            return self._reject(call, str(error))
        if self.proposed >= self.max_experiments:
            return {"rejected": "experiment budget exhausted"}
        self.proposed += 1
        body = llm_agent.requested(variant, faults, name=f"adversary-{self.proposed}")
        self.awaiting.append((call["id"], len(proposals)))
        proposals.append(body)
        return None

    def _reject(self, call, reason):
        self.rejected.append({"tool": call["name"], "arguments": call["raw"], "reason": reason})
        return {"rejected": reason, "executed": False}

    def minimize(self, run_id):
        record = self._run(run_id)
        if record is None:
            return {"error": "unknown run_id"}
        trace = (record["run"].get("evidence") or {}).get("trace") or []
        actions = [{"tool": e["request"]["tool"], "arguments": e["request"]["arguments"]}
                   for e in trace]
        try:
            result = minimize_prefix(RefundEnvAdapter, dict(llm_agent.FIXTURE), actions,
                                     refund_rules_violated)
        except ValueError as error:
            return {"run_id": run_id, "error": str(error)}
        return {"run_id": run_id, "required_prefix": [a["tool"] for a in
                                                      result["required_prefix"]],
                "dropped": result["dropped"], "stable": result["stable"],
                "actions": result["required_prefix"]}

    def _run(self, run_id):
        try:
            return ops.get_run(self.store, self.project_id, run_id)
        except Exception:
            return None


def _baseline(state, explorer, agent_name):
    """No-fault run of the same agent, executed once per agent and cached."""
    cache = state.setdefault("baselines", {})
    if agent_name not in cache:
        body = llm_agent.requested(agent_name, [], name=f"baseline-{agent_name}")
        bound = llm_agent.bind(explorer.llm_client) if explorer.llm_client is not None \
            else contextlib.nullcontext()
        with bound:
            result = plan.execute_plan(explorer.store, explorer.registry, plan.build_plan(
                "refund-env", llm_agent.FIXTURE, agent_name, "baseline", llm_agent.RULES, None,
                body["limits"], 1, "fixed", [0], requested_configs=[body],
                policy=llm_agent.policy()), explorer.project_id)
        run_id = result["per_run"][0]["run_id"]
        cache[agent_name] = explorer._run(run_id)["run"]
    return cache[agent_name]


def _finding(state, explorer, row, body):
    finding = {"run_id": row["run_id"], "outcome": row["outcome"], "reason": row["reason"],
               "status": row["status"], "verdict": row["verdict"],
               "rules_failed": row["rules_failed"], "agent_variant": body["agent"]}
    if row["outcome"] == "executed":
        run = explorer._run(row["run_id"])["run"]
        explorer.bodies[row["run_id"]] = body
        finding["faults_fired"] = [f["fired"] for f in run["faults"]]
        finding["tool_calls"] = run["tool_calls"]
        finding["model_calls"] = run["model_calls"]
        finding["reality_diff"] = compact_diff(_baseline(state, explorer, body["agent"]), run)
    return finding


def new_state(explorer, *, max_tokens=200_000, max_usd=1.0) -> dict:
    return {"explorer": explorer, "experiments": [], "rounds": 0, "findings": [],
            "stop_status": None, "error": None, "failure": None,
            "limits": {"max_experiments": explorer.max_experiments, "max_tokens": max_tokens,
                       "max_usd": max_usd}, "ops_rejected": 0, "cells": set()}


def _tokens(client):
    return sum((c.get("input_tokens") or 0) + (c.get("output_tokens") or 0)
               for c in client.calls)


def explore(state) -> dict:
    """Run (or resume) an exploration until a stop status. Mutates and returns ``state``."""
    explorer = state["explorer"]
    client = explorer.client
    state["stop_status"], state["error"] = None, None
    total_cells = (len(explorer.variants) * (len(explorer.catalog["trigger_tools"])
                                             * len(explorer.catalog["occurrences"])))
    while True:
        if (len(state["experiments"]) >= state["limits"]["max_experiments"]
                or _tokens(client) >= state["limits"]["max_tokens"]
                or getattr(client, "session_usd", 0) >= state["limits"]["max_usd"]):
            state["stop_status"] = "budget_exhausted"
            break
        the_plan = plan.build_plan(
            "refund-env", llm_agent.FIXTURE, "vulnerable", "adversary exploration",
            llm_agent.RULES, None, llm_agent.requested("vulnerable", [])["limits"], 1,
            "model_guided", [0], explorer=explorer, policy=llm_agent.policy())
        try:
            if explorer.llm_client is not None:
                with llm_agent.bind(explorer.llm_client):
                    result = plan.execute_plan(explorer.store, explorer.registry, the_plan,
                                               explorer.project_id,
                                               findings=state["findings"])
            else:
                result = plan.execute_plan(explorer.store, explorer.registry, the_plan,
                                           explorer.project_id, findings=state["findings"])
        except ModelCallFailed as error:
            text = str(error)
            state["error"] = text
            state["stop_status"] = "budget_exhausted" if "cap" in text else "paused_on_error"
            break
        except plan.PlanError:
            if explorer.done:  # the model ended the conversation
                covered = len(state["cells"]) >= total_cells
                state["stop_status"] = "coverage_complete" if covered else "model_stopped"
                break
            raise
        state["rounds"] += 1
        findings = [(row, explorer.last_proposals[row["unit_index"]])
                    for row in result["per_run"]]
        state["findings"] = []
        for row, body in findings:
            finding = _finding(state, explorer, row, body)
            finding["unit_index"] = row["unit_index"]
            state["findings"].append(finding)
            state["experiments"].append({**finding, "body": body})
            if row["outcome"] != "executed":
                state["ops_rejected"] += 1
            state["cells"].add(cell(body))
        violating = [f for f in state["findings"] if f["outcome"] == "executed"
                     and f["rules_failed"] and f["verdict"] == "fail"]
        if violating:
            target = violating[0]
            minimized = explorer.minimize(target["run_id"])
            if minimized.get("stable"):
                state["failure"] = {**target, "minimized": minimized}
                state["stop_status"] = "found_failure"
                break
        if len(state["cells"]) >= total_cells:
            state["stop_status"] = "coverage_complete"
            break
    return state


def summary(state) -> dict:
    explorer = state["explorer"]
    client = explorer.client
    return {"stop_status": state["stop_status"], "error": state["error"],
            "rounds": state["rounds"], "experiments": [
                {k: v for k, v in e.items() if k != "body"} | {"agent": e["body"]["agent"],
                                                              "faults": e["body"]["faults"]}
                for e in state["experiments"]],
            "local_rejections": deepcopy(explorer.rejected),
            "ops_rejections": state["ops_rejected"], "failure": state["failure"],
            "final_text": explorer.final_text,
            "budget": {"usd_spent": getattr(client, "session_usd", 0.0),
                       "usd_cap": state["limits"]["max_usd"], "tokens": _tokens(client),
                       "max_tokens": state["limits"]["max_tokens"],
                       "experiments": len(state["experiments"]),
                       "max_experiments": state["limits"]["max_experiments"]},
            "model_calls": list(client.calls)}


DIAGNOSIS_SYSTEM = """You diagnose failures found by GhostLab. You get a Reality Diff (baseline vs
faulted run of the same agent) and the minimized action trace that still violates an
invariant. Reply with only a JSON object: {"root_cause": "<2-3 sentences>",
"proposed_patch": "<concrete change to the agent's retry logic, pseudo-code allowed>"}."""


def diagnose(client, state) -> dict:
    """One model call per confirmed failure. Output is explanation, never evidence.

    The evidence is the rerun: the same fault schedule against the repaired agent
    through the controller, whose deterministic verdict is reported alongside.
    """
    failure = state["failure"]
    explorer = state["explorer"]
    body = explorer.bodies[failure["run_id"]]
    conv = client.open(DIAGNOSIS_SYSTEM, [])
    turn = conv.send(user=json.dumps({"agent": body["agent"], "faults": body["faults"],
                                      "rules_failed": failure["rules_failed"],
                                      "reality_diff": failure["reality_diff"],
                                      "minimized_trace": failure["minimized"]["actions"]}))
    text = turn.text or ""
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        parsed = {"root_cause": None, "proposed_patch": None, "unparsed": text[:2000]}
    out = {"label": DIAGNOSIS_LABEL, "model": turn.model, "status": turn.status,
           "error": turn.error, **{k: parsed.get(k) for k in ("root_cause", "proposed_patch")}}
    if "unparsed" in parsed:
        out["unparsed"] = parsed["unparsed"]
    fixed_agent = "repaired" if body["agent"] in DETERMINISTIC else None
    if fixed_agent:
        rerun_body = llm_agent.requested(fixed_agent, body["faults"], name="diagnosis-rerun")
        result = plan.execute_plan(explorer.store, explorer.registry, plan.build_plan(
            "refund-env", llm_agent.FIXTURE, fixed_agent, "rerun", llm_agent.RULES, None,
            rerun_body["limits"], 1, "fixed", [0], requested_configs=[rerun_body],
            policy=llm_agent.policy()), explorer.project_id)
        row = result["per_run"][0]
        out["rerun"] = {"agent": fixed_agent, "run_id": row["run_id"], "verdict": row["verdict"],
                        "rules_failed": row["rules_failed"], "evidence": "controller verdict"}
    else:
        out["rerun"] = {"agent": "llm-hardened",
                        "capsule": "demo/recorded/hardened-spark-pass.json",
                        "evidence": "recorded capsule; replay it keylessly"}
    return out


EXPLORATION_KIND = "ghostlab-exploration/1"


def record_payload(state, adversary_records, agent_records, diagnosis=None,
                   diagnosis_records=None) -> dict:
    explorer = state["explorer"]
    payload = {"kind": EXPLORATION_KIND, "variants": list(explorer.variants),
               "limits": state["limits"], "adversary_model": explorer.client.model,
               "adversary_turns": adversary_records, "agent_turns": agent_records,
               "diagnosis_turns": diagnosis_records or [], "summary": summary(state),
               "diagnosis": diagnosis}
    from ..capsule import _digest
    return {"payload": payload, "sha256": _digest(payload)}


def replay_exploration(payload, directory) -> dict:
    """Re-run a recorded exploration with ReplayClients: zero network, zero keys."""
    from . import capsules
    from .client import ReplayClient
    registry, store = capsules.fresh_env(directory)
    adversary = ReplayClient(payload["adversary_turns"], model=payload["adversary_model"])
    agent_client = ReplayClient(payload["agent_turns"]) if payload["agent_turns"] else None
    explorer = ModelExplorer(adversary, store=store, registry=registry, project_id="replay",
                             max_experiments=payload["limits"]["max_experiments"],
                             variants=payload["variants"], llm_client=agent_client)
    state = explore(new_state(explorer, max_tokens=payload["limits"]["max_tokens"],
                              max_usd=payload["limits"]["max_usd"]))
    diagnosis = None
    if payload.get("diagnosis_turns") and state["failure"]:
        diagnosis = diagnose(ReplayClient(payload["diagnosis_turns"]), state)
    got = summary(state)
    want = payload["summary"]
    keys = ("verdict", "rules_failed", "outcome", "agent")
    matches = {"stop_status": got["stop_status"] == want["stop_status"],
               "experiments": [{k: e.get(k) for k in keys} for e in got["experiments"]]
               == [{k: e.get(k) for k in keys} for e in want["experiments"]],
               "adversary_turns_used": adversary.exhausted}
    return {"reproduced": all(matches.values()), "matches": matches, "summary": got,
            "diagnosis": diagnosis, "model_calls_live": 0, "state": state}
