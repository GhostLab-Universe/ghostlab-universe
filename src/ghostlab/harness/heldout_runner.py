"""Generic held-out runner plus the versioned result contract.

The hidden scenario set lives with the held-out owner and is NEVER authored,
bundled or inspected here: this module only defines the scenario-directory
plus manifest shape it consumes at run time, the frozen-config handshake that
pins a run, and the result contract every run emits. The only scenarios
shipped alongside are the clearly-labeled SYNTHETIC self-test fixture in
``selftest_fixture/`` (namespace ``synthetic-selftest``), which exercises the
verdict pipeline end to end without touching hidden content.

Execution drives the existing deterministic entry points only --
:func:`ghostlab.explore.harness.run_campaign` for the fixed-scenario arm and
:func:`ghostlab.verify.assess` over :func:`ghostlab.verify.refund_evidence`.
No inference, no network, no services: standard library only.

Separation contract: a candidate (the frozen target under test) receives ONLY
a case id plus an opaque :class:`HeldoutHandle`. Scenario content -- fixture,
fault schedule, actor binding, scripts, expectations -- never crosses that
boundary; per-case results carry verdicts plus evidence digests, never
scenario content.
"""

import hashlib
import hmac
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ghostlab.adapters.refund_env import FIXTURE_NAME, RefundEnvironment
from ghostlab.core import Fixture, Scenario
from ghostlab.explore.harness import (FIXED_ARM, AttemptSpec, ScriptedTarget,
                                      run_campaign)
from ghostlab.extensions.environment import Action
from ghostlab.extensions.registry import check_target
from ghostlab.extensions.target import TargetTask
from ghostlab.verify import (CATALOG, HELDOUT_RESULT_VERSION, HELDOUT_RUNNER_VERSION,
                             REFUND_RULES, RULES_VERSION, assess, freeze,
                             refund_completion, refund_evidence)

HELDOUT_MANIFEST_VERSION = "ghostlab-heldout-manifest/1"
HELDOUT_SPEC_VERSION = "ghostlab-heldout-spec/1"
HELDOUT_CONFIG_VERSION = "ghostlab-heldout-config/1"
HELDOUT_HANDSHAKE_VERSION = "ghostlab-heldout-handshake/1"

HELDOUT_NAMESPACE = "heldout"
SYNTHETIC_NAMESPACE = "synthetic-selftest"
SYNTHETIC_PREFIX = "SYNTH-"

REFUND_WORLD_ID = "refund"
EVIDENCE_KIND = "sha256-trace-state/1"

VERDICT_PASS = "pass"
VERDICT_VIOLATION = "violation"
VERDICT_UNKNOWN = "unknown"
_VERDICTS = (VERDICT_PASS, VERDICT_VIOLATION, VERDICT_UNKNOWN)

_MANIFEST_MAX_BYTES = 1_000_000
_SPEC_MAX_BYTES = 65_536
_MAX_CASES = 1000
_MAX_SCRIPT_ACTIONS = 1000

_CONFIG_KEYS = frozenset({"config_version", "namespace", "seed", "max_attempts",
                          "action_cap"})
_HANDSHAKE_KEYS = frozenset({"handshake_version", "config", "config_digest"})
_MANIFEST_KEYS = frozenset({"manifest_version", "namespace", "cases"})
_CASE_KEYS = frozenset({"case_id", "spec"})
_SPEC_BASE_KEYS = frozenset({"spec_version", "world", "scenario"})
_SPEC_OPTIONAL_KEYS = frozenset({"fixture", "actor_id", "task_id"})
_SCRIPT_KEY = "candidate_script"
_SCRIPT_ACTION_KEYS = frozenset({"tool", "arguments"})
_CASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}")

_REFUND_TOOLS = frozenset({"get_order", "issue_refund", "get_refund_status"})


class HeldoutRunnerError(ValueError):
    """Base error: the harness refused to run, load or validate."""


class HeldoutManifestError(HeldoutRunnerError):
    """A scenario manifest (or its case list) broke the contract."""


class HeldoutSpecError(HeldoutRunnerError):
    """One case spec broke the contract (or smuggled candidate behavior)."""


class HeldoutConfigError(HeldoutRunnerError):
    """An exploration config broke the contract."""


class FreezeMismatchError(HeldoutRunnerError):
    """Frozen metadata did not match: config digest or freeze commit."""


class NamespaceError(HeldoutRunnerError):
    """Synthetic and held-out namespaces must never mix."""


class HeldoutResultError(HeldoutRunnerError):
    """A result broke the published result contract."""


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def _digest(payload):
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _known_rules():
    return set(CATALOG)


