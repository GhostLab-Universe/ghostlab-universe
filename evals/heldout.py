"""Held-out fault timings for ``group-journey/1``: chosen by rule, outside the gate.

    python -m evals.heldout --write-rule           write the selection to heldout_rule.json
    python -m evals.heldout --out <dir>            run it, store evidence, print the counts
    python -m evals.heldout --out <dir> --record   also write heldout_result.json

The cases in ``evals.cases`` use the timings they were built with. These don't. The rule
below fixes the universe, the exclusions and the seed before any selected case runs, and
``heldout_rule.json`` was committed before ``heldout_result.json``. A held-out result is
never pinned in ``expected.json``: a timing in the gate is a tuned timing.

Each case drives the real controller with one mocked proposal, and approves any repair
it reaches. Verdicts come from ``journey_oracle`` reading the stored runs, so a controller
claim can't change a count. An inconclusive run stays inconclusive, and nothing is dropped:
a case that raises is counted as errored. Inference is mocked. No model is called.
"""

import argparse
import itertools
import json
import os
import platform
import random
import re
import shutil
import sys
from pathlib import Path

from ghostlab.orchestration import DEFAULT_WORLDS
from ghostlab.worlds.journey import JourneyFixture

from .cases import CID, Case, Recorder, approve_and_resume, export_and_replay, start
from .evidence import canonical_sha256
from .harness import CHECKED, FIXTURE_LABEL, JOURNEY, NAIVE, delayed_update, journey_proposal
from .harness import reply, revocation
from .oracles import journey_oracle
from .run import run_case
from .score import score_case

SCHEMA = "ghostlab-heldout/1"
RULE_PATH = Path(__file__).with_name("heldout_rule.json")
RESULT_PATH = Path(__file__).with_name("heldout_result.json")
ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "c0a13cc"

SEED = 20261031  # the date this rule was written
CAP = 20         # the harness's default cap, for every case
PER_STRATUM = 5  # for each sampled stratum; a smaller stratum is taken whole
ADAPTERS = (NAIVE, CHECKED)
REVOKED = "sharing_revoked_while_queued"
STALE = "stale_preference_memory"
DELAYED = "delayed_itinerary_update"

# Every timing value in tests/ and evals/cases.py at BASE_COMMIT, per fault, whatever the
# recipient or adapter. A case using one of these for any of its faults is out. Tests also
# reach None, 2 (delayed) and 17 through fault_scenarios and race_scenario.
TUNED = {REVOKED: frozenset({None, 0, 2, 3, 4, 5, 10, 11, 12, 14, 15, 17, 999, 1000}),
         STALE: frozenset({None, 5}),
         DELAYED: frozenset(range(19))}  # test_m03_journey sweeps 0 to 18

CLASSIFICATION = {
    "failure_found": "journey_oracle finds an unpermitted delivery or a stale commitment in the "
                     "stored fork run, whatever coverage says",
    "fault_fired_no_violation": "the oracle finds no violation and coverage.complete is true",
    "inconclusive": "the oracle finds no violation and coverage.complete is false",
    "errored": "the case raised, or no fork run was stored",
    "unsafe_enforced_run": "journey_oracle finds a violation or mutated_on_denial in the stored "
                           "repaired run; the controller's safety_held is not read",
}

CONSTANTS = {"None": None, "RACE_AFTER_EVENT": 17}
_INT = re.compile(r"\d+")


# --- the rule ----------------------------------------------------------------------------

def _revocation(recipient: str, after_event: int | None) -> dict:
    return revocation(after_event, recipient)


def _stale(version: int, after_event: int | None) -> dict:
    return {"fault_id": STALE, "after_event": after_event,
            "parameters": [{"name": "preference_version", "value": version}]}


def _timing(fault: dict):
    if fault["fault_id"] == DELAYED:
        return fault["parameters"][0]["value"]
    return fault["after_event"]


def fault_choices(tuned=None) -> dict:
    """Every reachable choice for one fault: no capped run has an event past ``CAP``."""
    tuned = TUNED if tuned is None else tuned
    fixture = JourneyFixture()
    after = (None, *range(1, CAP + 1))
    choices = {
        REVOKED: [_revocation(recipient, value) for recipient in fixture.participants
                  for value in after],
        STALE: [_stale(version, value) for version, _ in fixture.intents for value in after],
        DELAYED: [delayed_update(value) for value in range(CAP + 1)]}
    return {fault_id: [fault for fault in faults if _timing(fault) not in tuned[fault_id]]
            for fault_id, faults in choices.items()}


