"""Code-only check registry with pinned versions and param bounds.

Checks are registered by trusted checked-in code only (core drivers and
example drivers). There is no client, model or browser path to register or
redefine a check: :func:`register_check` takes a live ``interpret`` callable,
so a JSON-shaped payload can never supply one, and registering an id twice
fails instead of overwriting.

Each check declares the evidence it needs (a subset of ``snapshot``/``trace``),
the environments it applies to, whether it is ``stateless`` or ``historical``,
and a ``params_spec`` of ``{name: (type, min, max)}`` bounds. :func:`evaluate`
rejects unknown ids and out-of-bounds params, and returns ``unknown`` (never
``pass``) when required evidence is missing.
"""

import re

CHECK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PARAM_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EVIDENCE_KINDS = ("snapshot", "trace")
TEMPORALS = ("stateless", "historical")
VERDICTS = ("pass", "violation", "unknown", "not-applicable")
_PARAM_TYPES = (int, float, str, bool)

_registry: dict = {}


class CheckError(ValueError):
    """Unknown check, bad params, or bad check registration."""


def _check_params_spec(spec) -> dict:
    if type(spec) is not dict:
        raise CheckError("params_spec must be {name: (type, min, max)}")
    cleaned = {}
    for name, entry in spec.items():
        if type(name) is not str or not PARAM_NAME_PATTERN.match(name):
            raise CheckError(f"invalid param name {name!r}")
        if type(entry) is not tuple or len(entry) != 3:
            raise CheckError(f"param {name!r} must be (type, min, max)")
        kind, low, high = entry
        if kind not in _PARAM_TYPES:
            raise CheckError(
                f"param {name!r} type must be one of int, float, str, bool")
        if kind is bool:
            if low is not None or high is not None:
                raise CheckError(
                    f"param {name!r}: bool params take no bounds")
        elif kind is str:
            for bound in (low, high):
                if bound is not None and (
                        type(bound) is not int or bound < 0):
                    raise CheckError(
                        f"param {name!r}: str bounds are lengths >= 0")
            if low is not None and high is not None and low > high:
                raise CheckError(f"param {name!r}: min exceeds max")
        else:
            for bound in (low, high):
                if bound is not None and (
                        type(bound) not in (int, float)):
                    raise CheckError(
                        f"param {name!r}: numeric bounds need int/float")
            if low is not None and high is not None and low > high:
                raise CheckError(f"param {name!r}: min exceeds max")
        cleaned[name] = (kind, low, high)
    return cleaned


def register_check(*, check_id: str, version: str, description: str,
                   params_spec: dict, required_evidence,
                   applies_to, temporal: str, interpret) -> None:
    """Register one named check (code-only; ids are never redefined)."""
    if type(check_id) is not str or not CHECK_ID_PATTERN.match(check_id):
        raise CheckError(f"invalid check_id {check_id!r}")
    if check_id in _registry:
        raise CheckError(f"check {check_id!r} is already registered;"
                         " checks are never redefined")
    if type(version) is not str or not version or len(version) > 64:
        raise CheckError(f"invalid version for check {check_id!r}")
    if type(description) is not str or not description \
            or len(description) > 500:
        raise CheckError(f"invalid description for check {check_id!r}")
    spec = _check_params_spec(params_spec)
    if not isinstance(required_evidence, (list, tuple, set, frozenset)) \
            or any(e not in EVIDENCE_KINDS for e in required_evidence):
        raise CheckError(f"check {check_id!r}: required_evidence must be"
                         f" a subset of {list(EVIDENCE_KINDS)}")
    if type(applies_to) is str:
        envs = (applies_to,)
    elif isinstance(applies_to, (list, tuple, set, frozenset)) \
            and applies_to:
        envs = tuple(applies_to)
    else:
        raise CheckError(f"check {check_id!r}: applies_to must be an"
                         " env id or a non-empty collection of env ids")
    for env in envs:
        if type(env) is not str or not env or len(env) > 64:
            raise CheckError(f"check {check_id!r}: invalid applies_to"
                             f" env {env!r}")
    if temporal not in TEMPORALS:
        raise CheckError(f"check {check_id!r}: temporal must be one of"
                         f" {list(TEMPORALS)}")
    if not callable(interpret):
        raise CheckError(f"check {check_id!r}: interpret must be a"
                         " code callable (checks are code-only; a"
                         " client/model payload cannot register one)")
    _registry[check_id] = {
        "check_id": check_id, "version": version,
        "description": description, "params_spec": spec,
        "required_evidence": tuple(sorted(set(required_evidence))),
        "applies_to": envs, "temporal": temporal,
        "interpret": interpret,
    }


