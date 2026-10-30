"""Guarded execution boundary for the relay world: world-rule enforcement.

This is not M06 authorization (identity and scope) and it does not replace the
verifier. It denies, at the moment of the call and from authoritative live state,
the business mistakes the lab world deliberately allows:

- any automated change to a conversation the business owner now holds;
- a lease past a resource's capacity, or a second lease for the same job and
  resource;
- a confirmation under a promise version that is no longer current, or without
  live leases for everything the job needs;
- releasing a lease that backs a confirmed service.

It runs inside the tool call, after the world applies what is due at this event
and after the host checks, so it sees a takeover, a competing lease or a lease
expiry that lands on this very event, and only ever sees well-formed calls. A
pre-call check could not close that window. A denied call is recorded as an error
event and changes nothing. This module shares no code with the verifier
(``ghostlab.worlds.relay_verify``), so a bug cannot hide in both.
"""

from ghostlab.worlds.relay import AUTOMATION, RelayWorld, ToolError

GUARDED_MUTATIONS = frozenset({"reserve_resource", "release_resource", "confirm_service"})
DENIALS = frozenset({"OwnerTakeoverDenied", "CapacityDenied", "DuplicateLeaseDenied",
                     "StalePromiseDenied", "UnbackedConfirmationDenied", "ConfirmedLeaseDenied"})


class OwnerTakeoverDenied(ToolError):
    pass


class CapacityDenied(ToolError):
    pass


class DuplicateLeaseDenied(ToolError):
    pass


class StalePromiseDenied(ToolError):
    pass


class UnbackedConfirmationDenied(ToolError):
    pass


class ConfirmedLeaseDenied(ToolError):
    pass


class GuardedRelayWorld(RelayWorld):
    def _precheck(self, at, tool, arguments):
        if tool not in GUARDED_MUTATIONS:
            return
        leases = self.state["leases"]
        job_id = (leases[arguments["lease_id"]]["job_id"] if tool == "release_resource"
                  else arguments["job_id"])
        conversation = self.fixture.job(job_id).conversation_id
        if self.state["conversations"][conversation] != AUTOMATION:  # read now, not earlier
            raise OwnerTakeoverDenied("the business owner holds this conversation")
        if tool == "reserve_resource":
            self._check_reservation(job_id, arguments)
        elif tool == "confirm_service":
            self._check_confirmation(job_id, arguments)
        elif leases[arguments["lease_id"]]["status"] == "committed":
            raise ConfirmedLeaseDenied("this lease backs a confirmed service")

    def _check_reservation(self, job_id, arguments):
        if arguments["request_key"] in self.state["request_keys"]:
            return  # a retry of a recorded request grants nothing new
        resource_id = arguments["resource_id"]
        holders = self._held(resource_id)
        if any(lease["job_id"] == job_id for lease in holders):
            raise DuplicateLeaseDenied("this job already holds that resource")
        if len(holders) >= self.fixture.resource(resource_id).capacity:
            raise CapacityDenied(f"{resource_id} is at capacity")

    def _check_confirmation(self, job_id, arguments):
        current = self.state["promises"][job_id]
        if arguments["promise_version"] != current:
            raise StalePromiseDenied(f"promise version {arguments['promise_version']} is not "
                                     f"the current version {current}")
        live = {lease["resource_id"] for lease in self._held(job_id=job_id)}
        part = self.fixture.job(job_id).part
        if not live & set(self.fixture.technicians) or (part is not None and part not in live):
            raise UnbackedConfirmationDenied("the job does not hold everything it needs")


def make_world(fixture, scenario, *, actor_id=None) -> RelayWorld:
    """The world class is chosen by the fixture's ``guarded`` flag (recorded in capsules)."""
    cls = GuardedRelayWorld if fixture.guarded else RelayWorld
    return cls(fixture, scenario, actor_id=actor_id)