def fault_sets() -> list[tuple[str, ...]]:
    faults = (REVOKED, STALE, DELAYED)
    return [combination for size in (1, 2, 3) for combination in itertools.combinations(faults, size)]


def select(tuned=None) -> list[dict]:
    """All of the revocation-only space, and a seeded sample of each other stratum."""
    choices = fault_choices(tuned)
    rng = random.Random(SEED)
    selected = []
    for adapter in ADAPTERS:
        for fault_set in fault_sets():
            stratum = [list(faults) for faults in itertools.product(
                *(choices[fault_id] for fault_id in fault_set))]
            whole = fault_set == (REVOKED,) or len(stratum) <= PER_STRATUM
            for faults in stratum if whole else rng.sample(stratum, PER_STRATUM):
                selected.append({"case_id": f"heldout-{len(selected) + 1:03}", "adapter": adapter,
                                 "fault_set": list(fault_set), "faults": faults})
    return selected


def build_rule() -> dict:
    cases = select()
    found = scan()[0]
    return {
        "schema": SCHEMA, "world": JOURNEY, "base_commit": BASE_COMMIT, "seed": SEED,
        "max_tool_calls": CAP, "per_stratum": PER_STRATUM,
        "rule": "Universe: both adapters, each non-empty set of the three faults, recipient or "
                "preference version from the fixture, after_event null or 1 to the cap, "
                "delay_events 0 to the cap. Remove any case with a fault whose timing value is "
                "in the exclusions, whatever its recipient or adapter. Strata are (adapter, "
                "fault set). Take the revocation-only strata whole. From each other stratum "
                "take per_stratum cases with random.Random(seed).sample, walking strata in "
                "order, or all of them if there are fewer.",
        "known_limits": [
            "Tests already sweep delay_events 0 to 18, so only 19 and 20 are left, and no "
            "adapter is expected to read the update that late. They stay in the universe "
            "rather than being filtered out, so this says nothing new about that fault's "
            "timing, and a case carrying it is unlikely to reach complete coverage.",
            "journey_oracle checks deliveries and commitments. It has no independent check of "
            "mandatory_participants_feasible, so that invariant is reported from the "
            "controller's findings and a repair for it alone would show as an integrity "
            "failure, not as failure_found."],
        "classification": CLASSIFICATION,
        "exclusions": [{"fault_id": fault_id, "values": sorted(
            TUNED[fault_id], key=lambda value: (value is not None, value)),
            "found_in": {str(value): sources for (fault, value), sources in sorted(
                found.items(), key=lambda item: (item[0][0], str(item[0][1])))
                if fault == fault_id}} for fault_id in TUNED],
        "case_count": len(cases), "selection_sha256": canonical_sha256(cases), "cases": cases}


# --- what the tuned cases use ------------------------------------------------------------

def _files():
    files = [path for path in sorted((ROOT / "tests").rglob("*.py"))
             if path.name != "test_m09_heldout.py"]
    return [*files, ROOT / "evals" / "cases.py"]


def _value(text: str, default=None):
    """(resolved, value): a literal or a known constant, or the name it couldn't resolve."""
    text = text.strip().removeprefix("after_event=").removeprefix("events=").strip()
    if not text:
        return True, default
    if _INT.fullmatch(text):
        return True, int(text)
    if text in CONSTANTS:
        return True, CONSTANTS[text]
    return False, text


def _parametrized(text: str):
    pattern = r'parametrize\("([^"]*after_event[^"]*)",\s*\[(.*?)\]\)'
    for match in re.finditer(pattern, text, re.DOTALL):
        names = [name.strip() for name in match[1].split(",")]
        for row in re.finditer(r"\(([^()]*)\)", match[2]):
            yield match.start(), row[1].split(",")[names.index("after_event")]


