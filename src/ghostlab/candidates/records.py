"""Versioned candidate, evaluation and promotion records (pure, stdlib only).

A *candidate* proposes one controlled improvement: a parent version, a
proposed version, the exact diff between them, config/code identity,
evidence references, and a lifecycle status with a rejection reason.
An *evaluation* records the multidimensional outcome of running parent
vs candidate (original failing scenario, development regressions,
completion, fault coverage, usage) with no aggregate verdict hiding
the dimensions. A *promotion* binds a candidate to one single-use
human approval. Unknown fields are rejected at every level; nothing
is silently ignored. Standard library only.
"""

import hashlib
import json

from ..experiments.configs import (ConfigError, canonical_digest,
                                   check_project_id as _check_pid,
                                   scan_for_secrets as _scan)


def check_project_id(project_id: str) -> str:
    try:
        return _check_pid(project_id)
    except ConfigError as error:
        raise CandidateError(str(error)) from None


def scan_for_secrets(node, path: str = "record") -> None:
    try:
        _scan(node, path)
    except ConfigError as error:
        raise CandidateError(str(error)) from None

CANDIDATE_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 262_144
MAX_DIFF_ENTRIES = 64
MAX_EVIDENCE_REFS = 32
MAX_REGRESSIONS = 32
MAX_CHECKS = 64

CANDIDATE_MODES = ("developer_supplied", "generated_config")
CANDIDATE_STATUSES = ("proposed", "validated", "evaluated",
                      "promotion_requested", "promoted", "rejected")
EVALUATION_KINDS = ("run", "finding", "capsule", "approval", "evaluation",
                    "task")
PROMOTION_STATUSES = ("pending", "completed", "rejected", "expired")

_CANDIDATE_KEYS = {"schema_version", "candidate_id", "project_id", "mode",
                   "parent", "proposed", "diff", "evidence_refs", "status",
                   "rejection", "created_by", "created_at", "digest"}
_ENDPOINT_KEYS = {"target_id", "target_version", "code_identity",
                  "config_digest"}
_DIFF_KEYS = {"kind", "path", "before", "after"}
_REF_KEYS = {"kind", "id", "sha256"}
_REJECTION_KEYS = {"code", "message", "rejected_by", "rejected_at"}
_EVALUATION_KEYS = {"schema_version", "evaluation_id", "candidate_id",
                    "project_id", "original", "regressions", "completion",
                    "fault_coverage", "usage", "evaluated_by",
                    "evaluated_at", "digest"}
_CASE_KEYS = {"case_id", "scenario_ref", "parent", "candidate"}
_SCENARIO_REF_KEYS = {"fixture", "scenario", "actor_id"}
_ATTEMPT_KEYS = {"status", "tool_calls", "stopped", "denial", "checks"}
_CHECK_KEYS = {"name", "passed"}
_COMPLETION_KEYS = {"parent", "candidate"}
_COMPLETION_SIDE_KEYS = {"status", "tool_calls", "stopped"}
_FAULT_KEYS = {"scenario_faults", "parent", "candidate"}
_FAULT_SIDE_KEYS = {"faults_fired"}
_USAGE_KEYS = {"cap", "parent", "candidate"}
_USAGE_SIDE_KEYS = {"tool_calls"}
_PROMOTION_KEYS = {"schema_version", "promotion_id", "candidate_id",
                   "project_id", "approval_id", "action_digest", "status",
                   "decided_by", "decided_at", "digest"}


class CandidateError(ValueError):
    """A candidate, evaluation or promotion record is invalid."""


def _short(value, label: str, maximum=200) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise CandidateError(f"invalid {label}")
    return value


def _digest_or_none(value, label: str):
    if value is None:
        return None
    if (type(value) is not str or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)):
        raise CandidateError(f"invalid {label}")
    return value


def _jsonable(value, label: str):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise CandidateError(f"{label} must be JSON") from None
    return value


def candidate_id(project_id: str, stable: dict) -> str:
    """Content-addressed id, so identical proposes are idempotent per project."""
    return "cand-" + canonical_digest(
        {"project_id": project_id, "candidate": stable})[:16]


def evaluation_id(project_id: str, candidate: str, stable: dict) -> str:
    return "eval-" + canonical_digest(
        {"project_id": project_id, "candidate_id": candidate,
         "evaluation": stable})[:16]


def promotion_id(project_id: str, candidate: str, approval: str) -> str:
    return "promo-" + canonical_digest(
        {"project_id": project_id, "candidate_id": candidate,
         "approval_id": approval})[:16]


