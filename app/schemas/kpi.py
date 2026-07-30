from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from app.services.kpi_service import validate_kpi_config

LINKABLE_TYPES = {"objective", "rock", "task", "kpi"}
GROUP_FORMULAS = {"sum", "average"}


# ─── KPI Groups ───────────────────────────────────────────────────────────────

class KPIGroupCreate(BaseModel):
    name: str
    formula: str = "sum"
    collapse_by_default: bool = False

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Group name cannot be empty.")
        return v

    @field_validator("formula")
    @classmethod
    def _formula_valid(cls, v: str) -> str:
        if v not in GROUP_FORMULAS:
            raise ValueError(f"formula must be one of: {', '.join(sorted(GROUP_FORMULAS))}")
        return v


class KPIGroupUpdate(BaseModel):
    name: Optional[str] = None
    formula: Optional[str] = None
    collapse_by_default: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Group name cannot be empty.")
        return v

    @field_validator("formula")
    @classmethod
    def _formula_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in GROUP_FORMULAS:
            raise ValueError(f"formula must be one of: {', '.join(sorted(GROUP_FORMULAS))}")
        return v


class KPIGroupOut(BaseModel):
    id: int
    name: str
    team_id: int
    formula: str
    collapse_by_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EntityLinkIn(BaseModel):
    linked_type: str
    linked_id: int
    title: str

    @field_validator("linked_type")
    @classmethod
    def validate_linked_type(cls, v: str) -> str:
        if v not in LINKABLE_TYPES:
            raise ValueError(f"linked_type must be one of: {', '.join(sorted(LINKABLE_TYPES))}")
        return v


class EntityLinkOut(BaseModel):
    linked_type: str
    linked_id: int
    title: str
    model_config = {"from_attributes": True}


class KPIEntryUpsert(BaseModel):
    value: Optional[float] = None
    forecast: Optional[float] = None
    period_start: date
    period_type: str


class KPIEntryAddNote(BaseModel):
    text: str


class KPINoteUpdate(BaseModel):
    text: str


class KPIReorderItem(BaseModel):
    id: int
    sort_order: int


class KPIEntryOut(BaseModel):
    id: int
    kpi_id: int
    value: Optional[float] = None
    forecast: Optional[float] = None
    notes: Optional[list] = []
    period_start: date
    period_type: str
    created_at: datetime

    model_config = {"from_attributes": True}


class KPIDerivedEntry(BaseModel):
    """A value computed by interpolation — never stored, never editable."""
    period_start: date
    period_type: str
    value: float
    interpolated: bool = True


class OwnerRef(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str

    model_config = {"from_attributes": True}


class RockRef(BaseModel):
    id: int
    title: str
    status: Optional[str] = None

    model_config = {"from_attributes": True}


class ProjectRef(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class KPICreate(BaseModel):
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    owner_id: Optional[int] = None
    rock_id: int
    project_id: Optional[int] = None
    kpi_group: Optional[str] = None
    kpi_group_id: Optional[int] = None
    supported_views: Optional[list] = ["weekly", "monthly", "quarterly", "yearly"]
    interpolation: Optional[str] = "no_interpolation"
    target_type: Optional[str] = "number"
    formula: Optional[str] = None
    reference_value: Optional[float] = None
    reference_max: Optional[float] = None
    is_snoozed: bool = False
    snoozed_until: Optional[date] = None
    links: list[EntityLinkIn] = []

    @model_validator(mode="after")
    def _validate_config(self):
        validate_kpi_config(
            interpolation=self.interpolation,
            target_type=self.target_type,
            formula=self.formula,
            reference_value=self.reference_value,
            reference_max=self.reference_max,
            supported_views=self.supported_views,
        )
        return self


class KPIUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    owner_id: Optional[int] = None
    rock_id: Optional[int] = None
    project_id: Optional[int] = None
    kpi_group: Optional[str] = None
    kpi_group_id: Optional[int] = None
    supported_views: Optional[list] = None
    interpolation: Optional[str] = None
    target_type: Optional[str] = None
    formula: Optional[str] = None
    reference_value: Optional[float] = None
    reference_max: Optional[float] = None
    is_snoozed: Optional[bool] = None
    snoozed_until: Optional[date] = None
    team_id: Optional[int] = None
    links: Optional[list[EntityLinkIn]] = None


class KPIOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    team_id: int
    owner_id: Optional[int] = None
    created_by_id: Optional[int] = None
    rock_id: Optional[int] = None
    project_id: Optional[int] = None
    kpi_group: Optional[str] = None
    kpi_group_id: Optional[int] = None
    supported_views: Optional[list] = None
    interpolation: str
    target_type: str
    formula: Optional[str] = None
    reference_value: Optional[float] = None
    reference_max: Optional[float] = None
    is_snoozed: bool = False
    snoozed_until: Optional[date] = None
    owner: Optional[OwnerRef] = None
    rock: Optional[RockRef] = None
    project: Optional[ProjectRef] = None
    entries: list[KPIEntryOut] = []
    links: list[EntityLinkOut] = []
    # Computed server-side (source of truth) — see app/services/kpi_service.py
    statuses: dict[str, str] = {}
    derived_entries: list[KPIDerivedEntry] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
