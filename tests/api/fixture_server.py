"""A loopback server for interface work, with scripted inference. Not a model and not a demo.

Every Explorer reply is a checked-in fixture on an httpx.MockTransport, chosen by world.
``/v1/session`` reports ``inference: "fixture"`` and ``auth_mode: "local_dev"``, and an
interface must show both. Worlds, stores, approvals and capsules are the real code.

    GHOSTLAB_AUTH_MODE=local_dev python tests/api/fixture_server.py --root <dir> --port 8788
"""

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestration"))

from ghostlab.api import create_app  # noqa: E402
from ghostlab.api.plane import ApiConfig  # noqa: E402
from ghostlab.api.sessions import LocalDevSessions  # noqa: E402
from ghostlab.policy import (ApprovalService, ProjectApproverAllowlist,  # noqa: E402
                             StaticPrincipalBinding, StaticWorldBinding)
from ghostlab.security.local_dev import LocalDevRefused  # noqa: E402
from m05_harness import VULNERABLE, meta_reply  # noqa: E402
from m05_journey_harness import J, ORGANIZER, JourneyLab, proposal_json  # noqa: E402


def scripted_reply(request: httpx.Request) -> httpx.Response:
    proposal = proposal_json() if b"group-journey" in request.content else VULNERABLE
    status, body = meta_reply(json.dumps(proposal))
    return httpx.Response(status, json=body)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fixture_config(root: Path, host: str) -> ApiConfig:
    sessions = LocalDevSessions.from_environment(host)
    project, principal = sessions.project_id, sessions.principal.principal_id
    approvers = ProjectApproverAllowlist({project: {principal}})
    lab = JourneyLab(root, scripted_reply, approvers=approvers,
                     binding=StaticPrincipalBinding({(project, principal):
                                                     "synthetic-customer-1"}),
                     world_binding=StaticWorldBinding({(project, principal, J): ORGANIZER}))
    lab.approvals = ApprovalService(lab.approval_store, approvers=approvers, local_dev_mode=True)
    services = dataclasses.replace(lab.services(), clock=utc_now)
    return ApiConfig(services=services, sessions=sessions, approvers=approvers,
                     graph_db=lab.paths.graph_db, app_db=lab.paths.app_db, bind_host=host)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    try:
        app = create_app(fixture_config(args.root, args.host))
    except LocalDevRefused as refused:
        print(f"Refused: {refused}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
