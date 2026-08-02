from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.core.onboarding_constants import (
    CHANGE_REQUEST_STATUSES,
    DOCUMENT_STATUSES,
    FORM_FIELD_TYPES,
    ONBOARDING_STATUSES,
    STEP_STATUSES,
    STEP_TYPES,
)


class UserRef(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str

    model_config = {"from_attributes": True}


class ProjectRef(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


# ── Form fields (template + client-copied) ──────────────────────────────────

class TemplateFormFieldCreate(BaseModel):
    label: str = Field(min_length=1, max_length=500)
    field_key: str = Field(min_length=1, max_length=100)
    field_type: str = "short_text"
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    is_required: bool = False
    display_order: int = 0
    options_json: Optional[list[str]] = None

    @field_validator("field_type")
    @classmethod
    def validate_field_type(cls, v: str) -> str:
        if v not in FORM_FIELD_TYPES:
            raise ValueError(f"field_type must be one of: {', '.join(sorted(FORM_FIELD_TYPES))}")
        return v


class TemplateFormFieldRead(BaseModel):
    id: int
    label: str
    field_key: str
    field_type: str
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    is_required: bool
    display_order: int
    options_json: Optional[list[str]] = None

    model_config = {"from_attributes": True}


class FormResponseRead(BaseModel):
    response_text: Optional[str] = None
    response_json: Optional[list[str]] = None
    is_draft: bool
    version: int
    submitted_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ClientFormFieldRead(BaseModel):
    id: int
    label: str
    field_key: str
    field_type: str
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    is_required: bool
    display_order: int
    options_json: Optional[list[str]] = None
    response: Optional[FormResponseRead] = None

    model_config = {"from_attributes": True}


class FormResponseItem(BaseModel):
    field_id: int
    response_text: Optional[str] = None
    response_json: Optional[list[str]] = None


class FormResponsesSaveRequest(BaseModel):
    responses: list[FormResponseItem] = []
    submit: bool = False


# ── Document requirements + documents ───────────────────────────────────────

class TemplateDocumentRequirementCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    is_required: bool = True
    display_order: int = 0
    allowed_file_types: Optional[str] = None
    max_file_size_mb: int = Field(default=10, ge=1, le=100)


class TemplateDocumentRequirementRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_required: bool
    display_order: int
    allowed_file_types: Optional[str] = None
    max_file_size_mb: int

    model_config = {"from_attributes": True}


class DocumentRead(BaseModel):
    id: int
    requirement_id: Optional[int] = None
    file_name: str
    file_url: str
    file_type: Optional[str] = None
    file_size: int
    version: int
    status: str
    review_comment: Optional[str] = None
    uploaded_by: Optional[UserRef] = None
    uploaded_at: datetime
    reviewed_by: Optional[UserRef] = None
    reviewed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ClientDocumentRequirementRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_required: bool
    display_order: int
    allowed_file_types: Optional[str] = None
    max_file_size_mb: int
    documents: list[DocumentRead] = []

    model_config = {"from_attributes": True}


class DocumentReviewRequest(BaseModel):
    status: str
    review_comment: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in {"approved", "rejected"}:
            raise ValueError("status must be 'approved' or 'rejected'")
        return v


# ── Change requests ──────────────────────────────────────────────────────────

class ChangeRequestCreate(BaseModel):
    reason: str = Field(min_length=1)


class ChangeRequestRead(BaseModel):
    id: int
    reason: str
    status: str
    requested_by: Optional[UserRef] = None
    client_response: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in CHANGE_REQUEST_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(CHANGE_REQUEST_STATUSES))}")
        return v


# ── Template steps ──────────────────────────────────────────────────────────

class OnboardingTemplateStepCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = None
    step_type: str = "manual_task"
    is_required: bool = True
    display_order: int = 0
    requires_approval: bool = False
    due_days_after_start: Optional[int] = Field(default=None, ge=0)
    depends_on_step_id: Optional[int] = None
    form_fields: list[TemplateFormFieldCreate] = []
    document_requirements: list[TemplateDocumentRequirementCreate] = []

    @field_validator("step_type")
    @classmethod
    def validate_step_type(cls, v: str) -> str:
        if v not in STEP_TYPES:
            raise ValueError(f"step_type must be one of: {', '.join(sorted(STEP_TYPES))}")
        return v


class OnboardingTemplateStepRead(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    step_type: str
    is_required: bool
    display_order: int
    requires_approval: bool
    due_days_after_start: Optional[int] = None
    depends_on_step_id: Optional[int] = None
    form_fields: list[TemplateFormFieldRead] = []
    document_requirements: list[TemplateDocumentRequirementRead] = []

    model_config = {"from_attributes": True}


# ── Templates ────────────────────────────────────────────────────────────────

class OnboardingTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    is_default: bool = False
    steps: list[OnboardingTemplateStepCreate] = []


class OnboardingTemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None
    steps: Optional[list[OnboardingTemplateStepCreate]] = None


class OnboardingTemplateRead(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_default: bool
    is_active: bool
    created_by: Optional[UserRef] = None
    steps: list[OnboardingTemplateStepRead] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Client onboarding steps ─────────────────────────────────────────────────

class ClientOnboardingStepUpdate(BaseModel):
    status: Optional[str] = None
    assigned_user_id: Optional[int] = None
    due_date: Optional[date] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in STEP_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(STEP_STATUSES))}")
        return v


class ClientOnboardingStepRead(BaseModel):
    id: int
    template_step_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    step_type: str
    status: str
    is_required: bool
    display_order: int
    requires_approval: bool
    assigned_user: Optional[UserRef] = None
    due_date: Optional[date] = None
    submitted_at: Optional[datetime] = None
    reviewed_by: Optional[UserRef] = None
    reviewed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    review_comment: Optional[str] = None
    form_fields: list[ClientFormFieldRead] = []
    document_requirements: list[ClientDocumentRequirementRead] = []
    documents: list[DocumentRead] = []
    change_requests: list[ChangeRequestRead] = []

    model_config = {"from_attributes": True}


# ── Client onboarding ────────────────────────────────────────────────────────

class ClientOnboardingCreate(BaseModel):
    client_user_id: int
    project_id: int
    project_manager_id: Optional[int] = None
    template_id: Optional[int] = None
    due_date: Optional[date] = None


class ClientOnboardingUpdate(BaseModel):
    status: Optional[str] = None
    project_manager_id: Optional[int] = None
    due_date: Optional[date] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ONBOARDING_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(ONBOARDING_STATUSES))}")
        return v


class ClientOnboardingRead(BaseModel):
    id: int
    client: UserRef
    project: ProjectRef
    project_manager: Optional[UserRef] = None
    template_id: Optional[int] = None
    status: str
    progress_percentage: int
    started_at: Optional[datetime] = None
    due_date: Optional[date] = None
    completed_at: Optional[datetime] = None
    approved_by: Optional[UserRef] = None
    approved_at: Optional[datetime] = None
    created_by: Optional[UserRef] = None
    steps: list[ClientOnboardingStepRead] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ClientOnboardingListRead(BaseModel):
    """Lightweight row for list views — omits the full step checklist."""
    id: int
    client: UserRef
    project: ProjectRef
    project_manager: Optional[UserRef] = None
    status: str
    progress_percentage: int
    due_date: Optional[date] = None
    created_at: datetime

    model_config = {"from_attributes": True}
