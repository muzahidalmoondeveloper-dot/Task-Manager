from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, get_tenant_context, require_org_manager
from app.models.kpi import KPI, KPIEntry, KPIGroup, KpiLink
from app.models.organization import OrganizationMembership
from app.models.project import Project
from app.models.rock import Rock
from app.models.team import Team
from app.schemas.kpi import (
    EntityLinkIn,
    KPICreate,
    KPIUpdate,
    KPIOut,
    KPIEntryUpsert,
    KPIEntryOut,
    KPIEntryAddNote,
    KPIGroupCreate,
    KPIGroupOut,
    KPIGroupUpdate,
    KPIReorderItem,
    KPINoteUpdate,
)
from app.services.kpi_service import (
    compute_derived_entries,
    compute_view_statuses,
    validate_entry_value,
    validate_kpi_config,
)


def _apply_links(kpi: KPI, links: list[EntityLinkIn]) -> None:
    kpi.links = [
        KpiLink(linked_type=l.linked_type, linked_id=l.linked_id, title=l.title)
        for l in links
    ]

router = APIRouter(tags=["kpis"])


def _kpi_out(kpi: KPI) -> KPIOut:
    """Serialize a KPI with server-computed statuses and interpolated values."""
    out = KPIOut.model_validate(kpi)
    out.statuses = compute_view_statuses(kpi)
    out.derived_entries = compute_derived_entries(kpi)
    return out


async def _get_kpi_or_404(db: AsyncSession, team_id: int, kpi_id: int, org_id) -> KPI:
    result = await db.execute(
        select(KPI).where(
            KPI.id == kpi_id,
            KPI.team_id == team_id,
            KPI.organization_id == org_id,
        )
    )
    kpi = result.scalar_one_or_none()
    if not kpi:
        raise HTTPException(status_code=404, detail="KPI not found")
    return kpi


