"""Fidelity assessment report: manifest + divergence + determinism evidence.

Track T3. Assembles one fidelity manifest, any number of ``diverge``
comparison outputs and ``fidelity_probe`` outputs, plus operator-assessed
lists, into a single assessed document. There is deliberately NO universal
score: behaviors modeled, approximated, unsupported, determinism limits,
reference comparisons and known mismatches stay separate facts. Validation is
strict and pure; unknown fields are rejected at every level.
"""

from copy import deepcopy

from .manifests import canonical_digest, validate_fidelity_manifest

REPORT_VERSION = 1
MAX_LIST_ITEMS = 64
MAX_TEXT_LEN = 500
MAX_COMPARISONS = 32
MAX_PROBES = 32

_COMPARISON_KEYS = {"comparable", "reason", "diverged",
                    "first_divergence_step", "diffs"}
_DIFF_KEYS = {"step", "kind", "action", "detail"}
_PROBE_KEYS = {"deterministic", "replays", "steps",
               "first_nondeterminism_step", "transitions",
               "final_snapshot_digest"}
_TRANSITION_KEYS = {"step", "tool", "outcome", "snapshot_digest"}


def _check_text_list(name: str, value) -> list:
    if type(value) not in (list, tuple):
        raise ValueError(f"report {name} must be a list of strings")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"report {name} lists at most {MAX_LIST_ITEMS}")
    for entry in value:
        if type(entry) is not str or not entry \
                or len(entry) > MAX_TEXT_LEN:
            raise ValueError(f"report {name} entries must be non-empty"
                             f" strings up to {MAX_TEXT_LEN} chars")
    return list(value)


def _check_comparison(entry, index: int) -> dict:
    where = f"comparisons[{index}]"
    if type(entry) is not dict or set(entry) != _COMPARISON_KEYS:
        raise ValueError(f"{where} must carry exactly the compare_runs"
                         " fields; unknown fields are rejected")
    if type(entry["comparable"]) is not bool:
        raise ValueError(f"{where}.comparable must be boolean")
    reason = entry["reason"]
    if reason is not None and (type(reason) is not str or not reason
                               or len(reason) > 2000):
        raise ValueError(f"{where}.reason must be null or a short string")
    if type(entry["diverged"]) is not bool:
        raise ValueError(f"{where}.diverged must be boolean")
    first = entry["first_divergence_step"]
    if first is not None and (type(first) is not int or first < 1):
        raise ValueError(f"{where}.first_divergence_step must be null or >= 1")
    diffs = entry["diffs"]
    if type(diffs) is not list or len(diffs) > 50:
        raise ValueError(f"{where}.diffs must list at most 50 entries")
    for pos, diff in enumerate(diffs):
        if type(diff) is not dict or set(diff) != _DIFF_KEYS:
            raise ValueError(f"{where}.diffs[{pos}] must carry exactly"
                             " step, kind, action and detail")
        if type(diff["step"]) is not int or diff["step"] < 1:
            raise ValueError(f"{where}.diffs[{pos}].step must be >= 1")
        for key in ("kind", "action", "detail"):
            if type(diff[key]) is not str or not diff[key] \
                    or len(diff[key]) > 2000:
                raise ValueError(f"{where}.diffs[{pos}].{key} must be a"
                                 " short non-empty string")
    return deepcopy(entry)


