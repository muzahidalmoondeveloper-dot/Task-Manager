import random
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, get_tenant_context
from app.models.meeting import (
    Meeting, MeetingParticipant, MeetingAgendaItem,
    MeetingNote, MeetingDecision, MeetingTask,
)
from app.models.task import Task
from app.schemas.meeting import (
    MeetingCreate, MeetingUpdate, MeetingOut,
    AgendaItemCreate, AgendaItemUpdate, AgendaItemOut, AgendaReorderItem,
    NoteCreate, NoteUpdate, NoteOut,
    DecisionCreate, DecisionOut,
    MeetingCreateTask, MeetingTaskOut,
    MeetingSummaryOut,
    MeetingParticipantOut, ParticipantJoinUpdate, ParticipantScoreUpdate, SelectSpeakerRequest,
)

router = APIRouter(prefix="/teams/{team_id}/meetings", tags=["meetings"])


async def _get_meeting(
    team_id: int, meeting_id: int, tenant: TenantContext, db: AsyncSession
) -> Meeting:
    result = await db.execute(
        select(Meeting).where(
            Meeting.id == meeting_id,
            Meeting.team_id == team_id,
            Meeting.organization_id == tenant.organization_id,
        )
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


# ─── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[MeetingOut])
async def list_meetings(
    team_id: int,
    filter: str = Query(None),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    q = select(Meeting).where(
        Meeting.team_id == team_id,
        Meeting.organization_id == tenant.organization_id,
    )
    if filter == "ongoing":
        q = q.where(Meeting.status == "ongoing")
    elif filter == "completed":
        q = q.where(Meeting.status == "completed")
    elif filter == "scheduled":
        q = q.where(Meeting.status == "scheduled")
    q = q.order_by(Meeting.scheduled_at.desc())
    result = await db.execute(q)
    return result.scalars().all()


@router.post("", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
async def create_meeting(
    team_id: int,
    payload: MeetingCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = Meeting(
        team_id=team_id,
        organization_id=tenant.organization_id,
        title=payload.title,
        description=payload.description,
        scheduled_at=payload.scheduled_at,
        duration_minutes=payload.duration_minutes,
        meeting_type=payload.meeting_type,
        organizer_id=payload.organizer_id or tenant.user.id,
    )
    db.add(meeting)
    await db.flush()

    seen = set()
    for user_id in payload.participant_ids:
        if user_id not in seen:
            db.add(MeetingParticipant(meeting_id=meeting.id, user_id=user_id))
            seen.add(user_id)

    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.get("/{meeting_id}", response_model=MeetingOut)
async def get_meeting(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await _get_meeting(team_id, meeting_id, tenant, db)


@router.patch("/{meeting_id}", response_model=MeetingOut)
async def update_meeting(
    team_id: int,
    meeting_id: int,
    payload: MeetingUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)

    for field, value in payload.model_dump(exclude_none=True, exclude={"participant_ids"}).items():
        setattr(meeting, field, value)

    if payload.participant_ids is not None:
        for p in list(meeting.participants):
            await db.delete(p)
        await db.flush()
        seen = set()
        for user_id in payload.participant_ids:
            if user_id not in seen:
                db.add(MeetingParticipant(meeting_id=meeting.id, user_id=user_id))
                seen.add(user_id)

    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.patch("/{meeting_id}/participants/{user_id}", response_model=MeetingParticipantOut)
async def set_participant_joined(
    team_id: int,
    meeting_id: int,
    user_id: int,
    payload: ParticipantJoinUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Toggle a participant's attendance for the live meeting. Like the rest
    of this router, host-only enforcement is left to the frontend (`canManage`)
    rather than a server-side role check, matching start/pause/end/agenda/etc."""
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    result = await db.execute(
        select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.user_id == user_id,
        )
    )
    participant = result.scalar_one_or_none()
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")

    participant.joined_at = datetime.now(timezone.utc) if payload.joined else None
    await db.commit()
    await db.refresh(participant)
    return participant


@router.patch("/{meeting_id}/participants/{user_id}/score", response_model=MeetingParticipantOut)
async def set_participant_score(
    team_id: int,
    meeting_id: int,
    user_id: int,
    payload: ParticipantScoreUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Wrap-up rating: an attendee submits their own score for the meeting.
    Self-only is enforced by the frontend (only your own row's Score button
    is enabled), matching the router's usual convention."""
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    result = await db.execute(
        select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.user_id == user_id,
        )
    )
    participant = result.scalar_one_or_none()
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")

    participant.score = payload.score
    participant.score_note = payload.note
    participant.scored_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(participant)
    return participant


# ─── Personal check-in speaking order (L10 meetings) ─────────────────────────

def _checkin_pool(meeting: Meeting) -> list[MeetingParticipant]:
    """Only joined participants are eligible for the speaking order — matches
    'display a Speaking Order section containing only the joined participants'."""
    return [p for p in meeting.participants if p.joined_at is not None]


async def _checkin_advance(meeting: Meeting, db: AsyncSession, *, skipped: bool) -> None:
    if meeting.checkin_current_participant_id:
        current = next((p for p in meeting.participants if p.id == meeting.checkin_current_participant_id), None)
        if current:
            current.spoken_at = datetime.now(timezone.utc)
            current.skipped = skipped

    remaining = [p for p in _checkin_pool(meeting) if p.spoken_at is None]
    meeting.checkin_current_participant_id = random.choice(remaining).id if remaining else None
    await db.commit()


@router.post("/{meeting_id}/checkin/next", response_model=MeetingOut)
async def checkin_next_speaker(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Marks the currently-picked speaker as done (if any) and randomly picks
    the next speaker from the eligible pool who haven't gone yet. Called once
    with no current pick to start the roulette, and again each time the host
    clicks the highlighted avatar to advance."""
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    await _checkin_advance(meeting, db, skipped=False)
    # Re-select rather than db.refresh(): refresh() doesn't reliably cascade
    # eager-reload nested lazy="selectin" attributes (e.g. participants[*].user)
    # after expire_on_commit, which can crash response serialization.
    return await _get_meeting(team_id, meeting_id, tenant, db)


@router.post("/{meeting_id}/checkin/skip", response_model=MeetingOut)
async def checkin_skip_speaker(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Like checkin/next, but flags the passed-over speaker as `skipped`
    rather than having actually spoken, for a distinct "Skipped" label."""
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    if not meeting.checkin_current_participant_id:
        raise HTTPException(status_code=400, detail="No current speaker to skip.")
    await _checkin_advance(meeting, db, skipped=True)
    return await _get_meeting(team_id, meeting_id, tenant, db)


@router.post("/{meeting_id}/checkin/select", response_model=MeetingOut)
async def checkin_select_speaker(
    team_id: int,
    meeting_id: int,
    payload: SelectSpeakerRequest,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Host manually picks a specific eligible participant as the next
    speaker, bypassing the random roulette. Marks the previous current
    speaker as spoken first, same as checkin/next."""
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)

    if meeting.checkin_current_participant_id:
        current = next((p for p in meeting.participants if p.id == meeting.checkin_current_participant_id), None)
        if current:
            current.spoken_at = datetime.now(timezone.utc)
            current.skipped = False

    target = next(
        (p for p in _checkin_pool(meeting) if p.user_id == payload.user_id and p.spoken_at is None), None
    )
    if not target:
        raise HTTPException(status_code=400, detail="That participant is not eligible to speak.")

    meeting.checkin_current_participant_id = target.id
    await db.commit()
    return await _get_meeting(team_id, meeting_id, tenant, db)


@router.post("/{meeting_id}/checkin/reset", response_model=MeetingOut)
async def checkin_reset(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Clears the speaking-order state so the check-in can be run again."""
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    for p in meeting.participants:
        p.spoken_at = None
        p.skipped = False
    meeting.checkin_current_participant_id = None
    await db.commit()
    return await _get_meeting(team_id, meeting_id, tenant, db)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    await db.delete(meeting)
    await db.commit()


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/start", response_model=MeetingOut)
async def start_meeting(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    if meeting.status not in ("scheduled", "paused"):
        raise HTTPException(status_code=400, detail=f"Cannot start a meeting with status '{meeting.status}'")
    if meeting.status == "scheduled" and not any(p.joined_at for p in meeting.participants):
        raise HTTPException(status_code=400, detail="At least one participant must join before starting the meeting.")
    meeting.status = "ongoing"
    if not meeting.started_at:
        meeting.started_at = datetime.now(timezone.utc)
        if meeting.current_agenda_item_id is None and meeting.agenda_items:
            first_pending = next((a for a in meeting.agenda_items if a.status != "done"), None)
            if first_pending:
                meeting.current_agenda_item_id = first_pending.id
    meeting.paused_at = None
    await db.commit()
    return await _get_meeting(team_id, meeting_id, tenant, db)


@router.post("/{meeting_id}/pause", response_model=MeetingOut)
async def pause_meeting(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    if meeting.status != "ongoing":
        raise HTTPException(status_code=400, detail="Only ongoing meetings can be paused")
    meeting.status = "paused"
    meeting.paused_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/{meeting_id}/resume", response_model=MeetingOut)
async def resume_meeting(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    if meeting.status != "paused":
        raise HTTPException(status_code=400, detail="Only paused meetings can be resumed")
    meeting.status = "ongoing"
    meeting.paused_at = None
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/{meeting_id}/end", response_model=MeetingOut)
async def end_meeting(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    if meeting.status == "completed":
        raise HTTPException(status_code=400, detail="Meeting is already completed")
    meeting.status = "completed"
    meeting.ended_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(meeting)
    return meeting


# ─── Agenda ───────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/agenda", response_model=AgendaItemOut, status_code=status.HTTP_201_CREATED)
async def add_agenda_item(
    team_id: int,
    meeting_id: int,
    payload: AgendaItemCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    item = MeetingAgendaItem(meeting_id=meeting_id, **payload.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.patch("/{meeting_id}/agenda/{item_id}", response_model=AgendaItemOut)
async def update_agenda_item(
    team_id: int,
    meeting_id: int,
    item_id: int,
    payload: AgendaItemUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    result = await db.execute(
        select(MeetingAgendaItem).where(
            MeetingAgendaItem.id == item_id,
            MeetingAgendaItem.meeting_id == meeting_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/{meeting_id}/agenda/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agenda_item(
    team_id: int,
    meeting_id: int,
    item_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    result = await db.execute(
        select(MeetingAgendaItem).where(
            MeetingAgendaItem.id == item_id,
            MeetingAgendaItem.meeting_id == meeting_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")
    await db.delete(item)
    await db.commit()


@router.post("/{meeting_id}/agenda/reorder", response_model=list[AgendaItemOut])
async def reorder_agenda(
    team_id: int,
    meeting_id: int,
    items: list[AgendaReorderItem],
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    order_map = {item.id: item.sort_order for item in items}
    for agenda_item in meeting.agenda_items:
        if agenda_item.id in order_map:
            agenda_item.sort_order = order_map[agenda_item.id]
    await db.commit()
    await db.refresh(meeting)
    return meeting.agenda_items


@router.post("/{meeting_id}/agenda/next", response_model=MeetingOut)
async def advance_agenda(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Marks the current agenda item done (if any) and advances the pointer
    to the next not-done item by sort_order, or null if none remain."""
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)

    if meeting.current_agenda_item_id:
        current = next((a for a in meeting.agenda_items if a.id == meeting.current_agenda_item_id), None)
        if current:
            current.status = "done"

    next_item = next(
        (a for a in meeting.agenda_items if a.status != "done" and a.id != meeting.current_agenda_item_id),
        None,
    )
    meeting.current_agenda_item_id = next_item.id if next_item else None

    await db.commit()
    return await _get_meeting(team_id, meeting_id, tenant, db)


# ─── Notes ────────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def add_note(
    team_id: int,
    meeting_id: int,
    payload: NoteCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    note = MeetingNote(
        meeting_id=meeting_id,
        content=payload.content,
        created_by_id=tenant.user.id,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


@router.patch("/{meeting_id}/notes/{note_id}", response_model=NoteOut)
async def update_note(
    team_id: int,
    meeting_id: int,
    note_id: int,
    payload: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    result = await db.execute(
        select(MeetingNote).where(
            MeetingNote.id == note_id,
            MeetingNote.meeting_id == meeting_id,
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    note.content = payload.content
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/{meeting_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    team_id: int,
    meeting_id: int,
    note_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    result = await db.execute(
        select(MeetingNote).where(
            MeetingNote.id == note_id,
            MeetingNote.meeting_id == meeting_id,
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    await db.delete(note)
    await db.commit()


# ─── Decisions ────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/decisions", response_model=DecisionOut, status_code=status.HTTP_201_CREATED)
async def add_decision(
    team_id: int,
    meeting_id: int,
    payload: DecisionCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    decision = MeetingDecision(
        meeting_id=meeting_id,
        content=payload.content,
        author_id=tenant.user.id,
    )
    db.add(decision)
    await db.commit()
    await db.refresh(decision)
    return decision


@router.delete("/{meeting_id}/decisions/{decision_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_decision(
    team_id: int,
    meeting_id: int,
    decision_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(team_id, meeting_id, tenant, db)
    result = await db.execute(
        select(MeetingDecision).where(
            MeetingDecision.id == decision_id,
            MeetingDecision.meeting_id == meeting_id,
        )
    )
    decision = result.scalar_one_or_none()
    if not decision:
        raise HTTPException(status_code=404, detail="Decision not found")
    await db.delete(decision)
    await db.commit()


# ─── Tasks ────────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/tasks", response_model=MeetingTaskOut, status_code=status.HTTP_201_CREATED)
async def create_meeting_task(
    team_id: int,
    meeting_id: int,
    payload: MeetingCreateTask,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    from datetime import date as dt_date
    await _get_meeting(team_id, meeting_id, tenant, db)

    due_date = None
    if payload.due_date:
        try:
            due_date = dt_date.fromisoformat(payload.due_date)
        except ValueError:
            pass

    task = Task(
        name=payload.name,
        assignee_id=payload.assignee_id,
        due_date=due_date,
        priority=payload.priority,
        team_id=team_id,
        organization_id=tenant.organization_id,
        created_by_id=tenant.user.id,
        status="todo",
    )
    db.add(task)
    await db.flush()

    meeting_task = MeetingTask(
        meeting_id=meeting_id,
        task_id=task.id,
        agenda_item_id=payload.agenda_item_id,
    )
    db.add(meeting_task)
    await db.commit()
    await db.refresh(meeting_task)
    return meeting_task


# ─── Summary ──────────────────────────────────────────────────────────────────

@router.get("/{meeting_id}/summary", response_model=MeetingSummaryOut)
async def get_meeting_summary(
    team_id: int,
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(team_id, meeting_id, tenant, db)
    return MeetingSummaryOut(
        total_duration_minutes=meeting.duration_minutes,
        completed_agenda=[a for a in meeting.agenda_items if a.status == "done"],
        pending_agenda=[a for a in meeting.agenda_items if a.status != "done"],
        decisions=meeting.decisions,
        action_items=meeting.meeting_tasks,
        notes_count=len(meeting.notes),
    )
