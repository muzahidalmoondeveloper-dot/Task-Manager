import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class OnboardingTemplate(Base):
    __tablename__ = "onboarding_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    created_by = relationship("User", foreign_keys=[created_by_id], lazy="selectin")
    steps: Mapped[list["OnboardingTemplateStep"]] = relationship(
        "OnboardingTemplateStep", back_populates="template", lazy="selectin",
        cascade="all, delete-orphan", order_by="OnboardingTemplateStep.display_order",
    )


class OnboardingTemplateStep(Base):
    __tablename__ = "onboarding_template_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    template_id: Mapped[int] = mapped_column(Integer, ForeignKey("onboarding_templates.id", ondelete="CASCADE"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_type: Mapped[str] = mapped_column(String(30), nullable=False, default="manual_task", server_default="manual_task")
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    due_days_after_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    depends_on_step_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("onboarding_template_steps.id", ondelete="SET NULL"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    template: Mapped["OnboardingTemplate"] = relationship("OnboardingTemplate", back_populates="steps")
    form_fields: Mapped[list["OnboardingTemplateFormField"]] = relationship(
        "OnboardingTemplateFormField", back_populates="template_step", lazy="selectin",
        cascade="all, delete-orphan", order_by="OnboardingTemplateFormField.display_order",
    )
    document_requirements: Mapped[list["OnboardingTemplateDocumentRequirement"]] = relationship(
        "OnboardingTemplateDocumentRequirement", back_populates="template_step", lazy="selectin",
        cascade="all, delete-orphan", order_by="OnboardingTemplateDocumentRequirement.display_order",
    )


class OnboardingTemplateFormField(Base):
    """Reusable form-field definition for an information_form/questionnaire
    template step. Copied into ClientOnboardingFormField when a template is
    assigned, so later edits here never change an in-flight onboarding."""
    __tablename__ = "onboarding_template_form_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    template_step_id: Mapped[int] = mapped_column(Integer, ForeignKey("onboarding_template_steps.id", ondelete="CASCADE"), nullable=False, index=True)

    label: Mapped[str] = mapped_column(String(500), nullable=False)
    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False, default="short_text", server_default="short_text")
    placeholder: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    help_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    options_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # dropdown/multi_select choices

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    template_step: Mapped["OnboardingTemplateStep"] = relationship("OnboardingTemplateStep", back_populates="form_fields")


class OnboardingTemplateDocumentRequirement(Base):
    """A named document a client must upload for a document_upload template
    step (e.g. 'Brand Guidelines'). Copied at onboarding-creation time."""
    __tablename__ = "onboarding_template_document_requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    template_step_id: Mapped[int] = mapped_column(Integer, ForeignKey("onboarding_template_steps.id", ondelete="CASCADE"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    allowed_file_types: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # csv, e.g. "pdf,png,jpg"
    max_file_size_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    template_step: Mapped["OnboardingTemplateStep"] = relationship("OnboardingTemplateStep", back_populates="document_requirements")


class ClientOnboarding(Base):
    __tablename__ = "client_onboardings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)

    client_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    project_manager_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    template_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("onboarding_templates.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", server_default="draft")
    progress_percentage: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    client = relationship("User", foreign_keys=[client_user_id], lazy="selectin")
    project = relationship("Project", foreign_keys=[project_id], lazy="selectin")
    project_manager = relationship("User", foreign_keys=[project_manager_id], lazy="selectin")
    approved_by = relationship("User", foreign_keys=[approved_by_id], lazy="selectin")
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="selectin")
    template = relationship("OnboardingTemplate", foreign_keys=[template_id], lazy="selectin")

    steps: Mapped[list["ClientOnboardingStep"]] = relationship(
        "ClientOnboardingStep", back_populates="onboarding", lazy="selectin",
        cascade="all, delete-orphan", order_by="ClientOnboardingStep.display_order",
    )


class ClientOnboardingStep(Base):
    __tablename__ = "client_onboarding_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_onboarding_id: Mapped[int] = mapped_column(Integer, ForeignKey("client_onboardings.id", ondelete="CASCADE"), nullable=False, index=True)
    # Steps are copied from the template at onboarding-creation time (see
    # workflow spec §26) so later template edits never retroactively change
    # an in-flight onboarding record.
    template_step_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("onboarding_template_steps.id", ondelete="SET NULL"), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_type: Mapped[str] = mapped_column(String(30), nullable=False, default="manual_task", server_default="manual_task")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="not_started", server_default="not_started")
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    assigned_user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Client-visible note from the reviewer — set on approve or request-changes.
    review_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    onboarding: Mapped["ClientOnboarding"] = relationship("ClientOnboarding", back_populates="steps")
    assigned_user = relationship("User", foreign_keys=[assigned_user_id], lazy="selectin")
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id], lazy="selectin")

    form_fields: Mapped[list["ClientOnboardingFormField"]] = relationship(
        "ClientOnboardingFormField", back_populates="step", lazy="selectin",
        cascade="all, delete-orphan", order_by="ClientOnboardingFormField.display_order",
    )
    document_requirements: Mapped[list["ClientOnboardingDocumentRequirement"]] = relationship(
        "ClientOnboardingDocumentRequirement", back_populates="step", lazy="selectin",
        cascade="all, delete-orphan", order_by="ClientOnboardingDocumentRequirement.display_order",
    )
    documents: Mapped[list["OnboardingDocument"]] = relationship(
        "OnboardingDocument", back_populates="step", lazy="selectin",
        cascade="all, delete-orphan", order_by="OnboardingDocument.uploaded_at",
    )
    change_requests: Mapped[list["OnboardingChangeRequest"]] = relationship(
        "OnboardingChangeRequest", back_populates="step", lazy="selectin",
        cascade="all, delete-orphan", order_by="OnboardingChangeRequest.created_at",
    )


