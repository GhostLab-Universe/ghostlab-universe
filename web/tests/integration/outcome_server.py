"""M07's fixture server, with scripted replies that reach the unfinished outcomes.

Everything except the Explorer's reply is M07's and M05's real code. Replies are
checked-in fixtures, never a model, and /v1/session still reports inference: "fixture".
A campaign picks its path with a marker in its objective, the same trick M07's fixture
server uses to tell worlds apart.

    GHOSTLAB_AUTH_MODE=local_dev python web/tests/integration/outcome_server.py \
        --m07 <checkout of feature/m07-api> --root <dir> --port 8788
"""

import argparse
import functools
import itertools
import json
import sys
from pathlib import Path

import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--m07", type=Path, required=True)
parser.add_argument("--root", required=True)
parser.add_argument("--port", default="8788")
args = parser.parse_args()
sys.path.insert(0, str(args.m07 / "tests" / "api"))
sys.argv = [sys.argv[0], "--root", args.root, "--port", args.port]

import fixture_server  # noqa: E402
from ghostlab.orchestration.worlds import DEFAULT_WORLDS, JourneyDriver  # noqa: E402
from ghostlab.worlds.journey import JourneyFixture  # noqa: E402
from m05_harness import meta_reply  # noqa: E402
from m05_journey_harness import J, ORGANIZER, JourneyLab, proposal_json  # noqa: E402

REVOKE_ORGANIZER = [{"fault_id": "sharing_revoked_while_queued", "after_event": 2,
                     "parameters": [{"name": "recipient_id", "value": ORGANIZER}]}]
default_reply = fixture_server.scripted_reply
truncated_caps = itertools.cycle((3, 4))  # a second iteration may not repeat the first


def scripted_reply(request: httpx.Request) -> httpx.Response:
    if b"outcome-truncated" in request.content:
        chosen = proposal_json(cap=next(truncated_caps))
    elif b"outcome-violation" in request.content:
        chosen = proposal_json(faults=REVOKE_ORGANIZER, cap=11)
    elif b"outcome-short-repair" in request.content:
        chosen = proposal_json(cap=12)
    else:
        return default_reply(request)
    status, body = meta_reply(json.dumps(chosen))
    return httpx.Response(status, json=body)


class OneShotDriver(JourneyDriver):
    """Reports a changed fixture exactly once, so the next check sees the settings restored."""

    armed = False

    def current_fixture(self, services):
        if self.armed:
            self.armed = False
            return JourneyFixture(journey_id="journey-changed")
        return super().current_fixture(services)


driver = OneShotDriver()
armed_campaigns = set()


def arm_settings_change(name, state):
    objective = state["spec"]["objective"]
    wanted = {"outcome-settings-at-consume": "consume",
              "outcome-settings-at-resume": "await_approval"}
    for marker, node in wanted.items():
        if marker in objective and name == node and state["campaign_id"] not in armed_campaigns:
            armed_campaigns.add(state["campaign_id"])
            driver.armed = True


fixture_server.scripted_reply = scripted_reply
fixture_server.JourneyLab = functools.partial(
    JourneyLab, worlds={**DEFAULT_WORLDS, J: driver}, on_node=arm_settings_change)

if __name__ == "__main__":
    sys.exit(fixture_server.main())