def _exact_keys(mapping, keys, label, error):
    if not isinstance(mapping, Mapping) or set(mapping) != keys:
        raise error(f"{label} keys must be exactly {sorted(keys)}")


def _valid_case_id(case_id):
    return (type(case_id) is str and bool(_CASE_ID_PATTERN.fullmatch(case_id)))


# -- frozen exploration config + handshake ------------------------------------


@dataclass(frozen=True)
class FrozenConfig:
    """Pinned exploration config: hashed at freeze time, re-checked at run."""

    namespace: str
    seed: int
    max_attempts: int
    action_cap: int
    digest: str

    def as_dict(self):
        return {"config_version": HELDOUT_CONFIG_VERSION,
                "namespace": self.namespace, "seed": self.seed,
                "max_attempts": self.max_attempts,
                "action_cap": self.action_cap}


def freeze_config(config):
    """Validate and pin an exploration config; any unknown field refuses."""
    _exact_keys(config, _CONFIG_KEYS, "held-out config", HeldoutConfigError)
    if config["config_version"] != HELDOUT_CONFIG_VERSION:
        raise HeldoutConfigError("wrong held-out config version")
    if config["namespace"] not in (HELDOUT_NAMESPACE, SYNTHETIC_NAMESPACE):
        raise HeldoutConfigError("config namespace must be heldout or "
                                 "synthetic-selftest")
    seed = config["seed"]
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise HeldoutConfigError("config seed must be an integer 0..2**63-1")
    max_attempts = config["max_attempts"]
    if type(max_attempts) is not int or not 1 <= max_attempts <= _MAX_CASES:
        raise HeldoutConfigError("config max_attempts must be 1..1000")
    action_cap = config["action_cap"]
    if type(action_cap) is not int or not 1 <= action_cap <= 1000:
        raise HeldoutConfigError("config action_cap must be 1..1000")
    pinned = {"config_version": HELDOUT_CONFIG_VERSION,
              "namespace": config["namespace"], "seed": seed,
              "max_attempts": max_attempts, "action_cap": action_cap}
    return FrozenConfig(namespace=pinned["namespace"], seed=seed,
                        max_attempts=max_attempts, action_cap=action_cap,
                        digest=_digest(pinned))


def frozen_handshake(config):
    """Freeze a config into a handshake record: config plus its digest."""
    frozen = freeze_config(config)
    return {"handshake_version": HELDOUT_HANDSHAKE_VERSION,
            "config": frozen.as_dict(), "config_digest": frozen.digest}


def verify_handshake(handshake):
    """Re-hash a handshake; any tampering after freezing refuses to run."""
    _exact_keys(handshake, _HANDSHAKE_KEYS, "frozen handshake",
                FreezeMismatchError)
    if handshake["handshake_version"] != HELDOUT_HANDSHAKE_VERSION:
        raise FreezeMismatchError("wrong handshake version")
    try:
        frozen = freeze_config(handshake["config"])
    except HeldoutRunnerError as error:
        raise FreezeMismatchError(f"handshake config is not frozen: {error}") from None
    digest = handshake["config_digest"]
    if type(digest) is not str or not hmac.compare_digest(digest, frozen.digest):
        raise FreezeMismatchError("config digest mismatch: the config changed "
                                  "after freezing")
    return frozen


# -- opaque candidate handle --------------------------------------------------


class HeldoutHandle:
    """The ONLY scenario-adjacent object a candidate ever receives.

    Carries the case id plus a seed-derived opaque token. No fixture,
    scenario, script, actor or expectation attributes exist on this object,
    and its repr carries no scenario content either.
    """

    __slots__ = ("_case_id", "_token")

    def __init__(self, case_id, token):
        self._case_id = case_id
        self._token = token

    @property
    def case_id(self):
        return self._case_id

    @property
    def token(self):
        return self._token

    def __repr__(self):
        return f"HeldoutHandle(case_id={self._case_id!r}, token=<opaque>)"


def _handle_token(seed, case_id):
    return _digest({"seed": seed, "case_id": case_id})[:32]


# -- scenario directory + manifest (consumed at run time, never authored here) --


@dataclass(frozen=True)
class _CaseSpec:
    """One validated case spec: world binding plus run parameters."""

    world: str
    fixture: Mapping[str, Any] | None
    scenario: Mapping[str, Any]
    actor_id: str | None
    task_id: str
    script: tuple[Action, ...] | None


@dataclass(frozen=True)
class _Manifest:
    namespace: str
    cases: tuple[tuple[str, _CaseSpec], ...]


def _safe_json_name(name, *, label, error):
    if (type(name) is not str or not name or len(name) > 200
            or not name.endswith(".json") or name != Path(name).name):
        raise error(f"invalid {label} file name {name!r}")
    return name