class ClientOnboardingFormField(Base):
    """Form field copied from the template at onboarding-creation time."""
    __tablename__ = "client_onboarding_form_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_onboarding_step_id: Mapped[int] = mapped_column(Integer, ForeignKey("client_onboarding_steps.id", ondelete="CASCADE"), nullable=False, index=True)
    template_field_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("onboarding_template_form_fields.id", ondelete="SET NULL"), nullable=True)

    label: Mapped[str] = mapped_column(String(500), nullable=False)
    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False, default="short_text", server_default="short_text")
    placeholder: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    help_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    options_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    step: Mapped["ClientOnboardingStep"] = relationship("ClientOnboardingStep", back_populates="form_fields")
    response: Mapped[Optional["OnboardingFormResponse"]] = relationship(
        "OnboardingFormResponse", back_populates="field", lazy="selectin",
        uselist=False, cascade="all, delete-orphan",
    )


class OnboardingFormResponse(Base):
    """Current answer for one field. Previous versions are preserved in
    OnboardingFormResponseVersion whenever a submitted answer is edited
    again (i.e. after a change request)."""
    __tablename__ = "onboarding_form_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    field_id: Mapped[int] = mapped_column(Integer, ForeignKey("client_onboarding_form_fields.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    client_user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    response_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # multi_select/checkbox values
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    field: Mapped["ClientOnboardingFormField"] = relationship("ClientOnboardingFormField", back_populates="response")


class OnboardingFormResponseVersion(Base):
    """Snapshot of a response taken just before it's overwritten by a
    resubmission, so history survives change-request cycles."""
    __tablename__ = "onboarding_form_response_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    field_id: Mapped[int] = mapped_column(Integer, ForeignKey("client_onboarding_form_fields.id", ondelete="CASCADE"), nullable=False, index=True)

    response_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientOnboardingDocumentRequirement(Base):
    """Document requirement copied from the template at onboarding-creation time."""
    __tablename__ = "client_onboarding_document_requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_onboarding_step_id: Mapped[int] = mapped_column(Integer, ForeignKey("client_onboarding_steps.id", ondelete="CASCADE"), nullable=False, index=True)
    template_requirement_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("onboarding_template_document_requirements.id", ondelete="SET NULL"), nullable=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    allowed_file_types: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    max_file_size_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    step: Mapped["ClientOnboardingStep"] = relationship("ClientOnboardingStep", back_populates="document_requirements")
    documents: Mapped[list["OnboardingDocument"]] = relationship(
        "OnboardingDocument", back_populates="requirement", lazy="selectin",
        order_by="OnboardingDocument.version",
    )


class OnboardingDocument(Base):
    """One uploaded file version. Replacing a rejected document inserts a
    new row (version + 1) rather than deleting the old one."""
    __tablename__ = "onboarding_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_onboarding_step_id: Mapped[int] = mapped_column(Integer, ForeignKey("client_onboarding_steps.id", ondelete="CASCADE"), nullable=False, index=True)
    requirement_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("client_onboarding_document_requirements.id", ondelete="SET NULL"), nullable=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)

    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploaded", server_default="uploaded")
    review_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    uploaded_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    step: Mapped["ClientOnboardingStep"] = relationship("ClientOnboardingStep", back_populates="documents")
    requirement: Mapped[Optional["ClientOnboardingDocumentRequirement"]] = relationship(
        "ClientOnboardingDocumentRequirement", back_populates="documents", lazy="selectin",
    )
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id], lazy="selectin")
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id], lazy="selectin")


class OnboardingChangeRequest(Base):
    """A reviewer's 'please fix this' note on a step — client-visible."""
    __tablename__ = "onboarding_change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_onboarding_step_id: Mapped[int] = mapped_column(Integer, ForeignKey("client_onboarding_steps.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    client_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    step: Mapped["ClientOnboardingStep"] = relationship("ClientOnboardingStep", back_populates="change_requests")
    requested_by = relationship("User", foreign_keys=[requested_by_id], lazy="selectin")
