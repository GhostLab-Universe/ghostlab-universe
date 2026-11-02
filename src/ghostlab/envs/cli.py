"""Environment CLI. Standalone (``python -m ghostlab.envs``) or wired into the
main CLI later via ``add_env_commands`` (owned by whoever owns cli.py)."""

import argparse
import json
from pathlib import Path

from . import ENVS_SCHEMA_VERSION
from .adapters import MODULE_PATTERN
from .capabilities import discover
from .manifest import import_manifest
from .qualification import run_qualification
from .registry import DEFAULT_ALLOWED_PREFIXES, EnvRegistry
from .reports import get_status

DEFAULT_DB = Path("artifacts/local/env-registry.sqlite")


def _registry(args) -> EnvRegistry:
    prefixes = list(DEFAULT_ALLOWED_PREFIXES)
    for extra in args.allow_prefix or ():
        bare = extra[:-1] if extra.endswith(".") else extra
        if not MODULE_PATTERN.match(bare):
            raise ValueError(f"invalid --allow-prefix '{extra}'")
        prefixes.append(extra if extra.endswith(".") else extra + ".")
    return EnvRegistry(args.db, allowed_adapter_prefixes=tuple(prefixes))


def _as_json(payload) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _cmd_register(args) -> int:
    try:
        tools = json.loads(args.tools)
        modes = json.loads(args.modes)
    except ValueError as error:
        raise ValueError("tools and modes must be JSON arrays") from error
    record = _registry(args).register(
        env_id=args.env_id, env_version=args.env_version,
        adapter_ref=args.adapter_ref, display_name=args.display_name,
        declared_tools=tools, declared_modes=modes,
        registered_by=args.registered_by)
    print(_as_json(record))
    return 0


def _cmd_list(args) -> int:
    print(_as_json({"envs": _registry(args).list_envs()}))
    return 0


def _cmd_import_manifest(args) -> int:
    data = Path(args.manifest).read_bytes()
    print(_as_json(import_manifest(_registry(args), data)))
    return 0


def _cmd_capabilities(args) -> int:
    print(_as_json(discover(_registry(args), args.env_id,
                           manifest_digest=args.manifest_digest)))
    return 0


def _cmd_qualify(args) -> int:
    fixture = None
    if args.fixture is not None:
        try:
            fixture = json.loads(args.fixture)
        except ValueError as error:
            raise ValueError("fixture must be a JSON object") from error
        if type(fixture) is not dict:
            raise ValueError("fixture must be a JSON object")
    report = run_qualification(_registry(args), args.env_id,
                               manifest_digest=args.manifest_digest,
                               fixture=fixture)
    print(_as_json(report))
    return 0 if report["status"] == "qualified-for-declared" else 1


def _cmd_report(args) -> int:
    registry = _registry(args)
    latest = registry.latest_report(args.env_id)
    if latest is None:
        print(_as_json({"env_id": args.env_id,
                        "status": get_status(registry, args.env_id),
                        "report": None}))
    else:
        print(_as_json(latest))
    return 0


def add_env_commands(subparsers) -> None:
    """Attach Track A commands to an existing argparse subparsers object."""
    register = subparsers.add_parser("env-register",
                                     help="Register reviewed local adapter code")
    register.add_argument("--db", type=Path, default=DEFAULT_DB)
    register.add_argument("--allow-prefix", action="append", default=None,
                          help="Extra reviewed adapter module prefix,"
                               " e.g. examples.equipment_booking.")
    register.add_argument("--env-id", required=True)
    register.add_argument("--env-version", required=True)
    register.add_argument("--adapter-ref", required=True,
                          help="Checked-in code as 'module:Class'")
    register.add_argument("--display-name", required=True)
    register.add_argument("--tools", required=True,
                          help='JSON array, e.g. [{"name":"get_order",'
                               '"parameters":["order_id"]}]')
    register.add_argument("--modes", required=True,
                          help='JSON array, e.g. ["recorded-actions"]')
    register.add_argument("--registered-by", default="local-operator")
    register.set_defaults(func=_cmd_register)

    listed = subparsers.add_parser("env-list", help="List registered environments")
    listed.add_argument("--db", type=Path, default=DEFAULT_DB)
    listed.add_argument("--allow-prefix", action="append", default=None,
                        help="Extra reviewed adapter module prefix.")
    listed.set_defaults(func=_cmd_list)

    imported = subparsers.add_parser(
        "env-import-manifest",
        help="Validate and store a JSON manifest (never executes the env)")
    imported.add_argument("--db", type=Path, default=DEFAULT_DB)
    imported.add_argument("--allow-prefix", action="append", default=None,
                          help="Extra reviewed adapter module prefix.")
    imported.add_argument("--manifest", type=Path, required=True)
    imported.set_defaults(func=_cmd_import_manifest)

    capabilities = subparsers.add_parser(
        "env-capabilities", help="Discover adapter capabilities (describe only)")
    capabilities.add_argument("--db", type=Path, default=DEFAULT_DB)
    capabilities.add_argument("--allow-prefix", action="append", default=None,
                              help="Extra reviewed adapter module prefix.")
    capabilities.add_argument("--env-id", required=True)
    capabilities.add_argument("--manifest-digest", default=None)
    capabilities.set_defaults(func=_cmd_capabilities)

    qualify = subparsers.add_parser(
        "env-qualify", help="Run explicit qualification (executes the env)")
    qualify.add_argument("--db", type=Path, default=DEFAULT_DB)
    qualify.add_argument("--allow-prefix", action="append", default=None,
                         help="Extra reviewed adapter module prefix.")
    qualify.add_argument("--env-id", required=True)
    qualify.add_argument("--manifest-digest", default=None)
    qualify.add_argument("--fixture", default=None,
                         help="JSON object fixture for non-refund envs")
    qualify.set_defaults(func=_cmd_qualify)

    report = subparsers.add_parser("env-report",
                                   help="Show the latest qualification report")
    report.add_argument("--db", type=Path, default=DEFAULT_DB)
    report.add_argument("--allow-prefix", action="append", default=None,
                        help="Extra reviewed adapter module prefix.")
    report.add_argument("--env-id", required=True)
    report.set_defaults(func=_cmd_report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GhostLab trusted local environment integration"
                    f" (schema {ENVS_SCHEMA_VERSION})")
    add_env_commands(parser.add_subparsers(dest="command", required=True))
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, f"Invalid environment request: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
