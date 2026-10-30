"""Small Business Relay (``service-relay/1``): a trusted synthetic world, stdlib only.

An appliance-repair shop has two technician slots, one compressor part and two
jobs its automation books. Front desk, inventory and dispatch act in turn: leases
for a part and a technician, then a confirmation to the customer. Each step can
succeed locally while the appointment as a whole is impossible.

Faults:

- Competing jobs lease resources while the shop's availability counters stay
  frozen, so a role reading the counters plans on stale numbers. The lease list
  next to them stays true, so an agent that checks leases can always act safely.
- A reservation commits and its acknowledgement is lost. The lease exists; the
  caller just doesn't know. Retrying with the same request key is safe.
- The business owner takes over a job's conversation once its leases are in place,
  while the automated confirmation is still pending.

A takeover, a competing lease or an expiring lease can land on the very event of
the call that depends on it. An agent that checks and then acts keeps a one-event
window there, which only the guarded boundary closes.

Like the refund world, this world deliberately permits business mistakes, such as
overselling a resource or confirming after a takeover, so the independent verifier
can find them. Host checks are always on: the controller binds the actor, tool
arguments cannot change it, and a denied call records an error event with no state
change. The guarded execution boundary (``ghostlab.execution.relay_guard``) adds
world-rule enforcement on top.

Nothing reads a clock. Faults and lease expiry trigger on event indexes, so
recorded actions replay deterministically.
"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

WORLD_VERSION = "service-relay/1"

_ID = ("string", (1, 100))
_VERSION = ("integer", (1, 1_000_000))
# Mirrors the reviewed pack manifest (cross-checked in tests/simulation).
TOOL_SCHEMAS: dict[str, dict[str, tuple]] = {
    "inspect_availability": {"job_id": _ID},
    "reserve_resource": {"job_id": _ID, "resource_id": _ID, "request_key": _ID},
    "release_resource": {"lease_id": _ID},
    "confirm_service": {"job_id": _ID, "promise_version": _VERSION},
    "hand_off_to_owner": {"conversation_id": _ID},
}
READ_TOOLS = frozenset({"inspect_availability"})
FAULTS = {"stale_availability_competition": {"competing_jobs": int},
          "lost_reservation_ack": {},
          "takeover_with_queued_confirmation": {}}
MAX_FAULTS = 8
MAX_COMPETING_JOBS = 8
MAX_FIXTURE_ITEMS = 32

AUTOMATION = "automation"
BUSINESS_OWNER = "business_owner"


class ToolError(Exception):
    """A call the world refused. The class name is recorded in the trace."""


class BudgetExceeded(ToolError):
    pass


class HostDenied(ToolError):
    """Identity or reference check failed: always on, never a business outcome."""


class ReservationAckLost(ToolError):
    """The lease is committed; only its acknowledgement was lost."""


@dataclass(frozen=True)
class RelayResource:
    resource_id: str
    kind: str  # "technician" or "part"
    capacity: int


@dataclass(frozen=True)
class RelayJob:
    job_id: str
    conversation_id: str
    part: str | None  # the part this job needs, if any; every job needs a technician


@dataclass(frozen=True)
class RelayCompetitor:
    """A job the automation doesn't serve, wanting one resource under the fault."""

    job_id: str
    resource_id: str