def _sightings(text: str):
    """(offset, fault id or None for any timed fault, text of the value, its default)."""
    forms = (
        (REVOKED, r"\brevocation\(\s*((?:after_event=)?[^,()\s]*)", None),
        (REVOKED, r"\brevoke\([^,()]+,\s*([^()]+?)\s*\)", None),
        (REVOKED, r"\brevoke\([^,()]+()\)", None),
        (REVOKED, r"\.update\(after_event=(\d+)\)", None),
        (DELAYED, r'"delay_events":\s*(\w+)', None),
        (DELAYED, r'"name":\s*"delay_events",\s*"value":\s*(\w+)', None),
        (DELAYED, r"\bdelayed(?:_update)?\(((?:events=)?\w*)\)", 3))
    for fault_id, pattern, default in forms:
        for match in re.finditer(pattern, text):
            yield match.start(), fault_id, match[1], default
    for match in re.finditer(r'"delay_events":\s*n\}\)\]\)\s*for n in range\((\d+)\)', text):
        for value in range(int(match[1])):
            yield match.start(), DELAYED, str(value), None
    for fault_id in (REVOKED, STALE):
        for match in re.finditer(rf'\(\s*"{fault_id}",\s*(\w+),', text):
            yield match.start(), fault_id, match[1], None
        pattern = rf'"fault_id":\s*"{fault_id}",\s*"after_event":\s*(\w+)'
        for match in re.finditer(pattern, text):
            yield match.start(), fault_id, match[1], None
    for match in re.finditer(r"\bone_fault\(steps,\s*([^,]+),\s*(\w+)\)", text):
        named = match[1].strip().strip('"')
        for fault_id in (REVOKED, STALE) if named == "fault_id" else (named,):
            yield match.start(), fault_id, match[2], None
    for offset, value in _parametrized(text):
        yield offset, REVOKED, value, None


def scan() -> tuple[dict, set[str]]:
    """Every timing written in tests/ and evals/cases.py, and any value left unresolved."""
    found, unresolved = {}, set()
    for path in _files():
        text = path.read_text("utf-8")
        for offset, fault_id, written, default in _sightings(text):
            resolved, value = _value(written, default)
            if not resolved:
                unresolved.add(value)
                continue
            place = f"{path.relative_to(ROOT).as_posix()}:{text.count(chr(10), 0, offset) + 1}"
            sources = found.setdefault((fault_id, value), [])
            if place not in sources:
                sources.append(place)
    return found, unresolved


# --- running -----------------------------------------------------------------------------

def _replay(stack, run: str) -> dict:
    """A replay that raises is a replay that failed, and it's counted as one."""
    try:
        return export_and_replay(stack, run, campaign=CID)
    except Exception as error:  # noqa: BLE001 - the class is the measurement
        return {"reproduced": False, "reported_model_calls": None,
                "provider_requests_during_replay": None, "error": type(error).__name__}


def _run_selected(selected: dict):
    def run(rec: Recorder) -> None:
        proposal = journey_proposal(selected["adapter"], faults=selected["faults"], cap=CAP)
        stack = rec.stack("main", reply(proposal))
        state = start(stack, JOURNEY, max_iterations=1)
        if state["paused"]:
            state = approve_and_resume(stack, state)
        with stack.controller() as controller:
            stored = controller.state(CID)
        repaired = bool((state.get("repair") or {}).get("run"))
        rec.observe(selected=selected,
                    fork_execution=[item.get("execution")
                                    for item in stored["experiments"].values()],
                    replay_fork=_replay(stack, "fork"),
                    replay_repaired=_replay(stack, "repaired") if repaired else None)
        rec.capture("main", stack)
    return run


def _violations(result: dict) -> bool:
    return bool(result["unpermitted_deliveries"] or result["stale_commitments"])


def _unsafe(result: dict) -> bool:
    return _violations(result) or bool(result["mutated_on_denial"])


def expect_heldout(ev: dict) -> dict:
    """What must be true of any case, whatever it found. None of it depends on the timing."""
    main, seen = ev["campaigns"]["main"], ev["observations"]
    fork, repaired = ev["oracles"]["main"]["fork"], ev["oracles"]["main"].get("repaired")
    violated = _violations(fork)
    complete = all(item["complete"] for item in main["coverage"].values())
    replays = [seen["replay_fork"], *([seen["replay_repaired"]] if seen["replay_repaired"] else [])]
    return {
        "fork_ran_in_the_lab": seen["fork_execution"] == [{"mode": "lab", "guarded": False}],
        "a_violation_reached_a_repair": not violated or repaired is not None,
        "no_repair_without_a_violation": violated or repaired is None,
        "repair_ran_on_the_guarded_boundary": repaired is None or main["repair"]["execution"] == {
            "mode": "enforced", "guarded": True},
        "no_enforced_run_was_unsafe": repaired is None or not _unsafe(repaired),
        "untested_stays_inconclusive": violated or complete or (
            main["outcome"] == "coverage_incomplete" and main["memory_ids"] == 0),
        "tested_and_clean_is_no_failure": violated or not complete
        or main["outcome"] == "no_failure_found",
        "every_capsule_reproduced": all(item["reproduced"] is True for item in replays),
        "replay_called_no_model": all(item["reported_model_calls"] == 0
                                      and item["provider_requests_during_replay"] == 0
                                      for item in replays)}