async def _get_entry_or_404(db: AsyncSession, kpi_id: int, entry_id: int, org_id) -> KPIEntry:
    result = await db.execute(
        select(KPIEntry)
        .join(KPI, KPIEntry.kpi_id == KPI.id)
        .where(
            KPIEntry.id == entry_id,
            KPIEntry.kpi_id == kpi_id,
            KPI.organization_id == org_id,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


# ─── Tenant-scoped reference validation ───────────────────────────────────────

async def _validate_team(db: AsyncSession, team_id: int, org_id) -> None:
    result = await db.execute(select(Team.id).where(Team.id == team_id, Team.organization_id == org_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Team not found")


async def _validate_owner(db: AsyncSession, owner_id: int | None, org_id) -> None:
    if owner_id is None:
        return
    result = await db.execute(
        select(OrganizationMembership.id).where(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == owner_id,
            OrganizationMembership.is_active.is_(True),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=422, detail="Owner must be an active member of this organization.")


async def _validate_rock(db: AsyncSession, rock_id: int | None, team_id: int, org_id, *, is_new_link: bool) -> None:
    """A KPI's rock must belong to the same org and team. New links to
    archived rocks are rejected; existing links survive archival so KPI
    history is preserved."""
    if rock_id is None:
        return
    result = await db.execute(
        select(Rock).where(Rock.id == rock_id, Rock.organization_id == org_id)
    )
    rock = result.scalar_one_or_none()
    if rock is None:
        raise HTTPException(status_code=422, detail="Rock not found in this organization.")
    if rock.team_id != team_id:
        raise HTTPException(status_code=422, detail="Rock must belong to the same team as the KPI.")
    if is_new_link and (rock.is_archived or rock.status == "archived"):
        raise HTTPException(status_code=422, detail="Cannot link a KPI to an archived Rock.")


async def _validate_group(db: AsyncSession, group_id: int | None, team_id: int, org_id) -> None:
    """A KPI's group must belong to the same team and organization."""
    if group_id is None:
        return
    result = await db.execute(
        select(KPIGroup).where(KPIGroup.id == group_id, KPIGroup.organization_id == org_id)
    )
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=422, detail="KPI group not found in this organization.")
    if group.team_id != team_id:
        raise HTTPException(status_code=422, detail="KPI group must belong to the same team as the KPI.")


async def _validate_project(db: AsyncSession, project_id: int | None, org_id) -> None:
    if project_id is None:
        return
    result = await db.execute(
        select(Project.id).where(Project.id == project_id, Project.organization_id == org_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=422, detail="Project not found in this organization.")


# ─── KPI Groups ───────────────────────────────────────────────────────────────

@router.get("/teams/{team_id}/kpi-groups", response_model=list[KPIGroupOut])
async def list_kpi_groups(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(KPIGroup)
        .where(KPIGroup.team_id == team_id, KPIGroup.organization_id == tenant.organization_id)
        .order_by(KPIGroup.name)
    )
    return result.scalars().all()


@router.post("/teams/{team_id}/kpi-groups", response_model=KPIGroupOut, status_code=status.HTTP_201_CREATED)
async def create_kpi_group(
    team_id: int,
    payload: KPIGroupCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_org_manager),
):
    await _validate_team(db, team_id, tenant.organization_id)

    async def _existing() -> KPIGroup | None:
        result = await db.execute(
            select(KPIGroup).where(
                KPIGroup.team_id == team_id,
                KPIGroup.organization_id == tenant.organization_id,
                KPIGroup.name == payload.name,
            )
        )
        return result.scalar_one_or_none()

    # Create-or-return-existing: the combobox offers "Create group" only when
    # no match is visible, but two users can race — the unique constraint wins.
    group = await _existing()
    if group:
        return group
    group = KPIGroup(
        team_id=team_id,
        organization_id=tenant.organization_id,
        name=payload.name,
        formula=payload.formula,
        collapse_by_default=payload.collapse_by_default,
    )
    db.add(group)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        group = await _existing()
        if group is None:
            raise HTTPException(status_code=409, detail="Could not create the group. Please retry.")
        return group
    await db.refresh(group)
    return group


@router.patch("/teams/{team_id}/kpi-groups/{group_id}", response_model=KPIGroupOut)
async def update_kpi_group(
    team_id: int,
    group_id: int,
    payload: KPIGroupUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_org_manager),
):
    result = await db.execute(
        select(KPIGroup).where(
            KPIGroup.id == group_id,
            KPIGroup.team_id == team_id,
            KPIGroup.organization_id == tenant.organization_id,
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="KPI group not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=422, detail="A group with this name already exists for this team.")
    await db.refresh(group)
    return group


@router.delete("/teams/{team_id}/kpi-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kpi_group(
    team_id: int,
    group_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_org_manager),
):
    """Deleting a group never deletes its KPIs — the FK is SET NULL, so member
    KPIs simply become ungrouped."""
    result = await db.execute(
        select(KPIGroup).where(
            KPIGroup.id == group_id,
            KPIGroup.team_id == team_id,
            KPIGroup.organization_id == tenant.organization_id,
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="KPI group not found")
    await db.delete(group)
    await db.commit()


# ─── KPI CRUD ─────────────────────────────────────────────────────────────────

@router.get("/teams/{team_id}/kpis", response_model=list[KPIOut])
async def list_kpis(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(KPI)
        .where(KPI.team_id == team_id, KPI.organization_id == tenant.organization_id)
        .order_by(KPI.sort_order, KPI.created_at.desc())
    )
    return [_kpi_out(k) for k in result.scalars().all()]


@router.put("/teams/{team_id}/kpis/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_kpis(
    team_id: int,
    payload: list[KPIReorderItem],
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_org_manager),
):
    for item in payload:
        result = await db.execute(
            select(KPI).where(
                KPI.id == item.id,
                KPI.team_id == team_id,
                KPI.organization_id == tenant.organization_id,
            )
        )
        kpi = result.scalar_one_or_none()
        if kpi:
            kpi.sort_order = item.sort_order
    await db.commit()


@router.post("/teams/{team_id}/kpis", response_model=KPIOut, status_code=status.HTTP_201_CREATED)
async def create_kpi(
    team_id: int,
    payload: KPICreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_org_manager),
):
    await _validate_team(db, team_id, tenant.organization_id)
    await _validate_owner(db, payload.owner_id, tenant.organization_id)
    await _validate_rock(db, payload.rock_id, team_id, tenant.organization_id, is_new_link=True)
    await _validate_project(db, payload.project_id, tenant.organization_id)
    await _validate_group(db, payload.kpi_group_id, team_id, tenant.organization_id)

    kpi = KPI(
        team_id=team_id,
        organization_id=tenant.organization_id,
        created_by_id=tenant.user.id,
        **payload.model_dump(exclude={"links"}),
    )
    _apply_links(kpi, payload.links)
    db.add(kpi)
    await db.commit()
    await db.refresh(kpi)
    return _kpi_out(kpi)


@router.patch("/teams/{team_id}/kpis/{kpi_id}", response_model=KPIOut)
async def update_kpi(
    team_id: int,
    kpi_id: int,
    payload: KPIUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_org_manager),
):
    kpi = await _get_kpi_or_404(db, team_id, kpi_id, tenant.organization_id)
    updates = payload.model_dump(exclude_unset=True, exclude={"links"})

    # The rock link is mandatory — it can be changed but never cleared.
    if "rock_id" in updates and updates["rock_id"] is None:
        raise HTTPException(status_code=422, detail="A KPI must be linked to a Rock.")

    # Validate the resulting configuration (current values + updates merged).
    merged = {
        f: updates.get(f, getattr(kpi, f))
        for f in ("interpolation", "target_type", "formula", "reference_value", "reference_max", "supported_views")
    }
    try:
        validate_kpi_config(**{k: merged[k] for k in merged})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    target_team_id = updates.get("team_id", kpi.team_id)
    if "team_id" in updates and updates["team_id"] != kpi.team_id:
        await _validate_team(db, target_team_id, tenant.organization_id)
    if "owner_id" in updates:
        await _validate_owner(db, updates["owner_id"], tenant.organization_id)
    if "project_id" in updates:
        await _validate_project(db, updates["project_id"], tenant.organization_id)
    # Re-validate the group when it changes OR when the KPI moves to another team.
    if "kpi_group_id" in updates or target_team_id != kpi.team_id:
        await _validate_group(
            db, updates.get("kpi_group_id", kpi.kpi_group_id), target_team_id, tenant.organization_id
        )
    # Re-validate the rock when it changes OR when the KPI moves to another team.
    rock_changes = "rock_id" in updates and updates["rock_id"] != kpi.rock_id
    if rock_changes or target_team_id != kpi.team_id:
        await _validate_rock(
            db,
            updates.get("rock_id", kpi.rock_id),
            target_team_id,
            tenant.organization_id,
            is_new_link=rock_changes,
        )

    for field, value in updates.items():
        setattr(kpi, field, value)
    if payload.links is not None:
        for l in list(kpi.links):
            await db.delete(l)
        await db.flush()
        _apply_links(kpi, payload.links)
    await db.commit()
    await db.refresh(kpi)
    return _kpi_out(kpi)


@router.delete("/teams/{team_id}/kpis/{kpi_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kpi(
    team_id: int,
    kpi_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(require_org_manager),
):
    kpi = await _get_kpi_or_404(db, team_id, kpi_id, tenant.organization_id)
    await db.delete(kpi)
    await db.commit()


# ─── Value recording ──────────────────────────────────────────────────────────

@router.put("/teams/{team_id}/kpis/{kpi_id}/entries", response_model=KPIEntryOut)
async def upsert_entry(
    team_id: int,
    kpi_id: int,
    payload: KPIEntryUpsert,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    kpi = await _get_kpi_or_404(db, team_id, kpi_id, tenant.organization_id)
    try:
        validate_entry_value(kpi.target_type, payload.value)
        validate_entry_value(kpi.target_type, payload.forecast)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    async def _find_existing() -> KPIEntry | None:
        result = await db.execute(
            select(KPIEntry).where(
                and_(
                    KPIEntry.kpi_id == kpi_id,
                    KPIEntry.period_start == payload.period_start,
                    KPIEntry.period_type == payload.period_type,
                )
            )
        )
        return result.scalar_one_or_none()

    entry = await _find_existing()
    if entry:
        entry.value = payload.value
        entry.forecast = payload.forecast
        await db.commit()
    else:
        entry = KPIEntry(
            kpi_id=kpi_id,
            value=payload.value,
            forecast=payload.forecast,
            notes=[],
            period_start=payload.period_start,
            period_type=payload.period_type,
        )
        db.add(entry)
        try:
            await db.commit()
        except IntegrityError:
            # Concurrent insert for the same period — the unique constraint
            # won the race; update the surviving row instead.
            await db.rollback()
            entry = await _find_existing()
            if entry is None:
                raise HTTPException(status_code=409, detail="Could not save the value. Please retry.")
            entry.value = payload.value
            entry.forecast = payload.forecast
            await db.commit()
    await db.refresh(entry)
    return entry


@router.post("/teams/{team_id}/kpis/{kpi_id}/entries/{entry_id}/notes", response_model=KPIEntryOut)
async def add_entry_note(
    team_id: int,
    kpi_id: int,
    entry_id: int,
    payload: KPIEntryAddNote,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    from datetime import datetime as dt
    entry = await _get_entry_or_404(db, kpi_id, entry_id, tenant.organization_id)
    notes = list(entry.notes or [])
    notes.append({
        "text": payload.text,
        "created_at": dt.utcnow().isoformat(),
        "author_id": tenant.user.id,
        "author_name": tenant.user.full_name or tenant.user.email,
    })
    entry.notes = notes
    await db.commit()
    await db.refresh(entry)
    return entry


@router.patch("/teams/{team_id}/kpis/{kpi_id}/entries/{entry_id}/notes/{note_idx}", response_model=KPIEntryOut)
async def edit_entry_note(
    team_id: int,
    kpi_id: int,
    entry_id: int,
    note_idx: int,
    payload: KPINoteUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    entry = await _get_entry_or_404(db, kpi_id, entry_id, tenant.organization_id)
    notes = list(entry.notes or [])
    if note_idx < 0 or note_idx >= len(notes):
        raise HTTPException(status_code=404, detail="Note not found")
    notes[note_idx] = {**notes[note_idx], "text": payload.text}
    entry.notes = notes
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/teams/{team_id}/kpis/{kpi_id}/entries/{entry_id}/notes/{note_idx}", response_model=KPIEntryOut)
async def delete_entry_note(
    team_id: int,
    kpi_id: int,
    entry_id: int,
    note_idx: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    entry = await _get_entry_or_404(db, kpi_id, entry_id, tenant.organization_id)
    notes = list(entry.notes or [])
    if note_idx < 0 or note_idx >= len(notes):
        raise HTTPException(status_code=404, detail="Note not found")
    notes.pop(note_idx)
    entry.notes = notes
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/teams/{team_id}/kpis/{kpi_id}/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    team_id: int,
    kpi_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    entry = await _get_entry_or_404(db, kpi_id, entry_id, tenant.organization_id)
    await db.delete(entry)
    await db.commit()
