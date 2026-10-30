"""Relay campaigns on REAL M04 SQLite stores and the REAL M01 adapter.

Inference is a labeled fixture (httpx.MockTransport): no network request is made
and no model is called. Worlds, adapters, verifier and capsules are the real code.
"""

import dataclasses
import json

from ghostlab.contracts.world import WorldCampaignSpec
from ghostlab.policy import StaticWorldBinding

from m05_harness import OPERATOR, PROJECT, meta_reply
from m05_journey_harness import JourneyLab

R = "service-relay/1"
AUTOMATION = "relay-automation"
BUSINESS_OWNER = "owner-1"
LOCAL, CHECKED = "relay-local-success", "relay-lease-checked"
FIRST_CONFIRM_EVENT = 5  # the lease-checked adapter's first confirmation in a clean run


def fault(fault_id, after_event=None, **parameters):
    return {"fault_id": fault_id, "after_event": after_event,
            "parameters": [{"name": k, "value": v} for k, v in sorted(parameters.items())]}


TAKEOVER = fault("takeover_with_queued_confirmation")
LOST_ACK = fault("lost_reservation_ack")
COMPETITION = fault("stale_availability_competition", competing_jobs=2)
TAKEOVER_RACE = fault("takeover_with_queued_confirmation", FIRST_CONFIRM_EVENT - 1)
CAPACITY_RACE = fault("stale_availability_competition", 1, competing_jobs=2)


def scenario(faults=None, *, cap=20):
    return {"schema_version": 1, "world_version": R, "max_tool_calls": cap,
            "faults": [TAKEOVER] if faults is None else faults}


def proposal_json(adapter=LOCAL, *, faults=None, cap=20, **changes):
    return {"schema_version": 1, "world_version": R,
            "hypothesis": "The front desk confirms a job the owner has just taken over",
            "adapter_id": adapter, "scenario": scenario(faults, cap=cap),
            "expected_signal": "no_automation_after_takeover fails", **changes}


def relay_reply(*args, **kwargs):
    return meta_reply(json.dumps(proposal_json(*args, **kwargs)))


def relay_spec(**budget):
    limits = {"max_model_calls": 10, "max_tool_calls": 100, "max_input_tokens": 100_000,
              "max_output_tokens": 20_000, "max_cost_minor": None, "cost_currency": None,
              "max_wall_clock_ms": 20 * 60_000, "max_parallel_runs": 2, **budget}
    return WorldCampaignSpec.model_validate({
        "schema_version": 1, "objective": "Find an automated change after an owner takeover",
        "success_criteria": ["A failing invariant is reproduced and a repair is verified"],
        "world_version": R, "budget": limits})


class RelayLab(JourneyLab):
    def __init__(self, root, script, *, world_binding=None, relay_fixture=None, **options):
        super().__init__(root, script, world_binding=world_binding or StaticWorldBinding(
            {(PROJECT, OPERATOR.principal_id, R): AUTOMATION}), **options)
        self.relay_fixture = relay_fixture

    def services(self):
        services = super().services()
        if self.relay_fixture is not None:
            services = dataclasses.replace(services, relay_fixture=self.relay_fixture)
        return services
