from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, field_validator

RISK_SEVERITIES = {"low", "medium", "high", "critical"}
RISK_LIKELIHOODS = {"low", "medium", "high"}
RISK_STATUSES = {"open", "mitigating", "closed"}


class OwnerRef(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str

    model_config = {"from_attributes": True}


class RiskProjectRef(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class RiskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    owner_id: Optional[int] = None
    project_id: Optional[int] = None
    severity: Optional[str] = "medium"
    likelihood: Optional[str] = "medium"
    status: Optional[str] = "open"
    mitigation_plan: Optional[str] = None
    identified_date: Optional[date] = None

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in RISK_SEVERITIES:
            raise ValueError(f"severity must be one of: {', '.join(sorted(RISK_SEVERITIES))}")
        return value

    @field_validator("likelihood")
    @classmethod
    def validate_likelihood(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in RISK_LIKELIHOODS:
            raise ValueError(f"likelihood must be one of: {', '.join(sorted(RISK_LIKELIHOODS))}")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in RISK_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(RISK_STATUSES))}")
        return value


class RiskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    owner_id: Optional[int] = None
    project_id: Optional[int] = None
    team_id: Optional[int] = None
    severity: Optional[str] = None
    likelihood: Optional[str] = None
    status: Optional[str] = None
    mitigation_plan: Optional[str] = None
    identified_date: Optional[date] = None

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in RISK_SEVERITIES:
            raise ValueError(f"severity must be one of: {', '.join(sorted(RISK_SEVERITIES))}")
        return value

    @field_validator("likelihood")
    @classmethod
    def validate_likelihood(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in RISK_LIKELIHOODS:
            raise ValueError(f"likelihood must be one of: {', '.join(sorted(RISK_LIKELIHOODS))}")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in RISK_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(RISK_STATUSES))}")
        return value


class RiskOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    team_id: int
    owner_id: Optional[int] = None
    project_id: Optional[int] = None
    severity: str
    likelihood: str
    status: str
    mitigation_plan: Optional[str] = None
    identified_date: Optional[date] = None
    owner: Optional[OwnerRef] = None
    project: Optional[RiskProjectRef] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
