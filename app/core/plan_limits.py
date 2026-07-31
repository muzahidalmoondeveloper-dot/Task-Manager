import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanLimits:
    max_members: int           # -1 = unlimited
    max_teams: int
    max_projects: int
    max_tasks_per_month: int
    has_ai_features: bool
    has_integrations: bool
    has_api_access: bool
    storage_gb: int            # -1 = unlimited

    def is_unlimited(self, field: str) -> bool:
        return getattr(self, field, -1) == -1

    def within_limit(self, field: str, current_count: int) -> bool:
        limit = getattr(self, field, -1)
        return limit == -1 or current_count < limit


PLAN_LIMITS: dict[str, PlanLimits] = {
    "starter": PlanLimits(
        max_members=10,
        max_teams=3,
        max_projects=-1,
        max_tasks_per_month=-1,
        has_ai_features=True,
        has_integrations=True,
        has_api_access=True,
        storage_gb=10,
    ),
    "business": PlanLimits(
        max_members=20,
        max_teams=10,
        max_projects=-1,
        max_tasks_per_month=-1,
        has_ai_features=True,
        has_integrations=True,
        has_api_access=True,
        storage_gb=50,
    ),
}

PLAN_DISPLAY_NAMES: dict[str, str] = {
    "starter": "Starter",
    "business": "Business",
}

ALL_PLANS = list(PLAN_LIMITS.keys())


def get_plan_limits(plan: str, extra_teams: int = 0, extra_users: int = 0) -> PlanLimits:
    """Effective limits for a plan, including any purchased add-on quantities.
    Unlimited (-1) fields stay unlimited regardless of add-ons."""
    base = PLAN_LIMITS.get(plan, PLAN_LIMITS["starter"])
    return dataclasses.replace(
        base,
        max_teams=base.max_teams if base.max_teams == -1 else base.max_teams + extra_teams,
        max_members=base.max_members if base.max_members == -1 else base.max_members + extra_users,
    )