def _read_json_file(path, *, cap_bytes, label, error):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise error(f"cannot read {label} {path}") from None
    if len(raw) > cap_bytes:
        raise error(f"{label} exceeds {cap_bytes} bytes")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise error(f"{label} is not valid JSON") from None
    if not isinstance(payload, Mapping):
        raise error(f"{label} must be a JSON object")
    return payload


def _validate_script(script, case_id):
    if type(script) is not list or len(script) > _MAX_SCRIPT_ACTIONS:
        raise HeldoutSpecError(f"case {case_id!r}: candidate_script must be "
                               f"a list of at most {_MAX_SCRIPT_ACTIONS}")
    actions = []
    for index, item in enumerate(script):
        _exact_keys(item, _SCRIPT_ACTION_KEYS,
                    f"case {case_id!r} script action {index}",
                    HeldoutSpecError)
        tool, arguments = item["tool"], item["arguments"]
        if type(tool) is not str or not tool or len(tool) > 200:
            raise HeldoutSpecError(f"case {case_id!r}: invalid script tool")
        if (not isinstance(arguments, Mapping) or len(arguments) > 50
                or any(type(k) is not str or not k or len(k) > 200
                       for k in arguments)
                or any(type(v) not in (str, int, float, bool)
                       and v is not None for v in arguments.values())):
            raise HeldoutSpecError(f"case {case_id!r}: script arguments must "
                                   "be flat scalar mappings")
        try:
            actions.append(Action(tool=tool, arguments=dict(arguments)))
        except (TypeError, ValueError) as error:
            raise HeldoutSpecError(f"case {case_id!r}: bad script action: "
                                   f"{error}") from None
    return tuple(actions)


def _validate_spec(payload, *, namespace, case_id):
    allowed = _SPEC_BASE_KEYS | _SPEC_OPTIONAL_KEYS
    if namespace == SYNTHETIC_NAMESPACE:
        allowed |= {_SCRIPT_KEY}
    if not isinstance(payload, Mapping) or set(payload) - allowed:
        raise HeldoutSpecError(f"case {case_id!r}: unknown spec keys "
                               f"{sorted(set(payload) - allowed)}")
    if _SCRIPT_KEY in payload and namespace != SYNTHETIC_NAMESPACE:
        raise HeldoutSpecError(
            f"case {case_id!r}: held-out specs must not embed candidate "
            "behavior; the candidate drives every held-out case")
    if not _SPEC_BASE_KEYS <= set(payload):
        raise HeldoutSpecError(f"case {case_id!r}: spec needs exactly "
                               f"{sorted(_SPEC_BASE_KEYS)} plus optionals")
    if payload["spec_version"] != HELDOUT_SPEC_VERSION:
        raise HeldoutSpecError(f"case {case_id!r}: wrong spec version")
    if payload["world"] != REFUND_WORLD_ID:
        raise HeldoutSpecError(f"case {case_id!r}: unknown world "
                               f"{payload['world']!r}; this runner executes "
                               f"{REFUND_WORLD_ID!r} only")
    fixture = payload.get("fixture")
    if fixture is not None:
        if not isinstance(fixture, Mapping):
            raise HeldoutSpecError(f"case {case_id!r}: invalid fixture")
        try:
            Fixture(**dict(fixture))
        except (TypeError, ValueError) as error:
            raise HeldoutSpecError(f"case {case_id!r}: invalid fixture: "
                                   f"{error}") from None
        fixture = dict(fixture)
    scenario = payload.get("scenario")
    if not isinstance(scenario, Mapping):
        raise HeldoutSpecError(f"case {case_id!r}: invalid scenario")
    try:
        Scenario(**dict(scenario))
    except (TypeError, ValueError) as error:
        raise HeldoutSpecError(f"case {case_id!r}: invalid scenario: "
                               f"{error}") from None
    for key, bound in (("actor_id", 100), ("task_id", 200)):
        value = payload.get(key)
        if value is not None and (type(value) is not str or not value
                                  or len(value) > bound):
            raise HeldoutSpecError(f"case {case_id!r}: invalid {key}")
    if payload.get("task_id") is not None and payload["task_id"] != case_id:
        raise HeldoutSpecError(
            f"case {case_id!r}: task_id must equal the case id")
    script = None
    if _SCRIPT_KEY in payload:
        script = _validate_script(payload[_SCRIPT_KEY], case_id)
    return _CaseSpec(world=REFUND_WORLD_ID, fixture=fixture,
                     scenario=dict(scenario), actor_id=payload.get("actor_id"),
                     task_id=case_id, script=script)


