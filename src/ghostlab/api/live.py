"""Explicit live inference for the local server: the qualified Meta path only.

``live_config`` is the ``--live`` counterpart to ``local_config``: real stores
and the local-dev session, plus a network runtime, the Meta Responses adapter
and a ``LiveInferenceAuthorization``. It refuses unless every gate passes:

- the model is exactly the qualified one (``muse-spark-1.3``); Baseten and the
  hackathon ``-1026`` id are refused here because no passing report qualifies
  them, and invented ids are never accepted;
- the key comes from ``MODEL_API_KEY`` in the environment;
- ``max_calls`` is within the supervisor ceiling (the controller re-checks it
  against each campaign's own budget, and every HTTP attempt -- retries
  included -- reserves budget through the attempt gate);
- a live M01 compatibility report passes for this provider, endpoint, model
  and schema, checked now so a bad report fails startup, not a campaign;
- paid calls are confirmed and server-side retention is explicitly allowed or
  refused (refused by default, sent as Meta ``store: false``).

The report only covers ``experiment_proposal`` (the refund world), so a live
journey campaign is refused by the per-campaign gate until its own schema has
proof. ``--live`` and ``--scripted`` are mutually exclusive. Streaming stays
off: ``ModelRequest.stream`` is fixed to false and the adapters refuse it.
"""

import dataclasses
import os
from pathlib import Path

from ghostlab.orchestration.inference import LiveInferenceAuthorization, LiveInferenceRefused
from ghostlab.policy import ceiling_for
from ghostlab.providers import Credential, OutboundPolicy, ProviderConfig, Runtime, adapter_for
from ghostlab.providers.base import META_DEFAULT_BASE_URL

from .local import local_config
from .plane import ApiConfig
from .sessions import LocalDevSessions

QUALIFIED_LIVE_PROVIDER = "meta_responses"
QUALIFIED_LIVE_MODEL = "muse-spark-1.3"
QUALIFIED_LIVE_KEY_VAR = "MODEL_API_KEY"
QUALIFIED_LIVE_BASE_URL_VAR = "GHOSTLAB_META_BASE_URL"


def live_config(root: Path, sessions: LocalDevSessions, *, bind_host: str, model: str,
                max_calls: int, compat_report: str | Path, confirm_paid_calls: bool,
                allow_server_retention: bool, environ: dict | None = None) -> ApiConfig:
    """The trusted host's live wiring. Every refusal names the gate that failed."""
    if model != QUALIFIED_LIVE_MODEL:
        raise LiveInferenceRefused(
            f"live inference is qualified only for {QUALIFIED_LIVE_MODEL}")
    if not confirm_paid_calls:
        raise LiveInferenceRefused("paid calls were not confirmed")
    ceiling = ceiling_for("supervisor")
    if type(max_calls) is not int or not 1 <= max_calls <= ceiling.max_model_calls:
        raise LiveInferenceRefused(
            f"max_calls must be an integer from 1 to {ceiling.max_model_calls}")
    environ = os.environ if environ is None else environ
    config = ProviderConfig(
        provider=QUALIFIED_LIVE_PROVIDER, model=model,
        credential=Credential(environ.get(QUALIFIED_LIVE_KEY_VAR, ""),
                              f"env:{QUALIFIED_LIVE_KEY_VAR}"),
        base_url=environ.get(QUALIFIED_LIVE_BASE_URL_VAR, "").strip() or META_DEFAULT_BASE_URL)
    live = LiveInferenceAuthorization(
        model=model, max_model_calls=max_calls,
        compatibility_report=Path(compat_report), confirm_paid_calls=True)
    # Fail fast at startup; the controller re-runs this gate per campaign with
    # the campaign's own limits and the world's schema.
    live.check(config, ceiling)
    policy = OutboundPolicy(allow_server_retention=allow_server_retention)
    base = local_config(root, sessions, bind_host=bind_host, environ=environ)
    services = dataclasses.replace(
        base.services,
        adapter_factory=lambda runtime: adapter_for(config, runtime),
        runtime=Runtime(outbound=policy), live=live)
    return dataclasses.replace(base, services=services)
