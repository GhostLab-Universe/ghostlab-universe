"""Serve the control plane in local-development mode. Not production authentication.

    GHOSTLAB_AUTH_MODE=local_dev python -m ghostlab.api --port 8787

One process only: running campaigns are tracked in memory. Inference stays disabled.
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from ghostlab.security.local_dev import LocalDevRefused

from .app import create_app
from .local import local_config
from .sessions import DEFAULT_PROJECT, LocalDevSessions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get(
        "GHOSTLAB_SQLITE_PATH", "artifacts/local/ghostlab.sqlite")).parent)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args(argv)
    try:
        sessions = LocalDevSessions.from_environment(args.host, project_id=args.project)
        app = create_app(local_config(args.root, sessions, bind_host=args.host))
    except LocalDevRefused as refused:
        print(f"Refused: {refused}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