def load_manifest(scenario_dir, manifest_name="manifest.json"):
    """Load a scenario directory plus manifest WITHOUT executing anything.

    The manifest and every spec are validated strictly: unknown fields,
    duplicate or mis-namespaced case ids, and unregistered worlds all refuse.
    Held-out case ids must NOT carry the ``SYNTH-`` prefix; synthetic case ids
    MUST, so the two namespaces can never collide.
    """
    try:
        directory = Path(scenario_dir)
    except (TypeError, ValueError):
        raise HeldoutManifestError("invalid scenario directory") from None
    if not directory.is_dir():
        raise HeldoutManifestError(f"scenario directory {directory} is missing")
    _safe_json_name(manifest_name, label="manifest", error=HeldoutManifestError)
    payload = _read_json_file(directory / manifest_name,
                              cap_bytes=_MANIFEST_MAX_BYTES, label="manifest",
                              error=HeldoutManifestError)
    _exact_keys(payload, _MANIFEST_KEYS, "held-out manifest",
                HeldoutManifestError)
    if payload["manifest_version"] != HELDOUT_MANIFEST_VERSION:
        raise HeldoutManifestError("wrong held-out manifest version")
    namespace = payload["namespace"]
    if namespace not in (HELDOUT_NAMESPACE, SYNTHETIC_NAMESPACE):
        raise HeldoutManifestError("manifest namespace must be heldout or "
                                   "synthetic-selftest")
    cases = payload["cases"]
    if type(cases) is not list or not cases or len(cases) > _MAX_CASES:
        raise HeldoutManifestError("manifest cases must be a list of 1..1000")
    checked = []
    seen = set()
    for index, item in enumerate(cases):
        _exact_keys(item, _CASE_KEYS, f"manifest case {index}",
                    HeldoutManifestError)
        case_id, spec_name = item["case_id"], item["spec"]
        if not _valid_case_id(case_id):
            raise HeldoutManifestError(f"invalid case id {case_id!r}")
        if case_id in seen:
            raise HeldoutManifestError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        synthetic = case_id.startswith(SYNTHETIC_PREFIX)
        if namespace == SYNTHETIC_NAMESPACE and not synthetic:
            raise HeldoutManifestError(
                f"case {case_id!r}: synthetic cases must use the SYNTH- prefix")
        if namespace == HELDOUT_NAMESPACE and synthetic:
            raise HeldoutManifestError(
                f"case {case_id!r}: held-out cases must not use the SYNTH- "
                "prefix")
        _safe_json_name(spec_name, label=f"case {case_id!r} spec",
                        error=HeldoutSpecError)
        spec_payload = _read_json_file(directory / spec_name,
                                       cap_bytes=_SPEC_MAX_BYTES,
                                       label=f"case {case_id!r} spec",
                                       error=HeldoutSpecError)
        checked.append((case_id, _validate_spec(spec_payload,
                                                namespace=namespace,
                                                case_id=case_id)))
    return _Manifest(namespace=namespace, cases=tuple(checked))


# -- deterministic execution --------------------------------------------------


class _PinnedRefundEnvironment(RefundEnvironment):
    """Refund adapter pinned to one validated inline fixture (or the default)."""

    def __init__(self, fixture):
        super().__init__()
        self._pinned_fixture = dict(fixture)

    def named_fixture(self, name):
        if name != FIXTURE_NAME:
            raise KeyError(f"Unknown fixture {name!r}")
        return dict(self._pinned_fixture)


def _refund_authorize(action):
    if action.tool in _REFUND_TOOLS and isinstance(action.arguments, Mapping):
        return None
    return {"decision": "deny", "reason": "unknown tool"}


def _default_fixture_dict():
    return asdict(Fixture())


def _empty_evidence_ref(note):
    return {"kind": EVIDENCE_KIND,
            "digest": _digest({"trace": [], "state": {}}),
            "trace_events": 0, "world": REFUND_WORLD_ID,
            "rules_version": RULES_VERSION, "note": note}


def _unknown_case(case_id, note):
    return {"case_id": case_id, "verdict": VERDICT_UNKNOWN, "rules_fired": [],
            "unknown_rules": [],
            "evidence_ref": _empty_evidence_ref(note),
            "inference_usage": {"model_calls": 0, "candidate_model": None,
                                "deterministic": False}}


def _candidate_model(made):
    if not made:
        return None, False
    try:
        model = made[0].info().model
    except Exception:
        return None, False
    if model is not None and type(model) is not str:
        return None, False
    if type(model) is str and (not model or len(model) > 500):
        return None, False
    return model, model is None