def get_check(check_id: str) -> dict:
    """Return a copy of one check's metadata (without its interpret)."""
    entry = _registry.get(check_id)
    if entry is None:
        raise CheckError(f"unknown check {check_id!r}")
    return {
        "check_id": entry["check_id"], "version": entry["version"],
        "description": entry["description"],
        "params_spec": dict(entry["params_spec"]),
        "required_evidence": entry["required_evidence"],
        "applies_to": entry["applies_to"],
        "temporal": entry["temporal"],
    }


def _validate_params(spec: dict, params) -> dict:
    if params is None:
        params = {}
    if type(params) is not dict:
        raise CheckError("params must be an object")
    for name in params:
        if name not in spec:
            raise CheckError(f"unknown param {name!r}")
    cleaned = {}
    for name, (kind, low, high) in spec.items():
        if name not in params:
            raise CheckError(f"missing param {name!r}")
        value = params[name]
        if kind is int:
            if type(value) is not int:
                raise CheckError(f"param {name!r} must be int")
            if (low is not None and value < low) \
                    or (high is not None and value > high):
                raise CheckError(f"param {name!r} out of bounds"
                                 f" [{low}, {high}]")
        elif kind is float:
            if type(value) not in (int, float):
                raise CheckError(f"param {name!r} must be float")
            if (low is not None and value < low) \
                    or (high is not None and value > high):
                raise CheckError(f"param {name!r} out of bounds"
                                 f" [{low}, {high}]")
        elif kind is str:
            if type(value) is not str:
                raise CheckError(f"param {name!r} must be str")
            if (low is not None and len(value) < low) \
                    or (high is not None and len(value) > high):
                raise CheckError(f"param {name!r} length out of bounds"
                                 f" [{low}, {high}]")
        else:
            if type(value) is not bool:
                raise CheckError(f"param {name!r} must be bool")
        cleaned[name] = value
    return cleaned


def evaluate(check_id: str, *, snapshot=None, trace=None, params=None,
             fixture=None) -> dict:
    """Evaluate one registered check against controller-owned evidence.

    Unknown ids and out-of-bounds params raise :class:`CheckError`. Missing
    required evidence returns ``unknown``, never ``pass``.
    """
    entry = _registry.get(check_id)
    if entry is None:
        raise CheckError(f"unknown check {check_id!r}")
    cleaned = _validate_params(entry["params_spec"], params)
    provided = {"snapshot": snapshot is not None,
                "trace": trace is not None}
    missing = [kind for kind in entry["required_evidence"]
               if not provided[kind]]
    if missing:
        return {"verdict": "unknown",
                "detail": "missing required evidence:"
                          f" {', '.join(missing)}",
                "evidence_refs": []}
    result = entry["interpret"](snapshot=snapshot, trace=trace,
                                params=cleaned, fixture=fixture)
    if type(result) is not dict or set(result) != {
            "verdict", "detail", "evidence_refs"}:
        raise CheckError(f"check {check_id!r}: interpret must return"
                         " exactly verdict, detail and evidence_refs")
    if result["verdict"] not in VERDICTS:
        raise CheckError(f"check {check_id!r}: invalid verdict"
                         f" {result['verdict']!r}")
    if type(result["detail"]) is not str:
        raise CheckError(f"check {check_id!r}: detail must be str")
    if type(result["evidence_refs"]) is not list:
        raise CheckError(f"check {check_id!r}: evidence_refs must be a list")
    return {"verdict": result["verdict"], "detail": result["detail"],
            "evidence_refs": list(result["evidence_refs"])}
