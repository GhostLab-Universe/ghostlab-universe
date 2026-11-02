"""Allowed-change validation for candidates (pure, stdlib only).

Developer-supplied mode (wired): the candidate selects a changed
*registered* agent/service version -- both endpoints must resolve in
the operator registry for the candidate world, the proposed version
must equal the registered manifest version (no phantom versions),
and the endpoints must differ. Optional config overlays may only
touch allowlisted tunable fields.

Generated-config mode (bounded contract, no live model): the target
stays identical and only allowlisted tunable fields may change.

Refusals: ``not_allowlisted`` for unknown tunable fields,
``escape_attempt`` for paths reaching toward code, identity,
transport or secret-adjacent settings, and ``generated_code_refused``
for any code-kind diff -- generated-code execution stays refused
structurally (this package has no code execution path) and at
validation. Standard library only.
"""

# Deployment-owned default allowlist: dotted config path -> (kind, low, high).
# Only inert runtime tunables; anything addressing code, identity,
# transport, tools or prompts is never allowlisted.
TUNABLE_FIELDS = {
    "run.max_tool_calls": ("int", 1, 1000),
    "run.max_wall_clock_ms": ("int", 1, 3_600_000),
    "run.repetitions": ("int", 1, 25),
    "agent.timeout_ms": ("int", 1, 60_000),
    "agent.max_retries": ("int", 0, 5),
}

# Path segments that reach toward code, identity, transport, tools,
# prompts or secrets. A diff touching one is refused as an escape
# attempt, even when the full path is otherwise unknown.
_ESCAPE_SEGMENTS = frozenset({
    "code", "script", "actions", "action", "tool", "tools", "gateway",
    "adapter", "package", "module", "import", "path", "file", "command",
    "exec", "shell", "endpoint", "url", "uri", "host", "port", "model",
    "provider", "prompt", "system", "key", "secret", "token", "password",
    "credential", "auth", "identity", "principal", "allowlist", "policy",
    "registry", "manifest", "origin", "env",
})


class ChangeRefusal(ValueError):
    """A candidate change the allowlist or registration check refused."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code


def code_identity_of(manifest) -> str:
    """Stable identity for registered code: package@version/origin digest."""
    from ..experiments.configs import canonical_digest
    return canonical_digest({"package": manifest.package,
                             "version": manifest.version,
                             "origin": manifest.origin})


def check_tunable(path: str, value) -> None:
    """Accept one allowlisted tunable value, else refuse (pure)."""
    spec = TUNABLE_FIELDS.get(path)
    if spec is None:
        segments = {word.lower()
                    for segment in path.replace("-", "_").split(".")
                    for word in segment.split("_")}
        if segments & _ESCAPE_SEGMENTS:
            raise ChangeRefusal("escape_attempt",
                                f"'{path}' reaches outside tunable config")
        raise ChangeRefusal("not_allowlisted",
                            f"'{path}' is not an allowlisted tunable field")
    kind, low, high = spec
    if kind == "int" and (type(value) is not int or not low <= value <= high):
        raise ChangeRefusal("out_of_bounds",
                            f"'{path}' must be an int from {low} to {high}")


def validate_change(*, mode: str, parent: dict, proposed: dict, diff: list,
                    registry, world_version: str) -> dict:
    """Validate one candidate change against registration and allowlist.

    ``registry`` is the operator's ``AdapterRegistry`` and ``world_version``
    the candidate world; both endpoints resolve through it. Returns the
    resolved ``{\"parent_manifest\": ..., \"proposed_manifest\": ...}``.
    Raises :class:`ChangeRefusal` for registration, exactness, allowlist
    or escape violations. Makes no model calls and mutates nothing.
    """
    if mode not in ("developer_supplied", "generated_config"):
        raise ChangeRefusal("unsupported_mode", f"unknown mode '{mode}'")
    if type(world_version) is not str or not world_version:
        raise ChangeRefusal("unknown_world", "world version is invalid")
    try:
        parent_manifest = registry.target_manifest(world_version,
                                                   parent["target_id"])
    except (KeyError, LookupError, TypeError, ValueError):
        raise ChangeRefusal("unknown_target",
                            f"parent target '{parent.get('target_id')}' is "
                            f"unregistered for '{world_version}'") from None
    try:
        proposed_manifest = registry.target_manifest(world_version,
                                                     proposed["target_id"])
    except (KeyError, LookupError, TypeError, ValueError):
        raise ChangeRefusal("unknown_target",
                            f"proposed target '{proposed.get('target_id')}' is "
                            f"unregistered for '{world_version}'") from None
    if proposed["target_version"] != proposed_manifest.version:
        raise ChangeRefusal("version_mismatch",
                            "proposed version does not match the registered "
                            f"manifest version '{proposed_manifest.version}'")
    if parent["target_version"] != parent_manifest.version:
        raise ChangeRefusal("version_mismatch",
                            "parent version does not match the registered "
                            f"manifest version '{parent_manifest.version}'")
    if parent.get("code_identity") != code_identity_of(parent_manifest):
        raise ChangeRefusal("identity_mismatch",
                            "parent code_identity does not match registration")
    if proposed.get("code_identity") != code_identity_of(proposed_manifest):
        raise ChangeRefusal("identity_mismatch",
                            "proposed code_identity does not match registration")

    target_entries = [e for e in diff if e["kind"] == "target"]
    field_entries = [e for e in diff if e["kind"] == "field"]
    code_entries = [e for e in diff if e["kind"] == "code"]
    if code_entries:
        raise ChangeRefusal("generated_code_refused",
                            "code diffs are refused; generated-code "
                            "execution stays disabled")
    if len(target_entries) + len(field_entries) != len(diff):
        raise ChangeRefusal("unsupported_kind",
                            "diff entries must be kind target, field or code")

    changed_target = (parent["target_id"], parent["target_version"]) != (
        proposed["target_id"], proposed["target_version"])
    if mode == "generated_config":
        if changed_target:
            raise ChangeRefusal(
                "target_change_forbidden",
                "generated_config candidates are config-only; the target "
                "must stay identical")
        if not field_entries:
            raise ChangeRefusal("no_change", "generated_config diff changes nothing")
        if parent.get("config_digest") == proposed.get("config_digest"):
            raise ChangeRefusal("no_change",
                                "generated_config config_digest is unchanged")
    else:
        if not changed_target:
            raise ChangeRefusal("no_change",
                                "developer_supplied candidates must select a "
                                "changed registered version")
        if len(target_entries) != 1:
            raise ChangeRefusal("diff_mismatch",
                                "developer_supplied diff must carry exactly "
                                "one target entry")
        entry = target_entries[0]
        expected_before = {"target_id": parent["target_id"],
                           "target_version": parent["target_version"]}
        expected_after = {"target_id": proposed["target_id"],
                          "target_version": proposed["target_version"]}
        if (entry["path"] != "target" or entry["before"] != expected_before
                or entry["after"] != expected_after):
            raise ChangeRefusal("diff_mismatch",
                                "target entry must exactly describe the "
                                "parent to proposed change")
    for entry in field_entries:
        check_tunable(entry["path"], entry["after"])
        if entry["before"] == entry["after"]:
            raise ChangeRefusal("no_change",
                                f"'{entry['path']}' before equals after")
    return {"parent_manifest": parent_manifest,
            "proposed_manifest": proposed_manifest}


__all__ = ["TUNABLE_FIELDS", "ChangeRefusal", "code_identity_of",
           "check_tunable", "validate_change"]