def _execute_case(*, case_id, spec, seed, action_cap, make_target):
    """Run one case through run_campaign plus assess; fail closed per case.

    ``make_target(case_id, handle)`` builds the target: the ONLY scenario
    contact the candidate gets is the case id plus the opaque handle. A
    candidate or execution failure becomes verdict ``unknown`` for that case
    only -- the note names the error TYPE, never candidate text, so results
    cannot smuggle scenario content back out.
    """
    handle = HeldoutHandle(case_id, _handle_token(seed, case_id))
    made = []

    def target_factory():
        target = make_target(case_id, handle)
        check_target(target)
        made.append(target)
        return target

    fixture_dict = dict(spec.fixture) if spec.fixture else _default_fixture_dict()
    run_actor = spec.actor_id or fixture_dict["customer_id"]

    def env_factory():
        return _PinnedRefundEnvironment(fixture_dict)
    attempt = AttemptSpec(
        name=case_id, arm=FIXED_ARM, fixture_name=FIXTURE_NAME,
        scenario=dict(spec.scenario), actor_id=run_actor,
        task=TargetTask(task_id=spec.task_id, params={"case_id": case_id}),
        target_factory=target_factory, cap=action_cap)
    stash = {}

    def assess_attempt(trace, state):
        view = refund_evidence(list(trace), dict(state), actor_id=spec.actor_id)
        assessment = assess(view, REFUND_RULES,
                            completion=refund_completion(view.facts))
        stash["assessment"] = assessment
        return {"failed_rules": list(assessment.failed_rules),
                "unknown_rules": list(assessment.unknown_rules),
                "safety_held": assessment.safety_held}

    try:
        report = run_campaign((attempt,), env_factory,
                              authorize=_refund_authorize,
                              assess=assess_attempt, max_attempts=1)
    except Exception as error:
        return _unknown_case(case_id,
                             f"execution error: {type(error).__name__}"[:500])
    assessment = stash.get("assessment")
    if assessment is None:
        return _unknown_case(case_id, "execution error: missing assessment")
    failed, unknown = assessment.failed_rules, assessment.unknown_rules
    verdict = (VERDICT_VIOLATION if failed
               else VERDICT_UNKNOWN if unknown else VERDICT_PASS)
    record = report.attempts[0]
    model, deterministic = _candidate_model(made)
    return {"case_id": case_id, "verdict": verdict,
            "rules_fired": sorted(failed), "unknown_rules": sorted(unknown),
            "evidence_ref": {
                "kind": EVIDENCE_KIND,
                "digest": _digest({"trace": record["trace"],
                                   "state": record["state"]}),
                "trace_events": len(record["trace"]), "world": REFUND_WORLD_ID,
                "rules_version": RULES_VERSION, "note": ""},
            "inference_usage": {"model_calls": 0, "candidate_model": model,
                                "deterministic": deterministic}}


def _cases_with_model(cases):
    return sum(1 for case in cases
               if case["inference_usage"]["candidate_model"] is not None
               or not case["inference_usage"]["deterministic"])


def _summarize(entries):
    counts = {verdict: sum(1 for e in entries if e["verdict"] == verdict)
              for verdict in _VERDICTS}
    return {"evaluated": len(entries), VERDICT_PASS: counts[VERDICT_PASS],
            VERDICT_VIOLATION: counts[VERDICT_VIOLATION],
            VERDICT_UNKNOWN: counts[VERDICT_UNKNOWN]}


def _assemble(*, namespace, freeze_proof, config, entries):
    cases = list(entries)
    return {"contract_version": HELDOUT_RESULT_VERSION,
            "runner_version": HELDOUT_RUNNER_VERSION,
            "namespace": namespace, "freeze": dict(freeze_proof),
            "config": config.as_dict(), "config_digest": config.digest,
            "seed": config.seed, "cases": cases, "summary": _summarize(cases),
            "inference_usage": {"model_calls": 0,
                                "cases_with_model": _cases_with_model(cases)}}


# -- public entry points ------------------------------------------------------


def run_heldout(*, scenario_dir, manifest="manifest.json", handshake,
                freeze_commit, candidate_factory, checkout="."):
    """Evaluate a hidden scenario set with a frozen candidate and config.

    Refuses BEFORE touching the scenario directory when the handshake digest
    or the freeze commit mismatches, when the checkout is dirty, or when the
    config/manifest namespaces disagree. The candidate factory receives ONLY
    ``(case_id, handle)`` per case. Returns the validated result contract.
    """
    if not callable(candidate_factory):
        raise TypeError("run_heldout needs a candidate_factory callable")
    if type(freeze_commit) is not str or not freeze_commit:
        raise FreezeMismatchError("invalid freeze commit")
    config = verify_handshake(handshake)
    frozen = freeze(checkout)
    if frozen.commit != freeze_commit:
        raise FreezeMismatchError("freeze commit mismatch: the checkout moved "
                                  "after freezing")
    loaded = load_manifest(scenario_dir, manifest)
    if loaded.namespace != HELDOUT_NAMESPACE:
        raise NamespaceError("run_heldout takes heldout-namespace manifests "
                             "only; run run_selftest for the synthetic fixture")
    if config.namespace != loaded.namespace:
        raise NamespaceError("config namespace does not match the manifest "
                             "namespace: frozen tuning cannot cross namespaces")
    if len(loaded.cases) > config.max_attempts:
        raise HeldoutManifestError(f"{len(loaded.cases)} cases exceed the "
                                   f"frozen bound of {config.max_attempts}")
    entries = [_execute_case(case_id=case_id, spec=spec, seed=config.seed,
                             action_cap=config.action_cap,
                             make_target=candidate_factory)
               for case_id, spec in loaded.cases]
    freeze_proof = {"commit": frozen.commit,
                    "rules_version": frozen.rules_version}
    return validate_result(_assemble(namespace=loaded.namespace,
                                     freeze_proof=freeze_proof, config=config,
                                     entries=entries))


