"""Serve the control plane in local-development mode. Not production authentication.

    GHOSTLAB_AUTH_MODE=local_dev python -m ghostlab.api --port 8787
    GHOSTLAB_AUTH_MODE=local_dev python -m ghostlab.api --scripted --port 8787

One server per storage root; a second one is refused. Live inference stays disabled.
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
from ghostlab.security.local_dev import LocalDevRefused
from ghostlab.storage.database import StoragePaths

from .app import create_app
from .local import local_config
from .rootlock import RootLock, RootLocked
from .sessions import DEFAULT_PROJECT, LocalDevSessions
from .tracing import drop_provider_keys

DEFAULT_DATABASE = Path("artifacts/local/ghostlab.sqlite")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--root", type=Path, help="default: GHOSTLAB_SQLITE_PATH's directory")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--scripted", action="store_true",
                        help="serve checked-in proposals: no model, no network, no key")
    args = parser.parse_args(argv)
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
        sessions = LocalDevSessions.from_environment(args.host, project_id=args.project)
        with RootLock(args.root):  # a clear refusal; the server takes its own at startup
            pass
        app = create_app(local_config(args.root, sessions, bind_host=args.host,
                                      scripted=args.scripted))
    except (LocalDevRefused, RootLocked) as refused:
        print(f"Refused: {refused}", file=sys.stderr)
        return 2
    if args.scripted:
        print("Scripted inference: checked-in proposals, not a model.", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