def _check_probe(entry, index: int) -> dict:
    where = f"probes[{index}]"
    if type(entry) is not dict or set(entry) != _PROBE_KEYS:
        raise ValueError(f"{where} must carry exactly the fidelity_probe"
                         " fields; unknown fields are rejected")
    if type(entry["deterministic"]) is not bool:
        raise ValueError(f"{where}.deterministic must be boolean")
    for key in ("replays", "steps"):
        if type(entry[key]) is not int or entry[key] < 0:
            raise ValueError(f"{where}.{key} must be an int >= 0")
    first = entry["first_nondeterminism_step"]
    if first is not None and (type(first) is not int or first < 1):
        raise ValueError(f"{where}.first_nondeterminism_step must be null"
                         " or >= 1")
    transitions = entry["transitions"]
    if type(transitions) is not list or len(transitions) > 100000:
        raise ValueError(f"{where}.transitions must be a list")
    for pos, transition in enumerate(transitions):
        if type(transition) is not dict \
                or set(transition) != _TRANSITION_KEYS:
            raise ValueError(f"{where}.transitions[{pos}] must carry exactly"
                             " step, tool, outcome and snapshot_digest")
        if type(transition["step"]) is not int or transition["step"] < 1:
            raise ValueError(f"{where}.transitions[{pos}].step must be >= 1")
        for key in ("tool", "outcome", "snapshot_digest"):
            if type(transition[key]) is not str \
                    or not transition[key] \
                    or len(transition[key]) > 500:
                raise ValueError(f"{where}.transitions[{pos}].{key} must be"
                                 " a short non-empty string")
    digest = entry["final_snapshot_digest"]
    if type(digest) is not str or not digest or len(digest) > 500:
        raise ValueError(f"{where}.final_snapshot_digest must be a short"
                         " non-empty string")
    return deepcopy(entry)


def assemble_fidelity_report(env_id: str, *, manifest, comparisons=(),
                             probes=(), notes=(), behaviors_modeled=(),
                             approximated=(), unsupported_ops=(),
                             unsupported_faults=(),
                             known_mismatches=()) -> dict:
    """Assemble manifest + divergence + determinism evidence into one doc.

    Returns ``behaviors_modeled / approximated / unsupported_ops /
    unsupported_faults / determinism / comparisons / known_mismatches /
    notes`` plus ``assessed_by/at`` taken from the manifest. No universal
    score exists anywhere in the output. Unknown keyword arguments are
    rejected by the signature; unknown fields inside every input are rejected
    by validation.
    """
    if type(env_id) is not str or not env_id:
        raise ValueError("report env_id must be a non-empty string")
    validated = validate_fidelity_manifest(manifest)
    if validated["env_id"] != env_id:
        raise ValueError("report env_id does not match the manifest env_id")
    if type(comparisons) not in (list, tuple) \
            or len(comparisons) > MAX_COMPARISONS:
        raise ValueError("comparisons must list at most"
                         f" {MAX_COMPARISONS} compare_runs outputs")
    if type(probes) not in (list, tuple) or len(probes) > MAX_PROBES:
        raise ValueError("probes must list at most"
                         f" {MAX_PROBES} fidelity_probe outputs")
    checked_comparisons = [_check_comparison(entry, index)
                           for index, entry in enumerate(comparisons)]
    checked_probes = [_check_probe(entry, index)
                      for index, entry in enumerate(probes)]
    determinism = [{
        "deterministic": probe["deterministic"],
        "replays": probe["replays"], "steps": probe["steps"],
        "first_nondeterminism_step": probe["first_nondeterminism_step"],
        "final_snapshot_digest": probe["final_snapshot_digest"]}
        for probe in checked_probes]
    return {"report_version": REPORT_VERSION, "env_id": env_id,
            "manifest_digest": canonical_digest(validated),
            "behaviors_modeled": _check_text_list("behaviors_modeled",
                                                  behaviors_modeled),
            "approximated": _check_text_list("approximated", approximated),
            "unsupported_ops": _check_text_list("unsupported_ops",
                                                unsupported_ops),
            "unsupported_faults": _check_text_list("unsupported_faults",
                                                   unsupported_faults),
            "determinism": determinism,
            "comparisons": checked_comparisons,
            "known_mismatches": _check_text_list("known_mismatches",
                                                 known_mismatches),
            "notes": _check_text_list("notes", notes),
            "assessed_by": validated["assessed_by"],
            "assessed_at": validated["assessed_at"]}
