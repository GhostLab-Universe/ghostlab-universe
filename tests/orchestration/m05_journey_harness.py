"""Journey campaigns on REAL M04 SQLite stores and the REAL M01 adapter.

Inference is a labeled fixture (httpx.MockTransport): no network request is made
and no model is called. Worlds, adapters, verifier and capsules are the real code.
"""

import dataclasses
import json

from ghostlab.contracts.world import WorldCampaignSpec
from ghostlab.policy import StaticWorldBinding

from m05_harness import OPERATOR, PROJECT, Lab, meta_reply

J = "group-journey/1"
ORGANIZER = "traveller-a"
VIEWER = "participant-c"
NAIVE, CHECKED = "journey-naive", "journey-version-checked"
RACE_AFTER_EVENT = 17  # the version-checked adapter's last permission check before delivery
DELIVERY_RACE_EVENT = 18


def revocation(after_event=None):
    return {"fault_id": "sharing_revoked_while_queued", "after_event": after_event,
            "parameters": [{"name": "recipient_id", "value": VIEWER}]}


def scenario(faults=None, *, cap=20):
    return {"schema_version": 1, "world_version": J, "max_tool_calls": cap,
            "faults": [revocation()] if faults is None else faults}


def proposal_json(adapter=NAIVE, *, faults=None, cap=20, **changes):
    return {"schema_version": 1, "world_version": J,
            "hypothesis": "A message queued before revocation is still delivered afterwards",
            "adapter_id": adapter, "scenario": scenario(faults, cap=cap),
            "expected_signal": "delivered_messages_permitted fails", **changes}


def journey_reply(*args, **kwargs):
    return meta_reply(json.dumps(proposal_json(*args, **kwargs)))


def journey_spec(**budget):
    limits = {"max_model_calls": 10, "max_tool_calls": 100, "max_input_tokens": 100_000,
              "max_output_tokens": 20_000, "max_cost_minor": None, "cost_currency": None,
              "max_wall_clock_ms": 20 * 60_000, "max_parallel_runs": 2, **budget}
    return WorldCampaignSpec.model_validate({
        "schema_version": 1, "objective": "Find a delivery after sharing was withdrawn",
        "success_criteria": ["A failing invariant is reproduced and a repair is verified"],
        "world_version": J, "budget": limits})


class JourneyLab(Lab):
    def __init__(self, root, script, *, world_binding=None, worlds=None, journey_fixture=None,
                 refund_fixture=None, **options):
        super().__init__(root, script, **options)
        self.journey_fixture, self.refund_fixture = journey_fixture, refund_fixture
        self.world_binding = world_binding or StaticWorldBinding(
            {(PROJECT, OPERATOR.principal_id, J): ORGANIZER})
        self.worlds = worlds

    def services(self):
        services = dataclasses.replace(super().services(), world_binding=self.world_binding)
        if self.worlds is not None:
            services = dataclasses.replace(services, worlds=self.worlds)
        if self.journey_fixture is not None:
            services = dataclasses.replace(services, journey_fixture=self.journey_fixture)
        if self.refund_fixture is not None:
            services = dataclasses.replace(services, fixture=self.refund_fixture)
        return services
