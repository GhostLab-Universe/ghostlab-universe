"""Operator environment commands: register, list, import, qualify.

``python -m ghostlab envs <verb>``. Registration and manifest import never
execute adapter code beyond a shape check; ``env-qualify`` is the explicit
operation that runs disposable test behavior. JSON to stdout, errors to
stderr; 0 on success, 1 on failure.
"""

import argparse
import json
import sys
from pathlib import Path

from .environment import Action
from .operator_registry import (OperatorRegistry, adapter_code_digest,
                                resolve_adapter_ref)
from .qualify import ProbeScript, qualify_environment

DEFAULT_DB = Path("artifacts/local/env-registry.sqlite")


def _registry(args) -> OperatorRegistry:
    prefixes = ["ghostlab."]
    for extra in args.allow_prefix or ():
        bare = extra[:-1] if extra.endswith(".") else extra
        from .operator_registry import MODULE_PATTERN
        if not MODULE_PATTERN.match(bare):
            raise ValueError(f"invalid --allow-prefix '{extra}'")
        prefixes.append(bare)
        prefixes.append(bare + ".")
    return OperatorRegistry(args.db, allowed_adapter_prefixes=tuple(prefixes))


def add_env_commands(commands) -> None:
    """Register the ``envs`` verb group on the main subparsers."""
    envs = commands.add_parser("envs", help="Register and qualify environments")
    add_env_verbs(envs.add_subparsers(dest="envs_command", required=True))


def add_env_verbs(verbs) -> None:
    """Register the environment verbs on one subparsers object."""

    def common(parser):
        parser.add_argument("--db", type=Path, default=DEFAULT_DB)
        parser.add_argument("--allow-prefix", action="append", default=None)

    register = verbs.add_parser("register", help="Register reviewed adapter code")
    common(register)
    register.add_argument("--world", required=True)
    register.add_argument("--env-id", required=True)
    register.add_argument("--env-version", required=True)
    register.add_argument("--adapter-ref", required=True)
    register.add_argument("--display-name", required=True)
    register.add_argument("--tools", required=True, help="JSON array")
    register.add_argument("--modes", required=True, help="JSON array")
    register.add_argument("--registered-by", required=True)

    listing = verbs.add_parser("list", help="List registered environments")
    common(listing)

    import_manifest = verbs.add_parser("import-manifest",
                                       help="Import a JSON manifest")
    common(import_manifest)
    import_manifest.add_argument("--manifest", type=Path, required=True)

    capabilities = verbs.add_parser("capabilities", help="Describe-only discovery")
    common(capabilities)
    capabilities.add_argument("--world", required=True)

    qualify = verbs.add_parser("env-qualify",
                               help="Qualify against disposable test state")
    common(qualify)
    qualify.add_argument("--world", required=True)
    qualify.add_argument("--fixture", required=True)
    qualify.add_argument("--scenario", required=True, help="JSON object")
    qualify.add_argument("--script", required=True, help="JSON action array")
    qualify.add_argument("--task-id", default="operator-probe")
    qualify.add_argument("--actor", required=True)
    qualify.add_argument("--manifest-digest", default=None)


def _as_json(payload) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _describe_only(operator: OperatorRegistry, world: str) -> dict:
    record = operator.get(world)
    cls = resolve_adapter_ref(record["adapter_ref"], operator.allowed_prefixes)
    if adapter_code_digest(cls) != record["code_digest"]:
        raise ValueError(f"'{world}' adapter code changed since review")
    adapter = cls()
    try:
        description = adapter.describe()
    finally:
        adapter.close()
    return {"world_version": description.world_version,
            "environment_id": description.environment_id,
            "environment_version": description.environment_version,
            "tools": [{"name": tool.name, "effect": tool.effect,
                       "parameters": dict(tool.parameters)} for tool in description.tools],
            "faults": [fault.fault_id for fault in description.faults],
            "invariants": list(description.invariants),
            "fixtures": list(description.fixtures),
            "capabilities": dict(description.capabilities)}