@dataclass(frozen=True)
class RelayFixture:
    shop_id: str = "shop-1"
    operator: str = "relay-automation"  # the identity the target agent acts as
    business_owner: str = "owner-1"     # the human who can take a conversation over
    resources: tuple[RelayResource, ...] = (
        RelayResource("tech-a-0900", "technician", 1),
        RelayResource("tech-b-0900", "technician", 1),
        RelayResource("part-compressor", "part", 1),
    )
    jobs: tuple[RelayJob, ...] = (RelayJob("job-1", "conversation-1", "part-compressor"),
                                  RelayJob("job-2", "conversation-2", None))
    competitors: tuple[RelayCompetitor, ...] = (RelayCompetitor("job-3", "part-compressor"),
                                                RelayCompetitor("job-4", "tech-b-0900"))
    lease_ttl_events: int = 12          # a lease lapses this many events after its grant
    guarded: bool = False               # world-rule guard at the execution boundary

    def __post_init__(self):
        sized = (self.resources, self.jobs, self.competitors)
        if not all(type(group) is tuple and len(group) <= MAX_FIXTURE_ITEMS for group in sized):
            raise ValueError("Invalid fixture collections")
        if not (all(type(r) is RelayResource for r in self.resources)
                and all(type(j) is RelayJob for j in self.jobs)
                and all(type(c) is RelayCompetitor for c in self.competitors)):
            raise ValueError("Invalid fixture entries")
        names = [self.shop_id, self.operator, self.business_owner,
                 *(r.resource_id for r in self.resources), *(j.job_id for j in self.jobs),
                 *(j.conversation_id for j in self.jobs),
                 *(c.job_id for c in self.competitors)]
        if not all(type(n) is str and 1 <= len(n) <= 100 for n in names):
            raise ValueError("Invalid fixture identifier")
        if self.operator == self.business_owner:
            raise ValueError("The automation and the business owner must be distinct")
        numbers = [self.lease_ttl_events, *(r.capacity for r in self.resources)]
        if not all(type(n) is int and 1 <= n <= 1000 for n in numbers):
            raise ValueError("Fixture numbers must be integers from 1 to 1000")
        if type(self.guarded) is not bool:
            raise ValueError("guarded must be boolean")
        if not all(type(r.kind) is str and r.kind in {"technician", "part"}
                   for r in self.resources):
            raise ValueError("A resource is a technician slot or a part")
        for ids in ([r.resource_id for r in self.resources],
                    [j.job_id for j in self.jobs] + [c.job_id for c in self.competitors],
                    [j.conversation_id for j in self.jobs]):
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate fixture identifier")
        if not self.jobs or not self.technicians:
            raise ValueError("A shop needs at least one job and one technician slot")
        parts = {r.resource_id for r in self.resources if r.kind == "part"}
        for job in self.jobs:
            if job.part is not None and (type(job.part) is not str or job.part not in parts):
                raise ValueError("A job's part must be a part of this shop")
        known = {r.resource_id for r in self.resources}
        if not all(type(c.resource_id) is str and c.resource_id in known
                   for c in self.competitors):
            raise ValueError("A competitor must want a resource of this shop")

    @property
    def technicians(self) -> tuple[str, ...]:
        return tuple(r.resource_id for r in self.resources if r.kind == "technician")

    def resource(self, resource_id: str) -> RelayResource | None:
        return next((r for r in self.resources if r.resource_id == resource_id), None)

    def job(self, job_id: str) -> RelayJob | None:
        return next((j for j in self.jobs if j.job_id == job_id), None)

    def to_json(self) -> dict:
        return _listify(asdict(self))

    @classmethod
    def from_json(cls, data: dict) -> "RelayFixture":
        if type(data) is not dict or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("Invalid relay fixture")
        try:
            return cls(
                shop_id=data["shop_id"], operator=data["operator"],
                business_owner=data["business_owner"],
                resources=tuple(RelayResource(**r) for r in data["resources"]),
                jobs=tuple(RelayJob(**j) for j in data["jobs"]),
                competitors=tuple(RelayCompetitor(**c) for c in data["competitors"]),
                lease_ttl_events=data["lease_ttl_events"], guarded=data["guarded"])
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError(f"Invalid relay fixture: {error}") from error


