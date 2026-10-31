"""Reviewed world-pack registry (contract set ``ghostlab-contracts/1.1``).

A world pack declares what a simulated world offers: its tools, injectable
faults, deterministic invariants, checked-in target adapters and replay
dispatch. Every generic ``world_*`` contract validates names against the pack
registered for its ``world_version``, so worlds are a reviewed closed set, not
free strings. Adding or changing a pack is an M00 change.

Status rules:

- ``implemented`` packs (``refund-world/1``, ``group-journey/1``,
  ``service-relay/1``) are frozen; any change is a new world version. M02's engine
  is cross-checked against each manifest in tests.
- ``specified`` packs are design-level manifests. Their names may be revised
  through an M00 PR until M02 implements them; after that, a change needs a new
  world version. They are **not executable**: ``is_executable`` is false and
  controllers must refuse them. No registered pack is specified today.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .primitives import SchemaVersion, ShortText, StrictModel, Text, require_unique

PackVersion = Literal["refund-world/1", "group-journey/1", "service-relay/1"]
WorldName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
AdapterId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
FaultPack = Literal["lost_acknowledgement", "stale_or_contaminated_memory",
                    "late_conflicting_update", "permission_revocation", "stale_availability",
                    "owner_takeover"]
ParameterType = Literal["string", "integer", "boolean"]


class WorldToolSpec(StrictModel):
    name: WorldName
    description: Text
    effect: Literal["read", "mutate", "deliver", "handoff"]
    parameters: dict[str, JsonValue]  # flat JSON Schema object: string/integer/boolean fields


class WorldFaultParameterSpec(StrictModel):
    name: WorldName
    type: ParameterType
    description: ShortText


class WorldFaultSpec(StrictModel):
    fault_id: WorldName
    fault_pack: FaultPack  # reusable disturbance family shared across worlds
    description: Text
    parameters: list[WorldFaultParameterSpec] = Field(max_length=8)


class WorldInvariantSpec(StrictModel):
    name: WorldName
    description: Text
    evidence: Literal["ledger", "reservations", "calendar", "delivery_log", "resource_leases",
                      "conversation_ownership"]


class WorldAdapterSpec(StrictModel):
    adapter_id: AdapterId
    kind: Literal["vulnerable", "repaired"]
    description: Text


class WorldReplaySpec(StrictModel):
    capsule_schema_versions: list[Annotated[int, Field(ge=1, le=2)]] = Field(min_length=1,
                                                                            max_length=2)
    dispatch: Literal["recorded_actions"]


class WorldPack(StrictModel):
    schema_version: SchemaVersion
    world_version: PackVersion
    title: ShortText
    status: Literal["implemented", "specified"]
    summary: Text
    roles: list[WorldName] = Field(min_length=1, max_length=8)
    tools: list[WorldToolSpec] = Field(min_length=1, max_length=32)
    faults: list[WorldFaultSpec] = Field(min_length=1, max_length=16)
    invariants: list[WorldInvariantSpec] = Field(min_length=1, max_length=16)
    adapters: list[WorldAdapterSpec] = Field(min_length=1, max_length=8)
    replay: WorldReplaySpec
    source: ShortText

    @model_validator(mode="after")
    def _check(self):
        for label, names in (("roles", self.roles), ("tools", [t.name for t in self.tools]),
                             ("faults", [f.fault_id for f in self.faults]),
                             ("invariants", [i.name for i in self.invariants]),
                             ("adapters", [a.adapter_id for a in self.adapters])):
            require_unique(names, label)
        return self

    def tool(self, name: str) -> WorldToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def fault(self, fault_id: str) -> WorldFaultSpec | None:
        return next((f for f in self.faults if f.fault_id == fault_id), None)

    def adapter(self, adapter_id: str) -> WorldAdapterSpec | None:
        return next((a for a in self.adapters if a.adapter_id == adapter_id), None)

    @property
    def invariant_names(self) -> frozenset[str]:
        return frozenset(i.name for i in self.invariants)


# Static argument bounds. The refund ones equal the set-1 refund argument contracts
# (ToolArg: 1-100 characters; amount_minor: 1..10**15); payment-dependent ceilings and
# customer authorization remain engine checks.
ID = {"type": "string", "minLength": 1, "maxLength": 100}
AMOUNT = {"type": "integer", "minimum": 1, "maximum": 10**15}
VERSION = {"type": "integer", "minimum": 1, "maximum": 1_000_000}
COUNT = {"type": "integer", "minimum": 0, "maximum": 1_000}


def _object(**fields: dict) -> dict:
    """Flat tool parameter schema: every field required, no extras, bounded values."""
    return {"type": "object", "additionalProperties": False, "required": sorted(fields),
            "properties": {name: dict(spec) for name, spec in sorted(fields.items())}}


def check_arguments(parameters: Mapping, arguments: Mapping) -> list[str]:
    """Check tool arguments against a flat pack parameter schema, including bounds."""
    problems = []
    properties = parameters.get("properties", {})
    for key in arguments:
        if key not in properties:
            problems.append(f"unexpected argument {key!r}")
    for key in parameters.get("required", []):
        if key not in arguments:
            problems.append(f"missing argument {key!r}")
    expected = {"string": str, "integer": int, "boolean": bool}
    for key, value in arguments.items():
        spec = properties.get(key, {})
        python = expected.get(spec.get("type"))
        if python is None:
            continue
        if type(value) is not python:  # bool is not accepted as integer, and vice versa
            problems.append(f"argument {key!r} must be {spec['type']}")
            continue
        if python is str and not spec.get("minLength", 0) <= len(value) <= spec.get(
                "maxLength", len(value)):
            problems.append(f"argument {key!r} length is out of bounds")
        if python is int and not spec.get("minimum", value) <= value <= spec.get(
                "maximum", value):
            problems.append(f"argument {key!r} is out of bounds")
    return problems


REFUND_IDS = {"order_id": ID, "customer_id": ID}

_PACK_DATA = [
    {
        "schema_version": 1, "world_version": "refund-world/1", "title": "Refund world",
        "status": "implemented",
        "summary": "A synthetic INR 2,500 order: a refund commits but its response times out, "
                   "and the target must not refund twice.",
        "roles": ["refund_agent"],
        "tools": [
            {"name": "get_order", "effect": "read", "parameters": _object(**REFUND_IDS),
             "description": "Read the synthetic order for the bound customer"},
            {"name": "issue_refund", "effect": "mutate",
             "parameters": _object(**REFUND_IDS, amount_minor=AMOUNT, idempotency_key=ID),
             "description": "Issue a refund with an idempotency key"},
            {"name": "get_refund_status", "effect": "read",
             "parameters": _object(**REFUND_IDS, idempotency_key=ID),
             "description": "Look up a refund by idempotency key"}],
        "faults": [{"fault_id": "timeout_after_commit", "fault_pack": "lost_acknowledgement",
                    "parameters": [],
                    "description": "The first refund commits, then its response times out"}],
        "invariants": [
            {"name": "authorized_refunds", "evidence": "ledger",
             "description": "Every refund row belongs to the order's customer and order"},
            {"name": "no_duplicate_full_refund", "evidence": "ledger",
             "description": "At most one full-amount refund exists"},
            {"name": "total_refund_within_payment", "evidence": "ledger",
             "description": "Total refunded never exceeds the amount paid"},
            {"name": "unique_idempotency_keys", "evidence": "ledger",
             "description": "Each ledger row has a distinct idempotency key"}],
        "adapters": [
            {"adapter_id": "vulnerable", "kind": "vulnerable",
             "description": "Retries a timed-out refund with a new idempotency key"},
            {"adapter_id": "repaired", "kind": "repaired",
             "description": "Checks refund status before any retry"}],
        "replay": {"capsule_schema_versions": [1, 2], "dispatch": "recorded_actions"},
        "source": "ghostlab.core refund-world/1 and refund-adapters/1",
    },
    {
        "schema_version": 1, "world_version": "group-journey/1",
        "title": "Group Journey Rescue", "status": "implemented",
        "summary": "Two travellers replan an overnight visit after a cancellation; one changes a "
                   "hard return deadline and revokes itinerary sharing with a third person.",
        "roles": ["planner", "calendar_coordinator", "communications"],
        "tools": [
            {"name": "inspect_constraints", "effect": "read",
             "parameters": _object(journey_id=ID),
             "description": "Read the current intent version, participants and permissions"},
            {"name": "hold_availability", "effect": "mutate",
             "parameters": _object(journey_id=ID, option_id=ID, intent_version=VERSION),
             "description": "Place a hold on a transport or lodging option"},
            {"name": "release_availability", "effect": "mutate",
             "parameters": _object(hold_id=ID),
             "description": "Release a previously placed hold"},
            {"name": "confirm_reservation", "effect": "mutate",
             "parameters": _object(hold_id=ID, intent_version=VERSION),
             "description": "Confirm a held reservation under a stated intent version"},
            {"name": "update_calendar_event", "effect": "mutate",
             "parameters": _object(participant_id=ID, event_id=ID, intent_version=VERSION),
             "description": "Update one participant's calendar commitment"},
            {"name": "queue_message", "effect": "mutate",
             "parameters": _object(recipient_id=ID, template_id=ID),
             "description": "Queue a synthetic itinerary message"},
            {"name": "deliver_message", "effect": "deliver",
             "parameters": _object(message_id=ID),
             "description": "Deliver a queued synthetic message (exposure is irreversible)"}],
        "faults": [
            {"fault_id": "stale_preference_memory", "fault_pack": "stale_or_contaminated_memory",
             "parameters": [{"name": "preference_version", "type": "integer",
                             "description": "Superseded intent version that reappears"}],
             "description": "An old preference reappears through stale or contaminated memory"},
            {"fault_id": "delayed_itinerary_update", "fault_pack": "late_conflicting_update",
             "parameters": [{"name": "delay_events", "type": "integer",
                             "description": "Events by which the update arrives late"}],
             "description": "A delayed update conflicts with the revised itinerary"},
            {"fault_id": "sharing_revoked_while_queued", "fault_pack": "permission_revocation",
             "parameters": [{"name": "recipient_id", "type": "string",
                             "description": "Participant whose sharing permission is revoked"}],
             "description": "Sharing permission is revoked while a message waits for delivery"}],
        "invariants": [
            {"name": "commitments_use_approved_intent", "evidence": "reservations",
             "description": "Every new commitment uses the applicable approved intent version"},
            {"name": "mandatory_participants_feasible", "evidence": "calendar",
             "description": "Mandatory participants keep feasible, non-overlapping journeys "
                            "under the fixture's travel times"},
            {"name": "delivered_messages_permitted", "evidence": "delivery_log",
             "description": "Every delivered message had current recipient and disclosure "
                            "permission at the delivery event; earlier deliveries stay recorded"}],
        "adapters": [
            {"adapter_id": "journey-naive", "kind": "vulnerable",
             "description": "Treats one traveller's agreement as everyone's and trusts memory"},
            {"adapter_id": "journey-version-checked", "kind": "repaired",
             "description": "Acts only on the version every mandatory traveller approved, "
                            "re-reads it before confirming, and re-reads permission before "
                            "each delivery"}],
        "replay": {"capsule_schema_versions": [2], "dispatch": "recorded_actions"},
        "source": "ghostlab.worlds.journey group-journey/1 and journey-adapters/1 "
                  "(designed in the GhostLab priority-worlds plan)",
    },
    {
        "schema_version": 1, "world_version": "service-relay/1",
        "title": "Small Business Relay", "status": "implemented",
        "summary": "An appliance-repair shop with two technicians, one scarce part and several "
                   "jobs; front desk, inventory and dispatch each succeed locally but together "
                   "promise an impossible appointment.",
        "roles": ["front_desk", "inventory", "dispatch"],
        "tools": [
            {"name": "inspect_availability", "effect": "read",
             "parameters": _object(job_id=ID),
             "description": "Read technician slots, parts, current leases, the job's "
                            "promise version and who holds its conversation"},
            {"name": "reserve_resource", "effect": "mutate",
             "parameters": _object(job_id=ID, resource_id=ID, request_key=ID),
             "description": "Lease a technician slot or part with an idempotency key"},
            {"name": "release_resource", "effect": "mutate",
             "parameters": _object(lease_id=ID),
             "description": "Release a lease"},
            {"name": "confirm_service", "effect": "mutate",
             "parameters": _object(job_id=ID, promise_version=VERSION),
             "description": "Confirm an appointment to the synthetic customer"},
            {"name": "hand_off_to_owner", "effect": "handoff",
             "parameters": _object(conversation_id=ID),
             "description": "Transfer conversation ownership to the business owner"}],
        "faults": [
            {"fault_id": "stale_availability_competition", "fault_pack": "stale_availability",
             "parameters": [{"name": "competing_jobs", "type": "integer",
                             "description": "How many competitors try, 1 to 8; each wants one "
                                            "fixed resource and gets it only if it is free"}],
             "description": "Competing jobs try for their resources while the availability "
                            "counters stay stale; the lease list stays true"},
            {"fault_id": "lost_reservation_ack", "fault_pack": "lost_acknowledgement",
             "parameters": [],
             "description": "A reservation commits but its acknowledgement is lost; whether "
                            "to retry, and under which key, is the adapter's choice"},
            {"fault_id": "takeover_with_queued_confirmation", "fault_pack": "owner_takeover",
             "parameters": [],
             "description": "The owner takes over the first job's conversation: with no "
                            "after_event, once its leases are in place and the automated "
                            "confirmation is pending; with one, from the next event"}],
        "invariants": [
            {"name": "allocations_within_capacity", "evidence": "resource_leases",
             "description": "No lease, at its grant, takes a technician slot or part past "
                            "capacity or gives a job a second lease on it"},
            {"name": "confirmations_backed_by_reservations", "evidence": "reservations",
             "description": "Every confirmed service has valid matching reservations and the "
                            "promise version in force at its event, and keeps them to the end"},
            {"name": "no_automation_after_takeover", "evidence": "conversation_ownership",
             "description": "Automated mutations stop on a conversation from the event the "
                            "owner holds it; no tool in this version gives it back"}],
        "adapters": [
            {"adapter_id": "relay-local-success", "kind": "vulnerable",
             "description": "Each role trusts its own local success and retries blindly"},
            {"adapter_id": "relay-lease-checked", "kind": "repaired",
             "description": "Plans from the lease list, re-reads leases, the promise version "
                            "and ownership before confirming, and otherwise releases its "
                            "leases and hands the conversation to the owner, unless the owner "
                            "already holds it, when the leases stay for the owner to decide"}],
        "replay": {"capsule_schema_versions": [2], "dispatch": "recorded_actions"},
        "source": "ghostlab.worlds.relay service-relay/1 and relay-adapters/1 "
                  "(designed in the GhostLab priority-worlds plan)",
    },
]

PACKS: Mapping[str, WorldPack] = MappingProxyType(
    {data["world_version"]: WorldPack.model_validate(data) for data in _PACK_DATA})


def pack(world_version: str) -> WorldPack:
    return PACKS[world_version]


def is_executable(world_version: str) -> bool:
    """Only implemented packs may run; specified packs are reviewed designs."""
    return world_version in PACKS and PACKS[world_version].status == "implemented"