def _manifest_agreement(operator, world, manifest) -> tuple:
    """Manifest claims versus described reality: agreement plus detail."""
    described = _describe_only(operator, world)
    mismatches = []
    if (sorted(tool["name"] for tool in manifest["tools"])
            != sorted(tool["name"] for tool in described["tools"])):
        mismatches.append("tools")
    for key in ("fixtures", "invariants"):
        if sorted(manifest[key]) != sorted(described[key]):
            mismatches.append(key)
    if sorted(manifest["fault_hooks"]) != sorted(described["faults"]):
        mismatches.append("fault_hooks")
    if dict(manifest["capabilities"]) != described["capabilities"]:
        mismatches.append("capabilities")
    if mismatches:
        return False, "manifest disagrees on: " + ", ".join(sorted(mismatches))
    return True, "manifest matches the described adapter"


def run_env_command(args) -> int:
    """Execute one parsed ``envs`` command. Returns the process exit code."""
    try:
        operator = _registry(args)
        if args.envs_command == "register":
            try:
                tools = json.loads(args.tools)
                modes = json.loads(args.modes)
            except ValueError as error:
                raise ValueError("tools and modes must be JSON arrays") from error
            record = operator.register(
                world_version=args.world, environment_id=args.env_id,
                environment_version=args.env_version, adapter_ref=args.adapter_ref,
                display_name=args.display_name, declared_tools=tools,
                declared_modes=modes, registered_by=args.registered_by)
            print(_as_json(record))
        elif args.envs_command == "list":
            print(_as_json({"envs": operator.list_envs()}))
        elif args.envs_command == "import-manifest":
            data = Path(args.manifest).read_bytes()
            print(_as_json(operator.import_manifest(data)))
        elif args.envs_command == "capabilities":
            print(_as_json(_describe_only(operator, args.world)))
        elif args.envs_command == "env-qualify":
            try:
                scenario = json.loads(args.scenario)
                script = json.loads(args.script)
            except ValueError as error:
                raise ValueError("scenario and script must be JSON") from error
            record = operator.get(args.world)
            cls = resolve_adapter_ref(record["adapter_ref"], operator.allowed_prefixes)
            probe = ProbeScript(
                fixture=args.fixture, scenario=scenario,
                script=[Action(tool=item["tool"], arguments=dict(item["arguments"]))
                        for item in script], task_id=args.task_id)
            from .registry import AdapterManifest
            report = qualify_environment(
                world_version=args.world, env_factory=cls,
                manifest=AdapterManifest(package="operator-registry",
                                         version=record["environment_version"],
                                         origin="registered",
                                         description=record["display_name"]),
                probe=probe, actor_id=args.actor)
            checks = [check.to_dict() for check in report.checks]
            passed = report.passed
            if args.manifest_digest is not None:
                manifest = operator.get_manifest(args.manifest_digest)
                agree, detail = _manifest_agreement(operator, args.world, manifest)
                checks.append({"name": "manifest_agreement",
                               "passed": agree, "details": detail})
                passed = passed and agree
            stored = operator.record_qualification(
                world_version=args.world, checks=checks, passed=passed,
                manifest_digest=args.manifest_digest)
            shown = report.to_dict()
            shown["checks"] = checks
            shown["passed"] = passed
            print(_as_json({"report": shown, "stored": stored}))
        else:
            raise ValueError(f"unknown envs command '{args.envs_command}'")
        return 0
    except (ValueError, LookupError, TypeError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    """Standalone entry: ``python -m ghostlab.extensions.operator_cli``."""
    parser = argparse.ArgumentParser(prog="ghostlab envs",
                                     description="Register and qualify environments")
    add_env_verbs(parser.add_subparsers(dest="envs_command", required=True))
    args = parser.parse_args(argv)
    return run_env_command(args)


if __name__ == "__main__":
    sys.exit(main())
