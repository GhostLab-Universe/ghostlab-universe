"""Per-role budget ceilings for ``policy-1``. M06 supplies ceilings; M05 accounts.

These are conservative engineering defaults, not measured costs. Cost ceilings
stay unset (None) because no provider price is verified; M05 must still record
unknown cost as unknown rather than zero.
"""

from collections.abc import Mapping
from types import MappingProxyType

from ghostlab.contracts import BudgetLimits

from .authorization import POLICY_VERSION, Decision, allow, deny


def _limits(model_calls, tool_calls, input_tokens, output_tokens, wall_ms, parallel=1):
    return BudgetLimits.model_validate({
        "max_model_calls": model_calls, "max_tool_calls": tool_calls,
        "max_input_tokens": input_tokens, "max_output_tokens": output_tokens,
        "max_cost_minor": None, "cost_currency": None,
        "max_wall_clock_ms": wall_ms, "max_parallel_runs": parallel,
    })


# Campaign-level ceiling for the supervisor; per-task ceilings for workers.
ROLE_BUDGET_CEILINGS: Mapping[str, BudgetLimits] = MappingProxyType({
    "supervisor": _limits(50, 200, 500_000, 50_000, 30 * 60_000, parallel=2),
    "worldsmith": _limits(3, 0, 50_000, 8_000, 2 * 60_000),
    "explorer": _limits(5, 0, 50_000, 5_000, 2 * 60_000),
    "analyst": _limits(3, 0, 50_000, 8_000, 2 * 60_000),
    "target": _limits(10, 20, 50_000, 5_000, 2 * 60_000),
    "verifier": _limits(0, 0, 0, 0, 60_000),
})

_FIELDS = ("max_model_calls", "max_tool_calls", "max_input_tokens", "max_output_tokens",
           "max_wall_clock_ms", "max_parallel_runs")


def ceiling_for(role: str) -> BudgetLimits:
    return ROLE_BUDGET_CEILINGS[role]


def check_within_ceiling(role: str, requested: BudgetLimits) -> Decision:
    """A campaign or task budget may not exceed its role ceiling in any dimension."""
    ceiling = ROLE_BUDGET_CEILINGS.get(role)
    if ceiling is None:
        return deny("capability_denied", f"no budget ceiling for role {role!r}")
    over = [name for name in _FIELDS if getattr(requested, name) > getattr(ceiling, name)]
    if requested.max_cost_minor is not None and ceiling.max_cost_minor is not None and (
            requested.cost_currency != ceiling.cost_currency
            or requested.max_cost_minor > ceiling.max_cost_minor):
        over.append("max_cost_minor")
    if over:
        return deny("capability_denied", f"exceeds {POLICY_VERSION} ceiling: {', '.join(over)}")
    return allow()