def _check_endpoint(node, label: str) -> dict:
    if type(node) is not dict or set(node) != _ENDPOINT_KEYS:
        raise CandidateError(f"{label} must carry exactly the v1 endpoint fields")
    return {
        "target_id": _short(node["target_id"], f"{label}.target_id"),
        "target_version": _short(node["target_version"], f"{label}.target_version"),
        "code_identity": _short(node["code_identity"], f"{label}.code_identity",
                                maximum=64),
        "config_digest": _digest_or_none(node["config_digest"],
                                         f"{label}.config_digest"),
    }


def _check_diff(diff) -> list:
    if type(diff) is not list or not 1 <= len(diff) <= MAX_DIFF_ENTRIES:
        raise CandidateError("diff holds 1 to 64 entries")
    seen, clean = set(), []
    for index, entry in enumerate(diff):
        where = f"diff[{index}]"
        if type(entry) is not dict or set(entry) != _DIFF_KEYS:
            raise CandidateError(f"{where} must carry exactly kind, path, before, after")
        kind = entry["kind"]
        if kind not in ("target", "field", "code"):
            raise CandidateError(f"invalid {where}.kind")
        path = _short(entry["path"], f"{where}.path")
        if (kind, path) in seen:
            raise CandidateError(f"duplicate diff entry '{kind}:{path}'")
        seen.add((kind, path))
        clean.append({"kind": kind, "path": path,
                      "before": _jsonable(entry["before"], f"{where}.before"),
                      "after": _jsonable(entry["after"], f"{where}.after")})
    return clean


def _check_refs(refs) -> list:
    if type(refs) is not list or len(refs) > MAX_EVIDENCE_REFS:
        raise CandidateError("evidence_refs holds at most 32 references")
    clean = []
    for index, ref in enumerate(refs):
        where = f"evidence_refs[{index}]"
        if type(ref) is not dict or set(ref) != _REF_KEYS:
            raise CandidateError(f"{where} must carry exactly kind, id, sha256")
        if ref["kind"] not in EVALUATION_KINDS:
            raise CandidateError(f"invalid {where}.kind")
        clean.append({"kind": ref["kind"], "id": _short(ref["id"], f"{where}.id"),
                      "sha256": _digest_or_none(ref["sha256"], f"{where}.sha256")})
    return clean


def _check_rejection(node):
    if node is None:
        return None
    if type(node) is not dict or set(node) != _REJECTION_KEYS:
        raise CandidateError("rejection must carry exactly code, message, by, at")
    return {"code": _short(node["code"], "rejection.code", maximum=64),
            "message": _short(node["message"], "rejection.message", maximum=500),
            "rejected_by": _short(node["rejected_by"], "rejection.rejected_by"),
            "rejected_at": _short(node["rejected_at"], "rejection.rejected_at")}


def validate_candidate(obj) -> dict:
    """Validate a stored candidate record (pure)."""
    if type(obj) is not dict or set(obj) != _CANDIDATE_KEYS:
        raise CandidateError("candidate must carry exactly the v1 fields")
    if obj["schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise CandidateError("unsupported candidate schema_version")
    if obj["mode"] not in CANDIDATE_MODES:
        raise CandidateError("invalid candidate mode")
    if obj["status"] not in CANDIDATE_STATUSES:
        raise CandidateError("invalid candidate status")
    body = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_id": _short(obj["candidate_id"], "candidate_id"),
        "project_id": check_project_id(obj["project_id"]),
        "mode": obj["mode"],
        "parent": _check_endpoint(obj["parent"], "parent"),
        "proposed": _check_endpoint(obj["proposed"], "proposed"),
        "diff": _check_diff(obj["diff"]),
        "evidence_refs": _check_refs(obj["evidence_refs"]),
        "status": obj["status"],
        "rejection": _check_rejection(obj["rejection"]),
        "created_by": _short(obj["created_by"], "created_by"),
        "created_at": _short(obj["created_at"], "created_at"),
    }
    if (body["status"] == "rejected") != (body["rejection"] is not None):
        raise CandidateError("rejection is present exactly when status is rejected")
    if type(obj["digest"]) is not str or not obj["digest"]:
        raise CandidateError("invalid digest")
    body["digest"] = obj["digest"]
    scan_for_secrets({k: v for k, v in body.items() if k != "digest"},
                     "candidate")
    return body


