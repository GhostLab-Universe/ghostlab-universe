"""An explicit, expiring operator decision; it never enables live calls or deployment."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Callable


class RetentionRefused(ValueError):
    """Typed policy refusal, never a content-classification guarantee."""



@dataclass(frozen=True)
class RetentionDecision:
    provider: str
    endpoint: str
    model: str
    reviewed_at: datetime
    reviewer_id: str
    provider_policy_url: str
    account_controls_verified: bool = False
    synthetic_only: bool = True
    continuation_policy: str = "unreviewed"

    def require(self, *, provider: str, endpoint: str, model: str,
                data_classification: str, now: datetime) -> None:
        if (self.provider, self.endpoint, self.model) != (provider, endpoint, model):
            raise RetentionRefused("retention decision does not match this provider configuration")
        if self.reviewed_at.tzinfo is None or now.tzinfo is None:
            raise RetentionRefused("retention timestamps must have timezones")
        age = now - self.reviewed_at
        if not timedelta(0) <= age <= timedelta(days=30):
            raise RetentionRefused("retention decision is stale or from the future")
        if not self.reviewer_id or not self.provider_policy_url.startswith("https://"):
            raise RetentionRefused("retention review evidence is missing")
        if self.account_controls_verified is not True:
            raise RetentionRefused("provider account controls have not been verified")
        if provider == "meta_responses" and self.continuation_policy not in {
                "stateful-provider-storage-reviewed", "stateless-provider-storage-reviewed"}:
            raise RetentionRefused("Meta continuation and storage choice is not recorded")
        if self.synthetic_only is not True or data_classification != "synthetic":
            raise RetentionRefused("operator classification is not permitted by this metadata decision")


@dataclass(frozen=True)
class RetentionCheckedFactory:
    factory: Callable
    decision: RetentionDecision
    data_classification: str
    clock: Callable

    def __call__(self, runtime):
        adapter = self.factory(runtime)
        config = adapter.config
        self.decision.require(provider=config.provider, endpoint=config.base_url,
                              model=config.model, data_classification=self.data_classification,
                              now=self.clock())
        return LiveContentDisabledAdapter(adapter)


@dataclass(frozen=True)
class LiveContentDisabledAdapter:
    """Metadata can be inspected, but this draft cannot release live request content."""
    adapter: object

    @property
    def config(self):
        return self.adapter.config

    def complete(self, *args, **kwargs):
        raise RetentionRefused("Live content boundary is not qualified; inference is disabled")


@dataclass(frozen=True)
class LiveContentDisabledFactory:
    factory: Callable

    def __call__(self, runtime):
        return LiveContentDisabledAdapter(self.factory(runtime))