def _bundled_fixture_dir():
    from importlib import resources
    return Path(str(resources.files("ghostlab.harness") / "selftest_fixture"))


def _current_commit_or_unknown():
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", "rev-parse",
                              "HEAD"], capture_output=True, text=True,
                             check=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError, ValueError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def run_selftest(*, fixture_dir=None, handshake=None, candidate_factory=None,
                 freeze_proof=None):
    """Run the SYNTHETIC self-test fixture (never hidden evaluation content).

    Builds scripted targets from the fixture's embedded ``candidate_script``
    entries, or drives the fixture through ``candidate_factory`` when one is
    given (same ``(case_id, handle)`` separation as held-out runs). The freeze
    proof here is informational only -- it is recorded, never enforced.
    """
    if candidate_factory is not None and not callable(candidate_factory):
        raise TypeError("candidate_factory must be callable")
    directory = Path(fixture_dir) if fixture_dir is not None \
        else _bundled_fixture_dir()
    if handshake is None:
        raw = _read_json_file(directory / "selftest_config.json",
                              cap_bytes=_SPEC_MAX_BYTES,
                              label="self-test config",
                              error=HeldoutConfigError)
        handshake = frozen_handshake(raw)
    config = verify_handshake(handshake)
    loaded = load_manifest(directory, "manifest.json")
    if loaded.namespace != SYNTHETIC_NAMESPACE:
        raise NamespaceError("run_selftest takes synthetic-selftest "
                             "manifests only")
    if config.namespace != loaded.namespace:
        raise NamespaceError("config namespace does not match the manifest "
                             "namespace: frozen tuning cannot cross namespaces")
    if len(loaded.cases) > config.max_attempts:
        raise HeldoutManifestError(f"{len(loaded.cases)} cases exceed the "
                                   f"frozen bound of {config.max_attempts}")
    if freeze_proof is None:
        freeze_proof = {"commit": _current_commit_or_unknown(),
                        "rules_version": RULES_VERSION}
    else:
        _exact_keys(freeze_proof, frozenset({"commit", "rules_version"}),
                    "self-test freeze proof", HeldoutResultError)
        if freeze_proof["rules_version"] != RULES_VERSION:
            raise HeldoutResultError("wrong rules version in freeze proof")
        if type(freeze_proof["commit"]) is not str or not freeze_proof["commit"]:
            raise HeldoutResultError("invalid commit in freeze proof")

    def make_target(case_id, handle, spec):
        if candidate_factory is not None:
            return candidate_factory(case_id, handle)
        if spec.script is None:
            raise HeldoutSpecError(
                f"case {case_id!r}: no embedded candidate_script and no "
                "candidate_factory was given")
        return ScriptedTarget(list(spec.script),
                              adapter_id="synthetic-scripted")

    if candidate_factory is None:
        for case_id, spec in loaded.cases:
            if spec.script is None:
                raise HeldoutSpecError(
                    f"case {case_id!r}: no embedded candidate_script and no "
                    "candidate_factory was given")
    entries = [_execute_case(case_id=case_id, spec=spec, seed=config.seed,
                             action_cap=config.action_cap,
                             make_target=lambda c, h, s=spec: make_target(c, h, s))
               for case_id, spec in loaded.cases]
    return validate_result(_assemble(namespace=loaded.namespace,
                                     freeze_proof=freeze_proof, config=config,
                                     entries=entries))


# -- versioned result contract ------------------------------------------------

_RESULT_KEYS = frozenset({"contract_version", "runner_version", "namespace",
                          "freeze", "config", "config_digest", "seed", "cases",
                          "summary", "inference_usage"})