def _check_attempt(node, label: str) -> dict:
    if type(node) is not dict or set(node) != _ATTEMPT_KEYS:
        raise CandidateError(f"{label} must carry exactly the v1 attempt fields")
    if node["status"] not in ("completed", "budget_exhausted"):
        raise CandidateError(f"invalid {label}.status")
    if type(node["tool_calls"]) is not int or node["tool_calls"] < 0:
        raise CandidateError(f"invalid {label}.tool_calls")
    stopped = node["stopped"]
    if stopped is not None and (type(stopped) is not str or not stopped):
        raise CandidateError(f"invalid {label}.stopped")
    denial = node["denial"]
    if denial is not None:
        if type(denial) is not dict or set(denial) != {"code", "reason"}:
            raise CandidateError(f"invalid {label}.denial")
        denial = {"code": _short(denial["code"], f"{label}.denial.code", maximum=64),
                  "reason": _short(denial["reason"], f"{label}.denial.reason",
                                   maximum=500)}
    checks = node["checks"]
    if type(checks) is not list or len(checks) > MAX_CHECKS:
        raise CandidateError(f"invalid {label}.checks")
    clean_checks = []
    for item in checks:
        if type(item) is not dict or set(item) != _CHECK_KEYS:
            raise CandidateError(f"invalid {label}.checks entry")
        if type(item["passed"]) is not bool:
            raise CandidateError(f"invalid {label}.checks verdict")
        clean_checks.append({"name": _short(item["name"], f"{label}.check.name"),
                             "passed": item["passed"]})
    return {"status": node["status"], "tool_calls": node["tool_calls"],
            "stopped": stopped, "denial": denial, "checks": clean_checks}


def _check_case(node, label: str) -> dict:
    if type(node) is not dict or set(node) != _CASE_KEYS:
        raise CandidateError(f"{label} must carry exactly case, scenario and sides")
    ref = node["scenario_ref"]
    if type(ref) is not dict or set(ref) != _SCENARIO_REF_KEYS:
        raise CandidateError(f"invalid {label}.scenario_ref")
    return {
        "case_id": _short(node["case_id"], f"{label}.case_id"),
        "scenario_ref": {
            "fixture": _jsonable(ref["fixture"], f"{label}.fixture"),
            "scenario": _jsonable(ref["scenario"], f"{label}.scenario"),
            "actor_id": _short(ref["actor_id"], f"{label}.actor_id"),
        },
        "parent": _check_attempt(node["parent"], f"{label}.parent"),
        "candidate": _check_attempt(node["candidate"], f"{label}.candidate"),
    }


