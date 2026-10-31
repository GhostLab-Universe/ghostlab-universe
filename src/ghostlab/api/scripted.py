"""Scripted local inference: checked-in proposals, in process. Not a model.

An explicit opt-in (``python -m ghostlab.api --scripted``) so the real server can run a
whole flow with no key and no network. Every Explorer reply below is a recorded,
checked-in proposal served by an ``httpx.MockTransport``, which is the only transport the
live-inference gate treats as in-process, so that gate is unchanged. The endpoint is under
the reserved ``.invalid`` domain and is pinned into each campaign at start, which is how a
campaign stays labelled ``scripted`` for as long as it exists.

A client may name a scenario from ``ScriptedScenario`` when it creates a campaign. The
choice picks a checked-in proposal and carries no content. It applies to that campaign's
starting run: the scenario isn't stored, so a campaign recovered from a crash mid-proposal
gets the world's default proposal, still labelled scripted.

Worlds, verifier, approvals, stores and capsules are the real code. Only the proposal is
scripted, and nothing here shows that a model would propose it.
"""

import json
from typing import Literal

import httpx

from ghostlab.orchestration.inference import LiveInferenceRefused
from ghostlab.providers import Credential, ProviderConfig, Runtime, adapter_for

SCRIPTED_ENDPOINT = "https://scripted.ghostlab.invalid/v1"
SCRIPTED_MODEL = "ghostlab-scripted-proposals"
JOURNEY_MARKER = b"group-journey"

REFUND_WORLD, JOURNEY_WORLD = "refund-world/1", "group-journey/1"
REVOCATION_RECIPIENT = "participant-c"
RACE_AFTER_EVENT = 17  # journey-version-checked's last permission check before delivery
NEVER = 999  # past the end of any run: the fault is scheduled and never fires
SECOND_ATTEMPT_MARKER = b"Already explored"

# A closed list. A client names one of these; it never supplies proposal content.
ScriptedScenario = Literal["default", "revocation_race", "fault_never_fires"]
DEFAULT_SCENARIO = "default"


def _refund(hypothesis, *, timeout, cap=20, signal="total_refund_within_payment fails"):
    return {"schema_version": 1, "hypothesis": hypothesis, "adapter": "vulnerable",
            "scenario": {"timeout_after_commit": timeout, "max_tool_calls": cap},
            "expected_signal": signal}


def _journey(hypothesis, *, adapter, after_event=None, cap=20,
             signal="delivered_messages_permitted fails"):
    return {"schema_version": 1, "world_version": JOURNEY_WORLD, "hypothesis": hypothesis,
            "adapter_id": adapter,
            "scenario": {"schema_version": 1, "world_version": JOURNEY_WORLD,
                         "max_tool_calls": cap,
                         "faults": [{"fault_id": "sharing_revoked_while_queued",
                                     "after_event": after_event,
                                     "parameters": [{"name": "recipient_id",
                                                     "value": REVOCATION_RECIPIENT}]}]},
            "expected_signal": signal}


def _proposals(cap: int) -> dict:
    return {
        (REFUND_WORLD, "default"): _refund(
            "A retry after a post-commit timeout issues a second refund", timeout=True),
        (JOURNEY_WORLD, "default"): _journey(
            "A message queued before revocation is still delivered afterwards",
            adapter="journey-naive"),
        # The version-checked adapter leaks in lab mode: sharing ends after its last check.
        (JOURNEY_WORLD, "revocation_race"): _journey(
            "Sharing withdrawn after the last permission check is still delivered",
            adapter="journey-version-checked", after_event=RACE_AFTER_EVENT),
        (REFUND_WORLD, "fault_never_fires"): _refund(
            "A run with no timeout scheduled shows nothing about retries", timeout=False, cap=cap,
            signal="no fault fires, so a clean result proves nothing"),
        (JOURNEY_WORLD, "fault_never_fires"): _journey(
            "A revocation timed after the run ends never reaches a delivery",
            adapter="journey-version-checked", after_event=NEVER, cap=cap,
            signal="no fault fires, so a clean result proves nothing"),
    }


def available(world_version: str, scenario: str) -> bool:
    return (world_version, scenario) in _proposals(20)


def scripted_reply(scenario: str = DEFAULT_SCENARIO):
    """A transport handler serving one scenario's checked-in proposals."""
    def reply(request: httpx.Request) -> httpx.Response:
        world = JOURNEY_WORLD if JOURNEY_MARKER in request.content else REFUND_WORLD
        cap = 19 if SECOND_ATTEMPT_MARKER in request.content else 20  # a second, new experiment
        proposal = _proposals(cap)[world, scenario]
        return httpx.Response(200, json={
            "id": "resp_scripted", "object": "response", "status": "completed",
            "model": SCRIPTED_MODEL,
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(proposal),
                                     "annotations": []}]}],
            "usage": {"input_tokens": 0, "output_tokens": 0}})

    return reply


def scripted_runtime(scenario: str = DEFAULT_SCENARIO) -> Runtime:
    return Runtime(transport=httpx.MockTransport(scripted_reply(scenario)))


def scripted_adapter(runtime: Runtime):
    if not isinstance(runtime.transport, httpx.MockTransport):
        raise LiveInferenceRefused("scripted inference runs in process only")
    return adapter_for(ProviderConfig(provider="meta_responses", model=SCRIPTED_MODEL,
                                      credential=Credential("scripted-no-key", "scripted"),
                                      base_url=SCRIPTED_ENDPOINT), runtime)
