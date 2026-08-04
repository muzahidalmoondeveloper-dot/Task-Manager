import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AppException, ErrorDef
from app.core.onboarding_constants import (
    ONBOARDING_DUPLICATE_BLOCKING_STATUSES,
    ONBOARDING_TERMINAL_STATUSES,
    STEP_STATUSES_COMPLETE,
)
from app.models.onboarding import (
    ClientOnboarding,
    ClientOnboardingDocumentRequirement,
    ClientOnboardingFormField,
    ClientOnboardingStep,
    OnboardingChangeRequest,
    OnboardingDocument,
    OnboardingFormResponse,
    OnboardingFormResponseVersion,
    OnboardingTemplate,
    OnboardingTemplateDocumentRequirement,
    OnboardingTemplateFormField,
    OnboardingTemplateStep,
)
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.onboarding import (
    ClientOnboardingCreate,
    ClientOnboardingStepUpdate,
    ClientOnboardingUpdate,
    DocumentReviewRequest,
    FormResponsesSaveRequest,
    OnboardingTemplateCreate,
    OnboardingTemplateStepCreate,
    OnboardingTemplateUpdate,
)
from fastapi import status as http_status

_STEP_NOT_READY = ErrorDef(
    code="ONBOARDING_STEP_NOT_READY",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="This step is missing required information and cannot be submitted yet.",
)
_DEFAULT_TEMPLATE_PROTECTED = ErrorDef(
    code="DEFAULT_TEMPLATE_PROTECTED",
    status=http_status.HTTP_400_BAD_REQUEST,
    message="This is the default onboarding template. Set another template as default first.",
)
_TEMPLATE_REQUIRED = ErrorDef(
    code="ONBOARDING_TEMPLATE_REQUIRED",
    status=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
    message="Create or configure an onboarding template first.",
)


def _build_template_step(step: OnboardingTemplateStepCreate) -> OnboardingTemplateStep:
    data = step.model_dump(exclude={"form_fields", "document_requirements"})
    model = OnboardingTemplateStep(**data)
    model.form_fields = [
        OnboardingTemplateFormField(**f.model_dump())
        for f in sorted(step.form_fields, key=lambda f: f.display_order)
    ]
    model.document_requirements = [
        OnboardingTemplateDocumentRequirement(**d.model_dump())
        for d in sorted(step.document_requirements, key=lambda d: d.display_order)
    ]
    return model


class OnboardingTemplateRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    async def list_all(self) -> list[OnboardingTemplate]:
        stmt = (
            select(OnboardingTemplate)
            .where(OnboardingTemplate.organization_id == self.org_id)
            .order_by(OnboardingTemplate.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_by_id(self, template_id: int) -> OnboardingTemplate | None:
        stmt = select(OnboardingTemplate).where(
            OnboardingTemplate.id == template_id,
            OnboardingTemplate.organization_id == self.org_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_default(self) -> OnboardingTemplate | None:
        stmt = select(OnboardingTemplate).where(
            OnboardingTemplate.organization_id == self.org_id,
            OnboardingTemplate.is_default.is_(True),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _unset_other_defaults(self, keep_id: int | None) -> None:
        stmt = select(OnboardingTemplate).where(
            OnboardingTemplate.organization_id == self.org_id,
            OnboardingTemplate.is_default.is_(True),
        )
        if keep_id is not None:
            stmt = stmt.where(OnboardingTemplate.id != keep_id)
        result = await self.db.execute(stmt)
        for other in result.scalars().all():
            other.is_default = False

    async def create(self, payload: OnboardingTemplateCreate, created_by_id: int) -> OnboardingTemplate:
        # Every org must have exactly one active default template — the very
        # first template created for an org is always the default, regardless
        # of what the payload asked for, so the invariant holds from the start.
        existing = await self.list_all()
        is_default = payload.is_default or not existing

        template = OnboardingTemplate(
            organization_id=self.org_id,
            name=payload.name,
            description=payload.description,
            is_default=is_default,
            created_by_id=created_by_id,
        )
        template.steps = [_build_template_step(step) for step in sorted(payload.steps, key=lambda s: s.display_order)]
        self.db.add(template)
        await self.db.flush()
        if is_default:
            await self._unset_other_defaults(keep_id=template.id)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def update(self, template: OnboardingTemplate, payload: OnboardingTemplateUpdate) -> OnboardingTemplate:
        data = payload.model_dump(exclude_unset=True, exclude={"steps"})

        # Exactly one default template per org, always: promoting this one
        # unsets every other default; un-defaulting or deactivating THE
        # current default without first promoting another is rejected so the
        # org is never left with zero (or more than one) default template.
        if data.get("is_default") is True:
            await self._unset_other_defaults(keep_id=template.id)
        elif template.is_default and (data.get("is_default") is False or data.get("is_active") is False):
            raise AppException(_DEFAULT_TEMPLATE_PROTECTED)

        for key, value in data.items():
            setattr(template, key, value)

        if payload.steps is not None:
            template.steps = [_build_template_step(step) for step in sorted(payload.steps, key=lambda s: s.display_order)]

        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def delete(self, template: OnboardingTemplate) -> None:
        if template.is_default:
            raise AppException(_DEFAULT_TEMPLATE_PROTECTED)
        await self.db.delete(template)
        await self.db.commit()

    async def duplicate(self, template: OnboardingTemplate, created_by_id: int) -> OnboardingTemplate:
        copy = OnboardingTemplate(
            organization_id=self.org_id,
            name=f"{template.name} (Copy)",
            description=template.description,
            is_default=False,
            created_by_id=created_by_id,
        )
        new_steps = []
        for s in template.steps:
            step = OnboardingTemplateStep(
                title=s.title,
                description=s.description,
                step_type=s.step_type,
                is_required=s.is_required,
                display_order=s.display_order,
                requires_approval=s.requires_approval,
                due_days_after_start=s.due_days_after_start,
            )
            step.form_fields = [
                OnboardingTemplateFormField(
                    label=f.label, field_key=f.field_key, field_type=f.field_type,
                    placeholder=f.placeholder, help_text=f.help_text, is_required=f.is_required,
                    display_order=f.display_order, options_json=f.options_json,
                )
                for f in s.form_fields
            ]
            step.document_requirements = [
                OnboardingTemplateDocumentRequirement(
                    name=d.name, description=d.description, is_required=d.is_required,
                    display_order=d.display_order, allowed_file_types=d.allowed_file_types,
                    max_file_size_mb=d.max_file_size_mb,
                )
                for d in s.document_requirements
            ]
            new_steps.append(step)
        copy.steps = new_steps
        self.db.add(copy)
        await self.db.commit()
        await self.db.refresh(copy)
        return copy


def _recompute_progress(onboarding: ClientOnboarding) -> None:
    required = [s for s in onboarding.steps if s.is_required]
    if not required:
        onboarding.progress_percentage = 100 if onboarding.steps else 0
        return
    done = sum(1 for s in required if s.status in STEP_STATUSES_COMPLETE)
    onboarding.progress_percentage = round(done / len(required) * 100)


def _sync_onboarding_status(onboarding: ClientOnboarding) -> None:
    """Keep the parent onboarding's own status in step with what its steps
    are actually doing — called after every step mutation. Never touches a
    terminal status (completed/rejected/cancelled/archived); those are only
    ever set by an explicit admin action."""
    if onboarding.status in ONBOARDING_TERMINAL_STATUSES:
        return

    statuses = [s.status for s in onboarding.steps]
    if not statuses:
        return

    if onboarding.progress_percentage >= 100:
        onboarding.status = "ready_for_approval"
    elif any(s == "changes_requested" for s in statuses):
        onboarding.status = "changes_requested"
    elif any(s in ("submitted", "under_review") for s in statuses):
        onboarding.status = "under_review"
    elif any(s == "in_progress" for s in statuses):
        onboarding.status = "in_progress"
        if onboarding.started_at is None:
            onboarding.started_at = datetime.now(timezone.utc)


class ClientOnboardingRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    def _base_query(self):
        return select(ClientOnboarding).where(ClientOnboarding.organization_id == self.org_id)

    async def list_all(
        self, project_manager_id: int | None = None, client_user_id: int | None = None
    ) -> list[ClientOnboarding]:
        stmt = self._base_query().order_by(ClientOnboarding.created_at.desc())
        if project_manager_id is not None:
            stmt = stmt.where(ClientOnboarding.project_manager_id == project_manager_id)
        if client_user_id is not None:
            stmt = stmt.where(ClientOnboarding.client_user_id == client_user_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def delete(self, onboarding: ClientOnboarding) -> None:
        await self.db.delete(onboarding)
        await self.db.commit()

    async def get_by_id(self, onboarding_id: int) -> ClientOnboarding | None:
        stmt = self._base_query().where(ClientOnboarding.id == onboarding_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_client_project(
        self, client_user_id: int, project_id: int, exclude_id: int | None = None
    ) -> ClientOnboarding | None:
        """Blocks starting a second onboarding cycle for this exact
        client+project pair while one is in draft, in_progress, under_review,
        changes_requested, waiting_for_client, or ready_for_approval — a new
        cycle is only allowed once the existing one is completed, cancelled,
        or archived (this org already scopes `_base_query()`)."""
        stmt = self._base_query().where(
            ClientOnboarding.client_user_id == client_user_id,
            ClientOnboarding.project_id == project_id,
            ClientOnboarding.status.in_(ONBOARDING_DUPLICATE_BLOCKING_STATUSES),
        )
        if exclude_id is not None:
            stmt = stmt.where(ClientOnboarding.id != exclude_id)
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def audit_inconsistencies(self) -> list[dict]:
        """Read-only diagnostic scan — flags records whose stored state
        doesn't match what the current rules would derive, without touching
        them. Used by the admin-facing audit endpoint."""
        records = await self.list_all()
        findings: list[dict] = []

        seen: dict[tuple[int, int], list[int]] = {}
        for onboarding in records:
            key = (onboarding.client_user_id, onboarding.project_id)
            seen.setdefault(key, [])
            if onboarding.status in ONBOARDING_DUPLICATE_BLOCKING_STATUSES:
                seen[key].append(onboarding.id)

        for onboarding in records:
            reasons = []
            key = (onboarding.client_user_id, onboarding.project_id)
            if len(seen.get(key, [])) > 1 and onboarding.status in ONBOARDING_DUPLICATE_BLOCKING_STATUSES:
                reasons.append("duplicate_active_onboarding_for_client_project")

            if onboarding.progress_percentage >= 100 and onboarding.status not in (
                {"ready_for_approval"} | ONBOARDING_TERMINAL_STATUSES
            ):
                reasons.append("progress_complete_but_status_not_ready")

            if onboarding.progress_percentage < 100 and onboarding.status == "completed":
                reasons.append("marked_completed_but_progress_incomplete")

            if onboarding.due_date is not None and onboarding.status not in ONBOARDING_TERMINAL_STATUSES:
                if onboarding.due_date < datetime.now(timezone.utc).date():
                    reasons.append("past_due_date_still_active")

            if reasons:
                findings.append({
                    "onboarding_id": onboarding.id,
                    "client_user_id": onboarding.client_user_id,
                    "project_id": onboarding.project_id,
                    "status": onboarding.status,
                    "progress_percentage": onboarding.progress_percentage,
                    "due_date": onboarding.due_date.isoformat() if onboarding.due_date else None,
                    "reasons": reasons,
                })
        return findings

    async def create(
        self,
        payload: ClientOnboardingCreate,
        created_by_id: int,
        template: OnboardingTemplate | None,
    ) -> ClientOnboarding:
        # A real, non-empty template is mandatory — no blank onboarding
        # records. Callers resolve the selected template or the org's
        # default before getting here; this is the single choke point that
        # enforces it no matter which route (direct create or
        # accept-invitation) called in.
        if template is None or not template.steps:
            raise AppException(_TEMPLATE_REQUIRED)

        onboarding = ClientOnboarding(
            organization_id=self.org_id,
            client_user_id=payload.client_user_id,
            project_id=payload.project_id,
            project_manager_id=payload.project_manager_id,
            # Use the resolved template's own id (not payload.template_id)
            # so the FK is correct even when no template was explicitly
            # chosen and the caller resolved the org's default template.
            template_id=template.id,
            due_date=payload.due_date,
            created_by_id=created_by_id,
            status="draft",
        )
        # Steps (and their fields/document requirements) are copied from the
        # template at creation time — later template edits never
        # retroactively change an in-flight onboarding record.
        client_steps = []
        for step in template.steps:
            client_step = ClientOnboardingStep(
                template_step_id=step.id,
                title=step.title,
                description=step.description,
                step_type=step.step_type,
                is_required=step.is_required,
                display_order=step.display_order,
                requires_approval=step.requires_approval,
            )
            client_step.form_fields = [
                ClientOnboardingFormField(
                    template_field_id=f.id, label=f.label, field_key=f.field_key,
                    field_type=f.field_type, placeholder=f.placeholder, help_text=f.help_text,
                    is_required=f.is_required, display_order=f.display_order, options_json=f.options_json,
                )
                for f in step.form_fields
            ]
            client_step.document_requirements = [
                ClientOnboardingDocumentRequirement(
                    template_requirement_id=d.id, name=d.name, description=d.description,
                    is_required=d.is_required, display_order=d.display_order,
                    allowed_file_types=d.allowed_file_types, max_file_size_mb=d.max_file_size_mb,
                )
                for d in step.document_requirements
            ]
            client_steps.append(client_step)
        onboarding.steps = client_steps
        _recompute_progress(onboarding)
        _sync_onboarding_status(onboarding)
        self.db.add(onboarding)
        await self.db.commit()
        # `db.refresh()` only reloads the object's own scalar columns — the
        # freshly-created steps and their nested fields/requirements were
        # expired by the commit, and a bare `refresh()` won't re-trigger their
        # selectin loaders reliably during response serialization. Re-fetch
        # via a clean query instead, which loads the whole graph properly.
        return await self.get_by_id(onboarding.id)

    async def update(self, onboarding: ClientOnboarding, payload: ClientOnboardingUpdate) -> ClientOnboarding:
        data = payload.model_dump(exclude_unset=True)
        now = datetime.now(timezone.utc)

        if "status" in data and data["status"] != onboarding.status:
            new_status = data["status"]
            if new_status == "in_progress" and onboarding.started_at is None:
                onboarding.started_at = now
            if new_status == "completed":
                onboarding.completed_at = now

        for key, value in data.items():
            setattr(onboarding, key, value)

        await self.db.commit()
        await self.db.refresh(onboarding)
        return onboarding

    async def start(self, onboarding: ClientOnboarding) -> ClientOnboarding:
        if onboarding.status == "draft":
            onboarding.status = "in_progress"
            onboarding.started_at = datetime.now(timezone.utc)
            await self.db.commit()
            await self.db.refresh(onboarding)
        return onboarding

    async def get_step(self, onboarding_id: int, step_id: int) -> ClientOnboardingStep | None:
        stmt = select(ClientOnboardingStep).where(
            ClientOnboardingStep.id == step_id,
            ClientOnboardingStep.client_onboarding_id == onboarding_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_step_by_id(self, step_id: int) -> ClientOnboardingStep | None:
        """Look up a step without knowing its onboarding_id up front — used
        when resolving a step from a document row. Still org-scoped via the
        joined ClientOnboarding, so this can't leak another org's data."""
        stmt = (
            select(ClientOnboardingStep)
            .join(ClientOnboarding, ClientOnboarding.id == ClientOnboardingStep.client_onboarding_id)
            .where(ClientOnboardingStep.id == step_id, ClientOnboarding.organization_id == self.org_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_step(
        self, onboarding: ClientOnboarding, step: ClientOnboardingStep, payload: ClientOnboardingStepUpdate
    ) -> ClientOnboardingStep:
        data = payload.model_dump(exclude_unset=True)
        now = datetime.now(timezone.utc)

        if "status" in data and data["status"] != step.status:
            new_status = data["status"]
            if new_status == "submitted":
                step.submitted_at = now
            if new_status in ("approved", "completed"):
                step.completed_at = now

        for key, value in data.items():
            setattr(step, key, value)

        _recompute_progress(onboarding)
        _sync_onboarding_status(onboarding)
        await self.db.commit()
        return await self.get_step(onboarding.id, step.id)

    # ── Form responses ──────────────────────────────────────────────────────

    async def get_field(self, step: ClientOnboardingStep, field_id: int) -> ClientOnboardingFormField | None:
        for f in step.form_fields:
            if f.id == field_id:
                return f
        return None

    async def save_responses(
        self,
        onboarding: ClientOnboarding,
        step: ClientOnboardingStep,
        payload: FormResponsesSaveRequest,
        client_user_id: int,
    ) -> ClientOnboardingStep:
        now = datetime.now(timezone.utc)
        by_field = {item.field_id: item for item in payload.responses}

        for field in step.form_fields:
            item = by_field.get(field.id)
            if item is None:
                continue
            existing = field.response
            if existing is not None and not existing.is_draft:
                # Being resubmitted after review — snapshot the old answer
                # before overwriting so history survives the change-request cycle.
                self.db.add(OnboardingFormResponseVersion(
                    field_id=field.id,
                    response_text=existing.response_text,
                    response_json=existing.response_json,
                    version=existing.version,
                    submitted_at=existing.submitted_at,
                ))
                existing.version += 1
            if existing is None:
                existing = OnboardingFormResponse(field_id=field.id, client_user_id=client_user_id)
                self.db.add(existing)
                field.response = existing
            existing.response_text = item.response_text
            existing.response_json = item.response_json
            existing.is_draft = not payload.submit
            existing.client_user_id = client_user_id
            if payload.submit:
                existing.submitted_at = now

        if step.status == "not_started":
            step.status = "in_progress"

        await self.db.flush()

        if payload.submit:
            await self._submit_step(onboarding, step)
        else:
            await self.db.commit()
        return await self.get_step(onboarding.id, step.id)

    async def _validate_step_ready(self, step: ClientOnboardingStep) -> None:
        if step.step_type in ("information_form", "questionnaire"):
            for field in step.form_fields:
                if not field.is_required:
                    continue
                response = field.response
                has_value = response is not None and (
                    (response.response_text is not None and response.response_text.strip() != "")
                    or (response.response_json is not None and len(response.response_json) > 0)
                )
                if not has_value:
                    raise AppException(_STEP_NOT_READY, message=f'"{field.label}" is required.')
        elif step.step_type == "document_upload":
            for req in step.document_requirements:
                if not req.is_required:
                    continue
                has_active_doc = any(d.status in ("uploaded", "under_review", "approved") for d in req.documents)
                if not has_active_doc:
                    raise AppException(_STEP_NOT_READY, message=f'"{req.name}" must be uploaded.')

    async def _submit_step(self, onboarding: ClientOnboarding, step: ClientOnboardingStep) -> None:
        await self._validate_step_ready(step)
        now = datetime.now(timezone.utc)
        step.status = "submitted"
        step.submitted_at = now
        # A resubmission resolves any open change request on this step.
        for cr in step.change_requests:
            if cr.status == "open":
                cr.status = "resolved"
                cr.resolved_at = now
        _recompute_progress(onboarding)
        _sync_onboarding_status(onboarding)
        await self.db.commit()

    async def submit_step(self, onboarding: ClientOnboarding, step: ClientOnboardingStep) -> ClientOnboardingStep:
        await self._submit_step(onboarding, step)
        return await self.get_step(onboarding.id, step.id)

    # ── Documents ────────────────────────────────────────────────────────────

    async def get_document_requirement(
        self, step: ClientOnboardingStep, requirement_id: int
    ) -> ClientOnboardingDocumentRequirement | None:
        for r in step.document_requirements:
            if r.id == requirement_id:
                return r
        return None

    async def add_document(
        self,
        step: ClientOnboardingStep,
        requirement: ClientOnboardingDocumentRequirement | None,
        file_name: str,
        file_url: str,
        file_type: str,
        file_size: int,
        uploaded_by_id: int,
    ) -> OnboardingDocument:
        version = 1
        if requirement is not None:
            existing_versions = [d.version for d in requirement.documents]
            if existing_versions:
                version = max(existing_versions) + 1
                # Replacing a rejected (or any prior) document — keep the old
                # row as history rather than deleting it.
                for d in requirement.documents:
                    if d.status != "replaced":
                        d.status = "replaced"

        document = OnboardingDocument(
            client_onboarding_step_id=step.id,
            requirement_id=requirement.id if requirement else None,
            organization_id=self.org_id,
            file_name=file_name,
            file_url=file_url,
            file_type=file_type,
            file_size=file_size,
            version=version,
            status="uploaded",
            uploaded_by_id=uploaded_by_id,
        )
        self.db.add(document)
        if requirement is not None:
            requirement.documents.append(document)
        else:
            step.documents.append(document)
        if step.status == "not_started":
            step.status = "in_progress"
        await self.db.commit()
        await self.db.refresh(document)
        return document

    async def get_document(self, document_id: int) -> OnboardingDocument | None:
        result = await self.db.execute(
            select(OnboardingDocument).where(
                OnboardingDocument.id == document_id,
                OnboardingDocument.organization_id == self.org_id,
            )
        )
        return result.scalar_one_or_none()

    async def review_document(
        self, document: OnboardingDocument, payload: DocumentReviewRequest, reviewer_id: int
    ) -> OnboardingDocument:
        document.status = payload.status
        document.review_comment = payload.review_comment
        document.reviewed_by_id = reviewer_id
        document.reviewed_at = datetime.now(timezone.utc)
        await self.db.commit()
        await self.db.refresh(document)
        return document

    # ── Review workflow (approve / request changes) ─────────────────────────

    async def approve_step(
        self, onboarding: ClientOnboarding, step: ClientOnboardingStep, reviewer_id: int, comment: str | None
    ) -> ClientOnboardingStep:
        now = datetime.now(timezone.utc)
        step.status = "approved"
        step.reviewed_by_id = reviewer_id
        step.reviewed_at = now
        step.completed_at = now
        if comment:
            step.review_comment = comment
        _recompute_progress(onboarding)
        _sync_onboarding_status(onboarding)
        await self.db.commit()
        return await self.get_step(onboarding.id, step.id)

    async def request_step_changes(
        self, onboarding: ClientOnboarding, step: ClientOnboardingStep, reason: str, reviewer_id: int
    ) -> ClientOnboardingStep:
        now = datetime.now(timezone.utc)
        step.status = "changes_requested"
        step.reviewed_by_id = reviewer_id
        step.reviewed_at = now
        step.review_comment = reason
        change_request = OnboardingChangeRequest(
            client_onboarding_step_id=step.id,
            requested_by_id=reviewer_id,
            reason=reason,
            status="open",
        )
        self.db.add(change_request)
        # Keep the in-memory relationship in sync — the identity map will
        # otherwise hand back the `step` object with its previously-loaded
        # (now stale) empty `change_requests` collection on re-fetch.
        step.change_requests.append(change_request)
        _recompute_progress(onboarding)
        _sync_onboarding_status(onboarding)
        await self.db.commit()
        return await self.get_step(onboarding.id, step.id)