def validate_evaluation(obj) -> dict:
    """Validate a stored evaluation record (pure).

    Every dimension is required and carries its own per-side detail; there
    is no top-level aggregate verdict, so no single badge can hide a
    failing dimension.
    """
    if type(obj) is not dict or set(obj) != _EVALUATION_KEYS:
        raise CandidateError("evaluation must carry exactly the v1 fields")
    if obj["schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise CandidateError("unsupported evaluation schema_version")
    regressions = obj["regressions"]
    if type(regressions) is not list or len(regressions) > MAX_REGRESSIONS:
        raise CandidateError("regressions holds at most 32 cases")
    completion = obj["completion"]
    if type(completion) is not dict or set(completion) != _COMPLETION_KEYS:
        raise CandidateError("completion must carry exactly parent and candidate")
    clean_completion = {}
    for side in ("parent", "candidate"):
        node = completion[side]
        if type(node) is not dict or set(node) != _COMPLETION_SIDE_KEYS:
            raise CandidateError(f"invalid completion.{side}")
        if node["status"] not in ("completed", "budget_exhausted"):
            raise CandidateError(f"invalid completion.{side}.status")
        if type(node["tool_calls"]) is not int or node["tool_calls"] < 0:
            raise CandidateError(f"invalid completion.{side}.tool_calls")
        if node["stopped"] is not None and (
                type(node["stopped"]) is not str or not node["stopped"]):
            raise CandidateError(f"invalid completion.{side}.stopped")
        clean_completion[side] = {"status": node["status"],
                                  "tool_calls": node["tool_calls"],
                                  "stopped": node["stopped"]}
    faults = obj["fault_coverage"]
    if type(faults) is not dict or set(faults) != _FAULT_KEYS:
        raise CandidateError("fault_coverage must carry scenario faults and sides")
    if type(faults["scenario_faults"]) is not list or any(
            type(name) is not str for name in faults["scenario_faults"]):
        raise CandidateError("invalid fault_coverage.scenario_faults")
    clean_faults = {"scenario_faults": list(faults["scenario_faults"])}
    for side in ("parent", "candidate"):
        node = faults[side]
        if type(node) is not dict or set(node) != _FAULT_SIDE_KEYS:
            raise CandidateError(f"invalid fault_coverage.{side}")
        fired = node["faults_fired"]
        if type(fired) is not list:
            raise CandidateError(f"invalid fault_coverage.{side}.faults_fired")
        clean_side = []
        for entry in fired:
            bad_shape = (type(entry) is not dict
                         or set(entry) != {"case_id", "fired"})
            bad_case = (not bad_shape
                        and (type(entry["case_id"]) is not str
                             or not entry["case_id"]))
            bad_fired = (not bad_shape
                         and (type(entry["fired"]) is not list
                              or any(type(name) is not str
                                     for name in entry["fired"])))
            if bad_shape or bad_case or bad_fired:
                raise CandidateError(
                    f"invalid fault_coverage.{side}.faults_fired entry")
            clean_side.append({"case_id": entry["case_id"],
                               "fired": list(entry["fired"])})
        clean_faults[side] = {"faults_fired": clean_side}
    usage = obj["usage"]
    if type(usage) is not dict or set(usage) != _USAGE_KEYS:
        raise CandidateError("usage must carry exactly cap, parent and candidate")
    if type(usage["cap"]) is not int or not 1 <= usage["cap"] <= 1000:
        raise CandidateError("invalid usage.cap")
    clean_usage = {"cap": usage["cap"]}
    for side in ("parent", "candidate"):
        node = usage[side]
        if type(node) is not dict or set(node) != _USAGE_SIDE_KEYS:
            raise CandidateError(f"invalid usage.{side}")
        if type(node["tool_calls"]) is not int or node["tool_calls"] < 0:
            raise CandidateError(f"invalid usage.{side}.tool_calls")
        clean_usage[side] = {"tool_calls": node["tool_calls"]}
    body = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "evaluation_id": _short(obj["evaluation_id"], "evaluation_id"),
        "candidate_id": _short(obj["candidate_id"], "candidate_id"),
        "project_id": check_project_id(obj["project_id"]),
        "original": _check_case(obj["original"], "original"),
        "regressions": [_check_case(item, f"regressions[{i}]")
                        for i, item in enumerate(regressions)],
        "completion": clean_completion,
        "fault_coverage": clean_faults,
        "usage": clean_usage,
        "evaluated_by": _short(obj["evaluated_by"], "evaluated_by"),
        "evaluated_at": _short(obj["evaluated_at"], "evaluated_at"),
    }
    if type(obj["digest"]) is not str or not obj["digest"]:
        raise CandidateError("invalid digest")
    body["digest"] = obj["digest"]
    scan_for_secrets({k: v for k, v in body.items() if k != "digest"},
                     "evaluation")
    return body


def validate_promotion(obj) -> dict:
    """Validate a stored promotion record (pure)."""
    if type(obj) is not dict or set(obj) != _PROMOTION_KEYS:
        raise CandidateError("promotion must carry exactly the v1 fields")
    if obj["schema_version"] != CANDIDATE_SCHEMA_VERSION:
        raise CandidateError("unsupported promotion schema_version")
    if obj["status"] not in PROMOTION_STATUSES:
        raise CandidateError("invalid promotion status")
    decided_by = obj["decided_by"]
    if decided_by is not None and (
            type(decided_by) is not str or not decided_by):
        raise CandidateError("invalid decided_by")
    decided_at = obj["decided_at"]
    if decided_at is not None and (
            type(decided_at) is not str or not decided_at):
        raise CandidateError("invalid decided_at")
    if (obj["status"] == "pending") == (decided_by is not None):
        raise CandidateError("a pending promotion has no decider; a decided one names one")
    body = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "promotion_id": _short(obj["promotion_id"], "promotion_id"),
        "candidate_id": _short(obj["candidate_id"], "candidate_id"),
        "project_id": check_project_id(obj["project_id"]),
        "approval_id": _short(obj["approval_id"], "approval_id"),
        "action_digest": _short(obj["action_digest"], "action_digest", maximum=64),
        "status": obj["status"],
        "decided_by": decided_by,
        "decided_at": decided_at,
    }
    if type(obj["digest"]) is not str or not obj["digest"]:
        raise CandidateError("invalid digest")
    body["digest"] = obj["digest"]
    return body


def digest_of(body: dict) -> str:
    """sha256 over the canonical body, excluding its own digest field."""
    return hashlib.sha256(json.dumps(
        {k: v for k, v in body.items() if k != "digest"},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


__all__ = [
    "CANDIDATE_MODES", "CANDIDATE_SCHEMA_VERSION", "CANDIDATE_STATUSES",
    "MAX_RECORD_BYTES", "PROMOTION_STATUSES", "CandidateError",
    "candidate_id", "digest_of", "evaluation_id", "promotion_id",
    "validate_candidate", "validate_evaluation", "validate_promotion",
]
