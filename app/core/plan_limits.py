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
    "free": PlanLimits(
        max_members=5,
        max_teams=3,
        max_projects=5,
        max_tasks_per_month=50,
        has_ai_features=False,
        has_integrations=False,
        has_api_access=False,
        storage_gb=1,
    ),
    "starter": PlanLimits(
        max_members=20,
        max_teams=10,
        max_projects=-1,
        max_tasks_per_month=-1,
        has_ai_features=True,
        has_integrations=False,
        has_api_access=False,
        storage_gb=10,
    ),
    "professional": PlanLimits(
        max_members=100,
        max_teams=-1,
        max_projects=-1,
        max_tasks_per_month=-1,
        has_ai_features=True,
        has_integrations=True,
        has_api_access=True,
        storage_gb=100,
    ),
    "enterprise": PlanLimits(
        max_members=-1,
        max_teams=-1,
        max_projects=-1,
        max_tasks_per_month=-1,
        has_ai_features=True,
        has_integrations=True,
        has_api_access=True,
        storage_gb=-1,
    ),
}

PLAN_DISPLAY_NAMES: dict[str, str] = {
    "free": "Free",
    "starter": "Starter",
    "professional": "Professional",
    "enterprise": "Enterprise",
}

ALL_PLANS = list(PLAN_LIMITS.keys())


def get_plan_limits(plan: str) -> PlanLimits:
    # Subscription checks temporarily disabled — always return enterprise limits
    return PLAN_LIMITS["enterprise"]