def held_out_cases(selection: list[dict] | None = None) -> list[Case]:
    return [Case(item["case_id"], "held_out", JOURNEY, _run_selected(item), expect_heldout)
            for item in (select() if selection is None else selection)]


def run_heldout(out: Path, cases: list[Case]) -> None:
    errors = {}
    for case in cases:
        try:
            run_case(case, out)
        except Exception as error:  # noqa: BLE001 - a crash is an errored case, not a dropped one
            errors[case.case_id] = type(error).__name__
    shutil.rmtree(out / "work", ignore_errors=True)
    (out / "errors.json").write_text(json.dumps(errors, indent=1, sort_keys=True), "utf-8")


# --- scoring: files only -----------------------------------------------------------------

def _classify(fork_result: dict | None, complete: bool) -> str:
    if fork_result is None:
        return "errored"
    if _violations(fork_result):
        return "failure_found"
    return "fault_fired_no_violation" if complete else "inconclusive"


def _stored_run(campaign: dict, suffix: str) -> dict | None:
    return next((run for name, run in campaign["runs"].items() if name.endswith(suffix)), None)


def _oracle_view(result: dict) -> dict:
    keys = ("unpermitted_deliveries", "stale_commitments", "guard_denials", "mutated_on_denial",
            "delivered_to", "still_queued", "withdrawn")
    return {key: result[key] for key in keys}


def _repair_row(campaign: dict, stored: dict | None) -> dict | None:
    if stored is None:
        return None
    repair, result = campaign["repair"], journey_oracle(stored["body"])
    return {"campaign_outcome": campaign["outcome"], "verified": repair["verified"],
            "run_status": repair["run_status"], "held_by": repair["held_by"],
            "lost_faults": repair["lost_faults"], "execution": repair["execution"],
            "oracle": _oracle_view(result), "unsafe": _unsafe(result),
            "run_sha256": stored["sha256"]}


def case_row(selected: dict, evidence: dict | None, scored: dict | None, error: str | None) -> dict:
    """One case, from its stored evidence. A verdict here comes from a stored run body."""
    row = {key: selected[key] for key in ("case_id", "adapter", "faults")}
    if evidence is None:
        return {**row, "class": "errored", "error": error, "fork": None, "repair": None,
                "replays": [], "integrity_failures": ["evidence_missing"]}
    campaign = evidence["campaigns"]["main"]
    fork = _stored_run(campaign, "/fork")
    coverage = next(iter(campaign["coverage"].values()), None)
    result = journey_oracle(fork["body"]) if fork else None
    complete = bool(coverage and coverage["complete"])
    seen = evidence["observations"]
    replays = {"fork": seen["replay_fork"], "repaired": seen["replay_repaired"]}
    return {
        **row, "class": _classify(result, complete),
        "campaign": {"status": campaign["status"], "outcome": campaign["outcome"]},
        "fork": None if fork is None else {
            "execution": seen["fork_execution"][0], "status": fork["body"]["status"],
            "coverage": coverage, "oracle": _oracle_view(result),
            "faults_fired": sorted(DEFAULT_WORLDS[JOURNEY].faults_fired(
                {"scenario": {"faults": selected["faults"]}}, fork["body"])),
            "failed_invariants": sorted({item["invariant"] for item in campaign["findings"]
                                         if item["verdict"] == "failed"
                                         and fork["artifact_id"].endswith(item["run_id"])}),
            "run_sha256": fork["sha256"]},
        "repair": _repair_row(campaign, _stored_run(campaign, "repaired")),
        "replays": {name: item for name, item in replays.items() if item is not None},
        "model_attempts": len(evidence["http_attempts"]["main"]),
        "integrity_failures": scored["failed_expectations"]}