def _listify(value):
    if isinstance(value, (list, tuple)):
        return [_listify(v) for v in value]
    if isinstance(value, dict):
        return {k: _listify(v) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class RelayScenario:
    """A parsed ``world_scenario/1`` for this world (event-indexed fault triggers)."""

    faults: tuple[tuple[str, int | None, tuple[tuple[str, Any], ...]], ...] = ()
    max_tool_calls: int = 40

    @classmethod
    def from_json(cls, data: dict) -> "RelayScenario":
        if (type(data) is not dict or data.get("world_version") != WORLD_VERSION
                or set(data) != {"schema_version", "world_version", "faults", "max_tool_calls"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1):
            raise ValueError("Invalid relay scenario")
        budget = data["max_tool_calls"]
        if type(budget) is not int or not 1 <= budget <= 1000:
            raise ValueError("max_tool_calls must be between 1 and 1000")
        faults = data["faults"]
        if type(faults) is not list or len(faults) > MAX_FAULTS:
            raise ValueError("Invalid fault list")
        parsed, seen = [], set()
        for fault in faults:
            if type(fault) is not dict or set(fault) != {"fault_id", "after_event", "parameters"}:
                raise ValueError("Invalid fault")
            fault_id, after = fault["fault_id"], fault["after_event"]
            if type(fault_id) is not str or fault_id not in FAULTS or fault_id in seen:
                raise ValueError(f"Unknown or repeated fault {fault_id!r}")
            seen.add(fault_id)
            if after is not None and (type(after) is not int or not 1 <= after <= 1000):
                raise ValueError("after_event must be an event index")
            entries = fault["parameters"]
            if type(entries) is not list or not all(
                    type(p) is dict and set(p) == {"name", "value"} and type(p["name"]) is str
                    for p in entries):
                raise ValueError("Invalid fault parameters")
            params = {p["name"]: p["value"] for p in entries}
            if len(params) != len(entries) or set(params) != set(FAULTS[fault_id]) or any(
                    type(params[k]) is not t for k, t in FAULTS[fault_id].items()):
                raise ValueError(f"Invalid parameters for {fault_id}")
            if fault_id == "stale_availability_competition" and (
                    not 1 <= params["competing_jobs"] <= MAX_COMPETING_JOBS):
                raise ValueError(f"competing_jobs must be 1 to {MAX_COMPETING_JOBS}")
            parsed.append((fault_id, after, tuple(sorted(params.items()))))
        return cls(tuple(parsed), budget)

    def params(self, fault_id: str) -> dict | None:
        for fid, after, params in self.faults:
            if fid == fault_id:
                return {"after_event": after, **dict(params)}
        return None


def scenario_json(faults=(), max_tool_calls: int = 40) -> dict:
    """``world_scenario/1`` JSON for this world; faults are (id, after_event, {params})."""
    return {"schema_version": 1, "world_version": WORLD_VERSION,
            "max_tool_calls": max_tool_calls,
            "faults": [{"fault_id": fid, "after_event": after,
                        "parameters": [{"name": k, "value": v}
                                       for k, v in sorted(params.items())]}
                       for fid, after, params in faults]}


class RelayWorld:
    """Authoritative shop state; one instance per baseline or faulted branch.

    Live fields (``leases``, ``promises``, ``conversations``) are what the guard
    reads. The append-only logs and timelines are what the verifier reads.
    """

    def __init__(self, fixture: RelayFixture, scenario: RelayScenario, *,
                 actor_id: str | None = None):
        self.fixture, self.scenario = fixture, scenario
        # The controller binds this principal; tool arguments cannot set it.
        self.actor_id = fixture.operator if actor_id is None else actor_id
        if type(self.actor_id) is not str or not 1 <= len(self.actor_id) <= 100:
            raise ValueError("Invalid actor_id")
        self.calls = 0
        self.trace: list[dict] = []
        self._competition_from: int | None = None  # the event competitors arrive at
        self._takeover_from: int | None = None     # the event the owner's takeover lands on
        self._takeover_scheduled = False
        self._ack_lost = False
        self.state: dict[str, Any] = {
            "shop_id": fixture.shop_id,
            "cached_availability": None,  # frozen counters while the fault is in force
            "leases": {}, "request_keys": {},
            "promises": {j.job_id: 1 for j in fixture.jobs},
            "conversations": {j.conversation_id: AUTOMATION for j in fixture.jobs},
            "confirmed_jobs": [],
            "lease_log": [], "confirmation_log": [], "mutation_log": [],
            "promise_timeline": [{"from_event": 1, "job_id": j.job_id, "version": 1}
                                 for j in fixture.jobs],
            "ownership_timeline": [{"from_event": 1, "conversation_id": j.conversation_id,
                                    "owner": AUTOMATION, "source": "fixture"}
                                   for j in fixture.jobs],
        }
        competition = scenario.params("stale_availability_competition")
        if competition:
            # By default the competitors are already there when the run starts, so an
            # agent that reads the leases never has to race them.
            self._competition_from = (competition["after_event"] or 0) + 1
        takeover = scenario.params("takeover_with_queued_confirmation")
        if takeover and takeover["after_event"] is not None:
            self._takeover_from = takeover["after_event"] + 1

    def snapshot(self) -> dict:
        return deepcopy(self.state)

    def call(self, tool: str, arguments: dict) -> dict:
        if self.calls >= self.scenario.max_tool_calls:
            raise BudgetExceeded("Tool call budget exhausted")
        self.calls += 1
        at = self.calls
        self._apply_pending(at)
        request = {"tool": tool, "arguments": deepcopy(arguments)}
        try:
            self._check_request(tool, arguments)
            self._precheck(at, tool, arguments)  # guard hook; a no-op in the lab world
            result = getattr(self, f"_{tool}")(at, **arguments)
        except ToolError as error:
            self.trace.append({"event_id": at, "request": request,
                               "outcome": {"error": type(error).__name__,
                                           "message": str(error)[:2000] or type(error).__name__},
                               "state": self.snapshot()})
            raise
        self.trace.append({"event_id": at, "request": request,
                           "outcome": {"result": deepcopy(result)}, "state": self.snapshot()})
        return deepcopy(result)

    # -- timeline ---------------------------------------------------------------------

    def _apply_pending(self, at: int):
        """Everything due at this event lands before the call is checked or acts."""
        for lease in self.state["leases"].values():
            if (lease["status"] == "active" and lease["expires_at"] is not None
                    and at >= lease["expires_at"]):
                lease["status"] = "expired"
                self._log_lease(at, "expired", lease, "world")
        if self._competition_from is not None and at >= self._competition_from:
            self._competition_from = None
            self._competitors_arrive(at)
        if self._takeover_from is not None and at >= self._takeover_from:
            self._takeover_from = None
            conversation = self.fixture.jobs[0].conversation_id
            if self.state["conversations"][conversation] == AUTOMATION:
                self.state["conversations"][conversation] = BUSINESS_OWNER
                self.state["ownership_timeline"].append(
                    {"from_event": at, "conversation_id": conversation,
                     "owner": BUSINESS_OWNER, "source": "takeover"})

    def _competitors_arrive(self, at):
        count = self.scenario.params("stale_availability_competition")["competing_jobs"]
        self.state["cached_availability"] = {"as_of_event": at - 1,
                                             "available": self._available_now()}
        for competitor in self.fixture.competitors[:count]:
            if self._free(competitor.resource_id) < 1:
                continue  # it read stale numbers too and lost the resource at the counter
            self._grant(at, competitor.job_id, competitor.resource_id, "competitor", None, "world")
            self._bump_promises(at, competitor.resource_id)

    def _bump_promises(self, at, resource_id):
        """A promise that relied on this resource no longer stands as made."""
        kind = self.fixture.resource(resource_id).kind
        for job in self.fixture.jobs:
            if job.job_id in self.state["confirmed_jobs"]:
                continue
            if kind == "technician" or job.part == resource_id:
                version = self.state["promises"][job.job_id] + 1
                self.state["promises"][job.job_id] = version
                self.state["promise_timeline"].append(
                    {"from_event": at, "job_id": job.job_id, "version": version})

    def _precheck(self, at: int, tool: str, arguments: dict):
        """Overridden by the guarded execution boundary; the lab world allows mistakes."""

    # -- host checks (always on) -------------------------------------------------------

    def _check_request(self, tool, arguments):
        if type(tool) is not str or tool not in TOOL_SCHEMAS:
            raise HostDenied("Unknown tool")
        schema = TOOL_SCHEMAS[tool]
        if type(arguments) is not dict or set(arguments) != set(schema):
            raise HostDenied("Arguments do not match tool schema")
        for name, (kind, (low, high)) in schema.items():
            value = arguments[name]
            if kind == "string":
                if type(value) is not str or not low <= len(value) <= high:
                    raise HostDenied(f"Invalid {name}")
            elif type(value) is not int or not low <= value <= high:
                raise HostDenied(f"Invalid {name}")
        if self.actor_id not in {self.fixture.operator, self.fixture.business_owner}:
            raise HostDenied("Actor does not belong to this shop")
        if tool not in READ_TOOLS and self.actor_id != self.fixture.operator:
            raise HostDenied("Only the shop's automation may act through these tools")
        # Reference checks precede the guard, so an unknown version is never "stale".
        job = None
        if "job_id" in arguments:
            job = self.fixture.job(arguments["job_id"])
            if job is None:
                raise HostDenied("Job is not served by this automation")
        if "resource_id" in arguments:
            resource = self.fixture.resource(arguments["resource_id"])
            if resource is None:
                raise HostDenied("Resource does not exist")
            if resource.kind == "part" and job.part != resource.resource_id:
                raise HostDenied("This job does not use that part")
        if "lease_id" in arguments:
            lease = self.state["leases"].get(arguments["lease_id"])
            if lease is None:
                raise HostDenied("Lease does not exist")
            if lease["holder"] != AUTOMATION:
                raise HostDenied("Lease belongs to another job")
        if "conversation_id" in arguments and arguments["conversation_id"] not in {
                j.conversation_id for j in self.fixture.jobs}:
            raise HostDenied("Conversation does not exist")
        if ("promise_version" in arguments
                and arguments["promise_version"] > self.state["promises"][job.job_id]):
            raise HostDenied("Promise version does not exist")

    # -- shared bookkeeping -------------------------------------------------------------

    def _held(self, resource_id=None, job_id=None) -> list[dict]:
        """Live leases still holding capacity, optionally narrowed."""
        return [lease for lease in self.state["leases"].values()
                if lease["status"] in {"active", "committed"}
                and resource_id in (None, lease["resource_id"])
                and job_id in (None, lease["job_id"])]

    def _free(self, resource_id) -> int:
        return self.fixture.resource(resource_id).capacity - len(self._held(resource_id))

    def _available_now(self) -> dict:
        return {r.resource_id: max(0, self._free(r.resource_id)) for r in self.fixture.resources}

    def _log_lease(self, at, kind, lease, source):
        # Identifiers may contain ":", so rows keep them as separate fields.
        self.state["lease_log"].append({
            "event_id": at, "kind": kind, "source": source, "lease_id": lease["lease_id"],
            "job_id": lease["job_id"], "resource_id": lease["resource_id"],
            "holder": lease["holder"], "expires_at": lease["expires_at"]})

    def _log_mutation(self, at, tool, conversation_id):
        self.state["mutation_log"].append({"event_id": at, "tool": tool,
                                           "conversation_id": conversation_id})

    def _grant(self, at, job_id, resource_id, holder, request_key, source) -> dict:
        lease_id = f"lease-{len(self.state['leases']) + 1}"
        expires_at = None if holder != AUTOMATION else at + self.fixture.lease_ttl_events
        lease = {"lease_id": lease_id, "job_id": job_id, "resource_id": resource_id,
                 "holder": holder, "status": "active", "granted_at": at,
                 "expires_at": expires_at, "request_key": request_key}
        self.state["leases"][lease_id] = lease
        self._log_lease(at, "granted", lease, source)
        return lease

    # -- tools (``at`` is the event index) -----------------------------------------------

    def _inspect_availability(self, at, job_id):
        job = self.fixture.job(job_id)
        cached = self.state["cached_availability"]
        available = cached["available"] if cached else self._available_now()
        return {
            "job_id": job_id, "conversation_id": job.conversation_id,
            "conversation_owner": self.state["conversations"][job.conversation_id],
            "promise_version": self.state["promises"][job_id],
            "part_needed": job.part,
            "confirmed": job_id in self.state["confirmed_jobs"],
            "availability_as_of": cached["as_of_event"] if cached else at,
            "resources": [{"resource_id": r.resource_id, "kind": r.kind, "capacity": r.capacity,
                           "available": available[r.resource_id]}
                          for r in self.fixture.resources],
            "leases": [{"lease_id": lease["lease_id"], "job_id": lease["job_id"],
                        "resource_id": lease["resource_id"], "status": lease["status"]}
                       for lease in self._held()],
        }

    def _reserve_resource(self, at, job_id, resource_id, request_key):
        known = self.state["request_keys"].get(request_key)
        if known is not None:
            lease = self.state["leases"][known]
            if (lease["job_id"], lease["resource_id"]) != (job_id, resource_id):
                raise ToolError("Request key was used for another reservation")
            return self._lease_result(lease, replayed=True)  # idempotent: nothing changes
        lease = self._grant(at, job_id, resource_id, AUTOMATION, request_key, "tool")
        self.state["request_keys"][request_key] = lease["lease_id"]
        self._log_mutation(at, "reserve_resource", self.fixture.job(job_id).conversation_id)
        self._maybe_schedule_takeover(at)
        lost = self.scenario.params("lost_reservation_ack")
        if lost and not self._ack_lost and (lost["after_event"] is None
                                            or at > lost["after_event"]):
            self._ack_lost = True
            raise ReservationAckLost("Reservation committed; acknowledgement lost")
        return self._lease_result(lease, replayed=False)

    @staticmethod
    def _lease_result(lease, *, replayed):
        return {"lease_id": lease["lease_id"], "job_id": lease["job_id"],
                "resource_id": lease["resource_id"], "status": lease["status"],
                "replayed": replayed}

    def _maybe_schedule_takeover(self, at):
        """Default trigger: the first job's leases are all in place, confirmation pending."""
        takeover = self.scenario.params("takeover_with_queued_confirmation")
        if not takeover or takeover["after_event"] is not None or self._takeover_scheduled:
            return
        job = self.fixture.jobs[0]
        held = {lease["resource_id"] for lease in self._held(job_id=job.job_id)}
        if (held & set(self.fixture.technicians)
                and (job.part is None or job.part in held)):
            self._takeover_scheduled = True
            self._takeover_from = at + 1

    def _release_resource(self, at, lease_id):
        lease = self.state["leases"][lease_id]
        if lease["status"] not in {"active", "committed"}:
            raise ToolError("Lease is not held")
        lease["status"] = "released"
        self._log_lease(at, "released", lease, "tool")
        self._log_mutation(at, "release_resource",
                           self.fixture.job(lease["job_id"]).conversation_id)
        return {"lease_id": lease_id, "status": "released"}

    def _confirm_service(self, at, job_id, promise_version):
        if job_id in self.state["confirmed_jobs"]:
            raise ToolError("Job is already confirmed")
        # Confirmation is irreversible exposure to the customer; it is never undone.
        self.state["confirmed_jobs"].append(job_id)
        for lease in self._held(job_id=job_id):
            if lease["status"] == "active":
                lease["status"] = "committed"
                self._log_lease(at, "committed", lease, "tool")
        conversation = self.fixture.job(job_id).conversation_id
        self.state["confirmation_log"].append({"event_id": at, "job_id": job_id,
                                               "conversation_id": conversation,
                                               "promise_version": promise_version})
        self._log_mutation(at, "confirm_service", conversation)
        return {"job_id": job_id, "promise_version": promise_version, "status": "confirmed"}

    def _hand_off_to_owner(self, at, conversation_id):
        if self.state["conversations"][conversation_id] != AUTOMATION:
            raise ToolError("Conversation is already with the owner")
        self.state["conversations"][conversation_id] = BUSINESS_OWNER
        self.state["ownership_timeline"].append(
            {"from_event": at + 1, "conversation_id": conversation_id,
             "owner": BUSINESS_OWNER, "source": "handoff"})
        self._log_mutation(at, "hand_off_to_owner", conversation_id)
        return {"conversation_id": conversation_id, "owner": BUSINESS_OWNER}
