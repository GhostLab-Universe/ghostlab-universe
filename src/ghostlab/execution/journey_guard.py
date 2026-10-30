"""Guarded execution boundary for the journey world: world-rule enforcement.

This is not M06 authorization (identity and scope) and it does not replace the
verifier. It denies, at the moment of the call and from authoritative world
state, the two business mistakes the lab world deliberately allows:

- a commitment (hold, reservation or calendar update) stated under an intent
  version that is not the currently approved one;
- delivery of a message whose recipient's sharing permission has been withdrawn.

It runs after the world's host checks (schema, principal, references), so it
only ever sees well-formed calls. A denied call is recorded as an error event
and changes nothing: no hold, no commitment, no delivery. This module shares no code with the verifier
(``ghostlab.worlds.journey_verify``), so a bug cannot hide in both.
"""

from ghostlab.worlds.journey import JourneyWorld, ToolError

COMMITTING = frozenset({"hold_availability", "confirm_reservation", "update_calendar_event"})


class StaleIntentDenied(ToolError):
    pass


class PermissionWithdrawnDenied(ToolError):
    pass


class GuardedJourneyWorld(JourneyWorld):
    def _precheck(self, at, tool, arguments):
        if tool in COMMITTING:
            current = self.state["current_intent_version"]  # read now, not from the adapter
            if arguments["intent_version"] != current:
                raise StaleIntentDenied(
                    f"intent version {arguments['intent_version']} is not the approved "
                    f"version {current}")
        if tool == "deliver_message":  # host checks already proved the message exists
            message = self.state["messages"][arguments["message_id"]]
            if not self.state["sharing"][message["recipient_id"]]:
                raise PermissionWithdrawnDenied("recipient's sharing permission was withdrawn")


def make_world(fixture, scenario, *, actor_id=None) -> JourneyWorld:
    """The world class is chosen by the fixture's ``guarded`` flag (recorded in capsules)."""
    cls = GuardedJourneyWorld if fixture.guarded else JourneyWorld
    return cls(fixture, scenario, actor_id=actor_id)
