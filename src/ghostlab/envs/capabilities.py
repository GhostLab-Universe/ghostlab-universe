"""Capability discovery: compare declared versus implemented capabilities.

Discovery instantiates the registered adapter and calls ``describe()`` only.
It never resets, invokes tools, arms faults, or runs a workload.
"""

from .adapters import resolve_adapter_ref


def _check_describe(described) -> dict:
    required = {"env_id", "version", "tools", "reproduction_modes", "fault_hooks",
                "supports_cancellation", "isolation"}
    if type(described) is not dict or not required.issubset(described):
        raise ValueError("adapter describe() misses required fields")
    if type(described["tools"]) is not list or type(
            described["reproduction_modes"]) is not list:
        raise ValueError("adapter describe() has malformed tools or modes")
    if type(described["fault_hooks"]) is not list or type(
            described["supports_cancellation"]) is not bool:
        raise ValueError("adapter describe() has malformed hooks or flags")
    return described


def discover(registry, env_id: str, *, manifest_digest: str | None = None) -> dict:
    """Instantiate the adapter, read describe(), diff against declarations."""
    registration = registry.get(env_id)
    manifest = None
    if manifest_digest is not None:
        manifest = registry.get_manifest(manifest_digest)
        if manifest["env_id"] != env_id:
            raise ValueError("manifest belongs to a different env_id")
        declared_tools = manifest["manifest"]["tools"]
        declared_modes = manifest["manifest"]["reproduction_modes"]
    else:
        declared_tools = registration["declared_tools"]
        declared_modes = registration["declared_modes"]
    adapter_cls = resolve_adapter_ref(registration["adapter_ref"],
                                      registry.allowed_adapter_prefixes)
    described = _check_describe(adapter_cls().describe())
    mismatches = []
    if described["env_id"] != env_id:
        mismatches.append("env_id")
    if described["version"] != registration["env_version"]:
        mismatches.append("version")
    if ({(t["name"], tuple(t["parameters"])) for t in described["tools"]}
            != {(t["name"], tuple(t["parameters"])) for t in declared_tools}):
        mismatches.append("tools")
    if (sorted(described["reproduction_modes"]) != sorted(declared_modes)):
        mismatches.append("reproduction_modes")
    return {
        "env_id": env_id, "env_version": registration["env_version"],
        "adapter_ref": registration["adapter_ref"],
        "code_digest": registration["code_digest"],
        "manifest_digest": manifest_digest,
        "declared_tools": declared_tools, "discovered_tools": described["tools"],
        "declared_modes": declared_modes,
        "discovered_modes": described["reproduction_modes"],
        "fault_hooks": described["fault_hooks"],
        "supports_cancellation": described["supports_cancellation"],
        "isolation": described["isolation"],
        "match": not mismatches, "mismatches": mismatches,
    }