_FREEZE_KEYS = frozenset({"commit", "rules_version"})
_CASE_RESULT_KEYS = frozenset({"case_id", "verdict", "rules_fired",
                               "unknown_rules", "evidence_ref",
                               "inference_usage"})
_EVIDENCE_KEYS = frozenset({"kind", "digest", "trace_events", "world",
                            "rules_version", "note"})
_USAGE_KEYS = frozenset({"model_calls", "candidate_model", "deterministic"})
_SUMMARY_KEYS = frozenset({"evaluated", VERDICT_PASS, VERDICT_VIOLATION,
                           VERDICT_UNKNOWN})
_TOP_USAGE_KEYS = frozenset({"model_calls", "cases_with_model"})


def _checked_rule_list(rules, *, case_id, key, known):
    if (not isinstance(rules, (list, tuple)) or len(rules) > len(known)
            or any(type(r) is not str for r in rules)
            or any(r not in known for r in rules)):
        raise HeldoutResultError(f"case {case_id!r}: {key} must list known "
                                 "rule names")
    if sorted(set(rules)) != list(rules):
        raise HeldoutResultError(f"case {case_id!r}: {key} must be sorted "
                                 "and unique")
    return tuple(rules)


def _checked_evidence(ref, case_id):
    _exact_keys(ref, _EVIDENCE_KEYS, f"case {case_id!r} evidence_ref",
                HeldoutResultError)
    if ref["kind"] != EVIDENCE_KIND:
        raise HeldoutResultError(f"case {case_id!r}: wrong evidence kind")
    digest = ref["digest"]
    try:
        int(digest, 16)
    except (TypeError, ValueError):
        raise HeldoutResultError(
            f"case {case_id!r}: bad evidence digest") from None
    if type(digest) is not str or len(digest) != 64:
        raise HeldoutResultError(f"case {case_id!r}: bad evidence digest")
    if type(ref["trace_events"]) is not int or not 0 <= ref["trace_events"] <= 1000:
        raise HeldoutResultError(f"case {case_id!r}: bad trace event count")
    if ref["world"] != REFUND_WORLD_ID:
        raise HeldoutResultError(f"case {case_id!r}: unknown evidence world")
    if ref["rules_version"] != RULES_VERSION:
        raise HeldoutResultError(f"case {case_id!r}: wrong rules version")
    if type(ref["note"]) is not str or len(ref["note"]) > 500:
        raise HeldoutResultError(f"case {case_id!r}: bad evidence note")
    return {"kind": EVIDENCE_KIND, "digest": digest,
            "trace_events": ref["trace_events"], "world": REFUND_WORLD_ID,
            "rules_version": RULES_VERSION, "note": ref["note"]}


def _checked_usage(usage, case_id):
    _exact_keys(usage, _USAGE_KEYS, f"case {case_id!r} inference_usage",
                HeldoutResultError)
    if type(usage["model_calls"]) is not int or usage["model_calls"] != 0:
        raise HeldoutResultError(f"case {case_id!r}: model_calls must be 0; "
                                 "the harness performs no inference")
    model = usage["candidate_model"]
    if model is not None and (type(model) is not str or not model
                              or len(model) > 500):
        raise HeldoutResultError(f"case {case_id!r}: bad candidate model")
    if type(usage["deterministic"]) is not bool:
        raise HeldoutResultError(f"case {case_id!r}: bad deterministic flag")
    return {"model_calls": 0, "candidate_model": model,
            "deterministic": usage["deterministic"]}


