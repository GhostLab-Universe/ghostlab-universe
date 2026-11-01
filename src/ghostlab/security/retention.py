"""An explicit, expiring operator decision; it never enables live calls or deployment."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Callable


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

    def require(self, *, provider: str, endpoint: str, model: str,
                data_classification: str, now: datetime) -> None:
        if (self.provider, self.endpoint, self.model) != (provider, endpoint, model):
            raise ValueError("retention decision does not match this provider configuration")
        if self.reviewed_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("retention timestamps must have timezones")
        age = now - self.reviewed_at
        if not timedelta(0) <= age <= timedelta(days=30):
            raise ValueError("retention decision is stale or from the future")
        if not self.reviewer_id or not self.provider_policy_url.startswith("https://"):
            raise ValueError("retention review evidence is missing")
        if self.account_controls_verified is not True:
            raise ValueError("provider account controls have not been verified")
        if self.synthetic_only is not True or data_classification != "synthetic":
            raise ValueError("customer, personal and unclassified content remain disabled")


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
        return adapter
