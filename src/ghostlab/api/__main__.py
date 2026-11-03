"""Serve the control plane in local-development mode. Not production authentication.

    GHOSTLAB_AUTH_MODE=local_dev python -m ghostlab.api --port 8787
    GHOSTLAB_AUTH_MODE=local_dev python -m ghostlab.api --scripted --port 8787

One server per storage root; a second one is refused. Inference is disabled
unless --live names the qualified model, a call cap, a passing live
compatibility report and --confirm-paid-calls; --live and --scripted are
mutually exclusive.
With ``--scripted``, Explorer replies are checked-in proposals: no model, no network and
no key, and every campaign is labelled ``inference: "scripted"``.
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn
from pydantic import TypeAdapter, ValidationError

from ghostlab.contracts.primitives import Identifier
from ghostlab.orchestration import ledger_key
from ghostlab.orchestration.inference import LiveInferenceRefused
from ghostlab.providers import ProviderConfigError
from ghostlab.security.local_dev import LocalDevRefused
from ghostlab.storage.database import StoragePaths

from .app import create_app
from .live import live_config
from .local import local_config
from .rootlock import RootLock, RootLocked
from .sessions import DEFAULT_PROJECT, LocalDevSessions
from .oauth import OAuthSessions
from .tracing import drop_provider_keys

DEFAULT_DATABASE = Path("artifacts/local/ghostlab.sqlite")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", choices=["local", "oauth"], default="local", help="OAuth requires configured GitHub/Google clients and member assignments")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--root", type=Path, help="default: GHOSTLAB_SQLITE_PATH's directory")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--scripted", action="store_true",
                        help="serve checked-in proposals: no model, no network, no key")
    parser.add_argument("--live", action="store_true",
                        help="live Meta inference through the qualified path (paid calls)")
    parser.add_argument("--live-model", default=None,
                        help="exact qualified model id (live only)")
    parser.add_argument("--live-max-calls", type=int, default=None,
                        help="spending cap in HTTP attempts, 1..campaign max (live only)")
    parser.add_argument("--live-compat-report", type=Path, default=None,
                        help="passing live M01 compatibility report for the model (live only)")
    parser.add_argument("--confirm-paid-calls", action="store_true",
                        help="acknowledge that live calls may be billed (live only)")
    parser.add_argument("--allow-server-retention", action="store_true",
                        help="allow the provider to retain prompts server-side (live only)")
    args = parser.parse_args(argv)
    if args.live and args.scripted:
        print("Refused: --live and --scripted are mutually exclusive", file=sys.stderr)
        return 2
    if args.live:
        missing = [flag for flag, ok in (
            ("--live-model", args.live_model),
            ("--live-max-calls", args.live_max_calls),
            ("--live-compat-report", args.live_compat_report),
            ("--confirm-paid-calls", args.confirm_paid_calls)) if not ok]
        if missing:
            print(f"Refused: --live also needs {', '.join(missing)}", file=sys.stderr)
            return 2
    if args.root is None:
        database = Path(os.environ.get("GHOSTLAB_SQLITE_PATH", DEFAULT_DATABASE))
        # Every reader of a root expects this name, so any other would split the root in two.
        if database.name != StoragePaths(database.parent).app_db.name:
            print(f"Refused: GHOSTLAB_SQLITE_PATH must name {DEFAULT_DATABASE.name}; use its "
                  "directory to choose where the server keeps its files", file=sys.stderr)
            return 2
        args.root = database.parent
    if args.scripted:
        drop_provider_keys()
    try:
        # The longest key a campaign in this project will need, so a bad id fails here.
        TypeAdapter(Identifier).validate_python(ledger_key(args.project, f"c-{'0' * 32}"))
    except ValidationError:
        print("Refused: --project is not a usable project id", file=sys.stderr)
        return 2
    try:
        sessions = (OAuthSessions.from_environment() if args.auth == "oauth" else
                    LocalDevSessions.from_environment(args.host, project_id=args.project))
        with RootLock(args.root):  # a clear refusal; the server takes its own at startup
            pass
        if args.live:
            config = live_config(
                args.root, sessions, bind_host=args.host, model=args.live_model,
                max_calls=args.live_max_calls, compat_report=args.live_compat_report,
                confirm_paid_calls=args.confirm_paid_calls,
                allow_server_retention=args.allow_server_retention)
        else:
            config = local_config(args.root, sessions, bind_host=args.host,
                                  scripted=args.scripted)
        app = create_app(config)
    except (LocalDevRefused, RootLocked, LiveInferenceRefused,
            ProviderConfigError, ValueError) as refused:
        print(f"Refused: {refused}", file=sys.stderr)
        return 2
    if args.scripted:
        print("Scripted inference: checked-in proposals, not a model.", file=sys.stderr)
    if args.live:
        print(f"Live inference: {args.live_model}, at most {args.live_max_calls} HTTP attempts; "
              f"server-side retention {'allowed' if args.allow_server_retention else 'refused'}.",
              file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