def validate_result(result):
    """Validate a held-out result against the contract; unknown fields refuse.

    Accepts ONLY the exact top-level and per-case keys: verdicts from the
    ``pass``/``violation``/``unknown`` vocabulary, known rule names, evidence
    digests (never scenario content) and zero harness inference. Returns the
    normalized contract.
    """
    _exact_keys(result, _RESULT_KEYS, "held-out result", HeldoutResultError)
    if result["contract_version"] != HELDOUT_RESULT_VERSION:
        raise HeldoutResultError("wrong held-out result contract version")
    if result["runner_version"] != HELDOUT_RUNNER_VERSION:
        raise HeldoutResultError("wrong held-out runner version")
    if result["namespace"] not in (HELDOUT_NAMESPACE, SYNTHETIC_NAMESPACE):
        raise HeldoutResultError("result namespace must be heldout or "
                                 "synthetic-selftest")
    _exact_keys(result["freeze"], _FREEZE_KEYS, "result freeze proof",
                HeldoutResultError)
    if result["freeze"]["rules_version"] != RULES_VERSION:
        raise HeldoutResultError("wrong rules version in freeze proof")
    if type(result["freeze"]["commit"]) is not str or not result["freeze"]["commit"]:
        raise HeldoutResultError("invalid commit in freeze proof")
    try:
        config = freeze_config(result["config"])
    except HeldoutRunnerError as error:
        raise HeldoutResultError(f"result config is not frozen: {error}") from None
    if result["config_digest"] != config.digest or result["seed"] != config.seed:
        raise HeldoutResultError("result config digest or seed disagrees with "
                                 "the frozen config")
    if config.namespace != result["namespace"]:
        raise HeldoutResultError("result namespace disagrees with its config")
    cases = result["cases"]
    if not isinstance(cases, (list, tuple)) or not cases or len(cases) > _MAX_CASES:
        raise HeldoutResultError("result cases must be a list of 1..1000")
    known = _known_rules()
    checked, seen = [], set()
    for item in cases:
        _exact_keys(item, _CASE_RESULT_KEYS, "result case", HeldoutResultError)
        case_id = item["case_id"]
        if not _valid_case_id(case_id):
            raise HeldoutResultError(f"invalid case id {case_id!r}")
        if case_id in seen:
            raise HeldoutResultError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        if item["verdict"] not in _VERDICTS:
            raise HeldoutResultError(f"case {case_id!r}: bad verdict")
        fired = _checked_rule_list(item["rules_fired"], case_id=case_id,
                                   key="rules_fired", known=known)
        unknown = _checked_rule_list(item["unknown_rules"], case_id=case_id,
                                     key="unknown_rules", known=known)
        if set(fired) & set(unknown):
            raise HeldoutResultError(f"case {case_id!r}: a rule cannot be "
                                     "fired and unknown")
        checked.append({"case_id": case_id, "verdict": item["verdict"],
                        "rules_fired": fired, "unknown_rules": unknown,
                        "evidence_ref": _checked_evidence(item["evidence_ref"],
                                                          case_id),
                        "inference_usage": _checked_usage(item["inference_usage"],
                                                          case_id)})
    _exact_keys(result["summary"], _SUMMARY_KEYS, "result summary",
                HeldoutResultError)
    if result["summary"] != _summarize(checked):
        raise HeldoutResultError("result summary disagrees with its cases")
    _exact_keys(result["inference_usage"], _TOP_USAGE_KEYS,
                "result inference_usage", HeldoutResultError)
    top_calls = result["inference_usage"]["model_calls"]
    if type(top_calls) is not int or top_calls != 0:
        raise HeldoutResultError("result model_calls must be 0")
    if (result["inference_usage"]["cases_with_model"]
            != _cases_with_model(checked)):
        raise HeldoutResultError("result inference summary disagrees")
    return {"contract_version": HELDOUT_RESULT_VERSION,
            "runner_version": HELDOUT_RUNNER_VERSION,
            "namespace": result["namespace"],
            "freeze": {"commit": result["freeze"]["commit"],
                       "rules_version": RULES_VERSION},
            "config": config.as_dict(), "config_digest": config.digest,
            "seed": config.seed, "cases": tuple(checked),
            "summary": dict(result["summary"]),
            "inference_usage": dict(result["inference_usage"])}


def to_legacy_result(result):
    """Project a result onto the consumer-side held-out contract.

    The existing :func:`ghostlab.verify.validate_result` shape carries opaque
    scenario ids with detection booleans only; ``violation`` maps to detected
    and anything else to not detected. Per-case ``unknown_rules`` have no
    legacy spelling and are dropped here -- the full contract stays
    authoritative.
    """
    checked = validate_result(result)
    return {"contract_version": HELDOUT_RESULT_VERSION,
            "runner_version": HELDOUT_RUNNER_VERSION,
            "freeze_commit": checked["freeze"]["commit"],
            "scenarios": [
                {"scenario_id": case["case_id"],
                 "detected": case["verdict"] == VERDICT_VIOLATION,
                 "rules_fired": list(case["rules_fired"])}
                for case in checked["cases"]]}


def dump_result_json(result):
    """Serialize a validated result to canonical JSON text."""
    return _canonical(validate_result(result))


__all__ = ["HELDOUT_CONFIG_VERSION", "HELDOUT_HANDSHAKE_VERSION",
           "HELDOUT_MANIFEST_VERSION", "HELDOUT_NAMESPACE", "HELDOUT_SPEC_VERSION",
           "SYNTHETIC_NAMESPACE", "VERDICT_PASS", "VERDICT_UNKNOWN",
           "VERDICT_VIOLATION", "FreezeMismatchError", "FrozenConfig",
           "HeldoutHandle", "HeldoutManifestError", "HeldoutResultError",
           "HeldoutRunnerError", "HeldoutSpecError", "NamespaceError",
           "dump_result_json", "freeze_config", "frozen_handshake",
           "load_manifest", "run_heldout", "run_selftest", "to_legacy_result",
           "validate_result", "verify_handshake"]