def counts(rows: list[dict]) -> dict:
    classes = [row["class"] for row in rows]
    repairs = [row["repair"] for row in rows if row["repair"]]
    incomplete = [item for item in repairs if item["campaign_outcome"] == "repair_incomplete"]
    replays = [item for row in rows if row["replays"] for item in row["replays"].values()]
    failures = [row for row in rows if row["class"] == "failure_found"]
    return {
        "cases": len(rows),
        "failures_found": len(failures),
        "failures_found_with_incomplete_coverage": sum(
            not row["fork"]["coverage"]["complete"] for row in failures),
        "fault_fired_no_violation": classes.count("fault_fired_no_violation"),
        "inconclusive": classes.count("inconclusive"),
        "errored": classes.count("errored"),
        "repairs": {
            "attempted": len(repairs),
            "verified": sum(item["campaign_outcome"] == "repair_verified" for item in repairs),
            "incomplete": len(incomplete),
            "incomplete_with_lost_faults": sum(bool(item["lost_faults"]) for item in incomplete),
            "did_not_hold": sum(item["campaign_outcome"] == "repair_did_not_hold"
                                for item in repairs),
            "held_by_adapter": sum(item["held_by"] == "adapter" for item in repairs),
            "held_by_guarded_boundary": sum(item["held_by"] == "guarded_boundary"
                                            for item in repairs)},
        "unsafe_enforced_runs": sum(item["unsafe"] for item in repairs),
        "replays": {"attempted": len(replays),
                    "reproduced": sum(item["reproduced"] is True for item in replays),
                    "reported_model_calls": sum(item["reported_model_calls"] or 0
                                                for item in replays),
                    "provider_requests": sum(item["provider_requests_during_replay"] or 0
                                             for item in replays)},
        "cases_with_integrity_failures": sum(bool(row["integrity_failures"]) for row in rows)}


def _breakdown(rows: list[dict], key) -> dict:
    groups = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    return {name: {"cases": len(items), **{
        label: sum(row["class"] == label for row in items)
        for label in ("failure_found", "fault_fired_no_violation", "inconclusive", "errored")}}
        for name, items in sorted(groups.items())}


def score_heldout(out: Path, selection: list[dict]) -> dict:
    """Build the report from ``<out>/evidence``. Reads files only, plus the selection."""
    out = Path(out)
    errors_path = out / "errors.json"
    errors = json.loads(errors_path.read_text("utf-8")) if errors_path.exists() else {}
    rows = []
    for selected, case in zip(selection, held_out_cases(selection), strict=True):
        path = out / "evidence" / f"{case.case_id}.json"
        evidence = json.loads(path.read_text("utf-8")) if path.exists() else None
        scored = score_case(case, evidence, out / "telemetry") if evidence else None
        rows.append(case_row(selected, evidence, scored, errors.get(case.case_id)))
    return {
        "schema": SCHEMA, "inference": "mocked", "live_provider_contacted": False,
        "fixture_label": FIXTURE_LABEL, "world": JOURNEY, "seed": SEED,
        "selection_sha256": canonical_sha256(selection),
        "environment": {"python": platform.python_version(), "platform": sys.platform},
        "counts": counts(rows),
        "by_adapter": _breakdown(rows, lambda row: row["adapter"]),
        "by_fault_set": _breakdown(rows, lambda row: "+".join(
            sorted(fault["fault_id"] for fault in row["faults"]))),
        "cases": rows}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", "utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--write-rule", action="store_true")
    parser.add_argument("--record", action="store_true", help="Write heldout_result.json")
    args = parser.parse_args(argv)
    if args.write_rule:
        rule = build_rule()
        write_json(RULE_PATH, rule)
        print(json.dumps({key: rule[key] for key in ("case_count", "selection_sha256", "seed")}))
        return 0
    if args.out is None:
        parser.error("--out is required unless --write-rule is given")
    os.environ.setdefault("LANGSMITH_TRACING", "false")
    rule = json.loads(RULE_PATH.read_text("utf-8"))
    if canonical_sha256(rule["cases"]) != rule["selection_sha256"]:
        parser.exit(2, "heldout_rule.json doesn't match its digest\n")
    args.out.mkdir(parents=True, exist_ok=True)
    run_heldout(args.out, held_out_cases(rule["cases"]))
    report = score_heldout(args.out, rule["cases"])
    write_json(args.out / "report.json", report)
    if args.record:
        write_json(RESULT_PATH, report)
    print(json.dumps({"counts": report["counts"], "by_adapter": report["by_adapter"],
                      "by_fault_set": report["by_fault_set"]}, indent=2))
    return 1 if report["counts"]["cases_with_integrity_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
