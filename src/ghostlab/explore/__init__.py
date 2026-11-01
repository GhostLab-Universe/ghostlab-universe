"""Bounded exploration harness (P4): fixed, seeded-random and scripted-guided arms.

Drives worlds only through the frozen P1 extension interfaces. Standard
library only: no inference, no network, no services.
"""

from .harness import (FIXED_ARM, GUIDED_ARM, GUIDED_MODE, RANDOM_ARM, AttemptSpec,
                      CampaignReport, SamplingTarget, ScriptedTarget, refund_action_sampler,
                      refund_scenarios, run_campaign, run_scripted_guided)

__all__ = [
    "FIXED_ARM", "GUIDED_ARM", "GUIDED_MODE", "RANDOM_ARM", "AttemptSpec",
    "CampaignReport", "SamplingTarget", "ScriptedTarget", "refund_action_sampler",
    "refund_scenarios", "run_campaign", "run_scripted_guided",
]
