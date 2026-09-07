import random
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.meeting_reactions import list_reactions_since, push_reaction
from app.core.redis_client import get_redis
from app.core.tenant import TenantContext, get_tenant_context
from app.models.issue import Issue
from app.models.notification import Notification
from app.services.background_email import bg_send_meeting_summary
from app.models.meeting import (
    Meeting, MeetingParticipant, MeetingAgendaItem,
    MeetingNote, MeetingDecision, MeetingTask, MeetingTemplate,
)
from app.models.task import Task
from app.repositories.team_repository import TeamRepository
from app.schemas.meeting import (
    MeetingCreate, MeetingUpdate, MeetingOut,
    AgendaItemCreate, AgendaItemUpdate, AgendaItemOut, AgendaReorderItem,
    NoteCreate, NoteUpdate, NoteOut,
    DecisionCreate, DecisionOut,
    MeetingCreateTask, MeetingLinkTask, MeetingTaskOut,
    MeetingSummaryOut,
    MeetingParticipantOut, ParticipantJoinUpdate, ParticipantScoreUpdate, SelectSpeakerRequest,
    MeetingTemplateCreate, MeetingTemplateOut, SuggestedTasksOut,
    MeetingReactionCreate, MeetingReactionOut,
    TranscriptSubmit, TranscriptAnalyzeResult, ExtractedTaskSummary,
)

# Meetings are not team-specific — one flat router, `team_id` is an optional
# field on the meeting (and an optional filter on the list), never part of
# the URL. (Prior to the Smart Meeting System plan this was nested under
# /teams/{team_id}/meetings; every meeting had to belong to exactly one
# team. That nesting is gone — a meeting now optionally belongs to a team.)
router = APIRouter(prefix="/meetings", tags=["meetings"])

# Saved agenda templates (plan: "Save Meeting Template", meeting-type
# defaults) — org-wide, not team-scoped.
template_router = APIRouter(prefix="/meeting-templates", tags=["meetings"])

# The 9 prebuilt meeting types (plan section 5) and their default agendas —
# static lookups, not AI-generated (AI Agenda Suggestions is explicitly
# Phase 2 in the plan). "custom" uses the plan's general Recommended Default
# Agenda (section 14); the rest follow the same shape as the Weekly Team
# Sync example (section 6).
BUILTIN_MEETING_TEMPLATES: dict[str, dict] = {
    "weekly_sync": {
        "name": "Weekly Team Sync", "default_duration_minutes": 60,
        "agenda_sections": [
            {"title": "Wins / Check-in", "duration_minutes": 5},
            {"title": "KPI Review", "duration_minutes": 10},
            {"title": "Pending Tasks", "duration_minutes": 15},
            {"title": "Blockers", "duration_minutes": 15},
            {"title": "Priority Decisions", "duration_minutes": 10},
            {"title": "Next Actions", "duration_minutes": 5},
        ],
    },
    "project_review": {
        "name": "Project Review", "default_duration_minutes": 60,
        "agenda_sections": [
            {"title": "Project Status", "duration_minutes": 10},
            {"title": "Milestones Review", "duration_minutes": 10},
            {"title": "Risks & Blockers", "duration_minutes": 15},
            {"title": "Budget & Timeline", "duration_minutes": 10},
            {"title": "Decisions", "duration_minutes": 10},
            {"title": "Next Steps", "duration_minutes": 5},
        ],
    },
    "sprint_planning": {
        "name": "Sprint Planning", "default_duration_minutes": 60,
        "agenda_sections": [
            {"title": "Sprint Review", "duration_minutes": 10},
            {"title": "Backlog Grooming", "duration_minutes": 15},
            {"title": "Capacity Planning", "duration_minutes": 10},
            {"title": "Task Assignment", "duration_minutes": 15},
            {"title": "Sprint Goals", "duration_minutes": 10},
        ],
    },
    "daily_standup": {
        "name": "Daily Standup", "default_duration_minutes": 15,
        "agenda_sections": [
            {"title": "Yesterday's Progress", "duration_minutes": 5},
            {"title": "Today's Plan", "duration_minutes": 5},
            {"title": "Blockers", "duration_minutes": 5},
        ],
    },
    "one_on_one": {
        "name": "1:1 Meeting", "default_duration_minutes": 30,
        "agenda_sections": [
            {"title": "Check-in", "duration_minutes": 5},
            {"title": "Wins & Challenges", "duration_minutes": 10},
            {"title": "Career / Growth", "duration_minutes": 10},
            {"title": "Feedback", "duration_minutes": 5},
        ],
    },
    "client_meeting": {
        "name": "Client Meeting", "default_duration_minutes": 45,
        "agenda_sections": [
            {"title": "Welcome & Agenda", "duration_minutes": 5},
            {"title": "Project Update", "duration_minutes": 15},
            {"title": "Client Feedback", "duration_minutes": 15},
            {"title": "Next Steps", "duration_minutes": 10},
        ],
    },
    "retrospective": {
        "name": "Retrospective", "default_duration_minutes": 45,
        "agenda_sections": [
            {"title": "What Went Well", "duration_minutes": 10},
            {"title": "What Didn't Go Well", "duration_minutes": 10},
            {"title": "Action Items", "duration_minutes": 15},
            {"title": "Team Health", "duration_minutes": 10},
        ],
    },
    "leadership_review": {
        "name": "Leadership Review", "default_duration_minutes": 75,
        "agenda_sections": [
            {"title": "Company KPIs", "duration_minutes": 15},
            {"title": "Department Updates", "duration_minutes": 20},
            {"title": "Strategic Decisions", "duration_minutes": 15},
            {"title": "Risks", "duration_minutes": 10},
            {"title": "Action Items", "duration_minutes": 10},
        ],
    },
    "custom": {
        "name": "Custom Meeting", "default_duration_minutes": 90,
        "agenda_sections": [
            {"title": "Opening / Check-in", "duration_minutes": 5},
            {"title": "Goal Review", "duration_minutes": 5},
            {"title": "KPI / Progress Review", "duration_minutes": 10},
            {"title": "Pending Tasks Review", "duration_minutes": 15},
            {"title": "Blockers & Risks", "duration_minutes": 15},
            {"title": "Priority Discussion", "duration_minutes": 20},
            {"title": "Decisions", "duration_minutes": 10},
            {"title": "Action Items & Owners", "duration_minutes": 5},
            {"title": "Wrap-up", "duration_minutes": 5},
        ],
    },
}


# ─── Meeting Templates ──────────────────────────────────────────────────────

async def _seed_builtin_templates(db: AsyncSession, org_id) -> None:
    """Bootstrap the 9 prebuilt meeting-type templates the first time an
    org's template list is fetched — same on-first-read seeding pattern
    used by OnboardingTemplateRepository's default-template bootstrap.
    Idempotent: only runs when the org has zero templates."""
    existing = await db.execute(select(MeetingTemplate.id).where(MeetingTemplate.organization_id == org_id).limit(1))
    if existing.scalar_one_or_none() is not None:
        return
    for meeting_type, spec in BUILTIN_MEETING_TEMPLATES.items():
        db.add(MeetingTemplate(
            organization_id=org_id,
            name=spec["name"],
            meeting_type=meeting_type,
            default_duration_minutes=spec["default_duration_minutes"],
            agenda_sections=spec["agenda_sections"],
            is_builtin=True,
        ))
    await db.commit()


@template_router.get("", response_model=list[MeetingTemplateOut])
async def list_meeting_templates(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _seed_builtin_templates(db, tenant.organization_id)
    result = await db.execute(
        select(MeetingTemplate)
        .where(MeetingTemplate.organization_id == tenant.organization_id)
        .order_by(MeetingTemplate.is_builtin.desc(), MeetingTemplate.created_at.asc())
    )
    return result.scalars().all()


@template_router.post("", response_model=MeetingTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_meeting_template(
    payload: MeetingTemplateCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    template = MeetingTemplate(
        organization_id=tenant.organization_id,
        name=payload.name,
        meeting_type=payload.meeting_type,
        description=payload.description,
        default_duration_minutes=payload.default_duration_minutes,
        agenda_sections=[s.model_dump() for s in payload.agenda_sections],
        is_builtin=False,
        created_by_id=tenant.user.id,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


@template_router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(MeetingTemplate).where(
            MeetingTemplate.id == template_id,
            MeetingTemplate.organization_id == tenant.organization_id,
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    if template.is_builtin:
        raise HTTPException(status_code=400, detail="Built-in meeting-type templates can't be deleted.")
    # Creator, or a manager-level role, may remove a saved template.
    if template.created_by_id != tenant.user.id and not tenant.is_manager_or_above:
        raise HTTPException(status_code=403, detail="Only the creator or a manager can delete this template.")
    await db.delete(template)
    await db.commit()


# ─── Suggested tasks/issues (plan section 7 — Linked Task Integration) ────────

@router.get("/suggested-tasks", response_model=SuggestedTasksOut)
async def suggested_tasks(
    team_id: int | None = Query(None),
    project_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Overdue, high-priority, and unresolved-issue items — narrowed to a
    team and/or project when given, otherwise across the whole org (a
    meeting isn't required to belong to a team). Surfaced in the Create
    Meeting agenda builder so relevant work can be dropped straight into
    the agenda ("These 5 tasks are overdue. Add them to the agenda?")."""
    task_q = select(Task).where(
        Task.organization_id == tenant.organization_id,
        Task.status != "done",
    )
    if team_id is not None:
        task_q = task_q.where(Task.team_id == team_id)
    if project_id is not None:
        task_q = task_q.where(Task.project_id == project_id)

    today = date.today()
    overdue_q = task_q.where(Task.due_date.is_not(None), Task.due_date < today).order_by(Task.due_date.asc()).limit(10)
    high_priority_q = task_q.where(Task.priority.in_(["high", "urgent"])).order_by(Task.due_date.asc().nulls_last()).limit(10)

    issue_q = select(Issue).where(
        Issue.organization_id == tenant.organization_id,
        Issue.status.in_(["open", "in_progress"]),
    )
    if team_id is not None:
        issue_q = issue_q.where(Issue.team_id == team_id)
    if project_id is not None:
        issue_q = issue_q.where(Issue.project_id == project_id)
    issue_q = issue_q.order_by(Issue.priority.desc()).limit(10)

    overdue = (await db.execute(overdue_q)).scalars().all()
    high_priority = (await db.execute(high_priority_q)).scalars().all()
    issues = (await db.execute(issue_q)).scalars().all()

    return SuggestedTasksOut(
        overdue=overdue,
        # Avoid double-listing a task that's both overdue and high-priority.
        high_priority=[t for t in high_priority if t.id not in {o.id for o in overdue}],
        unresolved_issues=issues,
    )


async def _title_exists(db: AsyncSession, org_id, title: str, *, exclude_meeting_id: int | None = None) -> bool:
    """Case-insensitive org-wide title collision check, backing both the
    live inline validation on Create Meeting and the create-time guard
    below. `exclude_meeting_id` lets a rename (PATCH) check against every
    *other* meeting without tripping on itself."""
    q = select(Meeting.id).where(
        Meeting.organization_id == org_id,
        func.lower(Meeting.title) == title.strip().lower(),
    )
    if exclude_meeting_id is not None:
        q = q.where(Meeting.id != exclude_meeting_id)
    result = await db.execute(q.limit(1))
    return result.scalar_one_or_none() is not None


@router.get("/check-title")
async def check_meeting_title(
    title: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Backs the inline "This Meeting Name exists in your organization"
    validation on the Create Meeting page — checked as the user types,
    ahead of the same check enforced again (authoritatively) at submit."""
    return {"exists": await _title_exists(db, tenant.organization_id, title)}


# Meetings aren't visible org-wide by default any more — a caller can only
# see/act on a meeting they organize, are an explicitly-added attendee of,
# or (for managers/PMs, who can also create meetings) any meeting at all.
# This is the single source of truth both list_meetings and _get_meeting
# below consult, so "which meetings can I see" and "can I open this one
# directly by id" never drift apart.
def _can_manage_all_meetings(tenant: TenantContext) -> bool:
    return tenant.is_manager_or_above or tenant.has_project_manager_access


async def _is_meeting_participant(db: AsyncSession, meeting_id: int, user_id: int) -> bool:
    result = await db.execute(
        select(MeetingParticipant.id).where(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _get_meeting(meeting_id: int, tenant: TenantContext, db: AsyncSession) -> Meeting:
    result = await db.execute(
        select(Meeting).where(
            Meeting.id == meeting_id,
            Meeting.organization_id == tenant.organization_id,
        )
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if not _can_manage_all_meetings(tenant):
        is_organizer = meeting.organizer_id == tenant.user.id
        if not is_organizer and not await _is_meeting_participant(db, meeting.id, tenant.user.id):
            # Same 404 as "doesn't exist" (not 403) — a plain team member
            # has no business learning that a meeting they weren't invited
            # to even exists, matching this app's usual not-found convention.
            raise HTTPException(status_code=404, detail="Meeting not found")

    return meeting


async def _team_names(db: AsyncSession, team_ids: set) -> dict:
    if not team_ids:
        return {}
    from app.models.team import Team
    result = await db.execute(select(Team.id, Team.name).where(Team.id.in_(team_ids)))
    return {row[0]: row[1] for row in result.all()}


# ─── Meeting-related notifications ─────────────────────────────────────────────
# Three distinct triggers per the access/notifications spec: (1) added as an
# attendee, (2) the scheduled start time arrives (see the scheduler job,
# app.services.automation_scheduler.run_meeting_start_reminders), and
# (3) someone actually starts the meeting. All three write plain
# Notification rows (no meeting_id != None the frontend can special-case
# for a "go to this meeting" click, same pattern as task_id/project_id).

def _notify_added_as_attendee(db: AsyncSession, meeting: Meeting, user_ids: set[int], *, actor_id: int) -> None:
    when = meeting.scheduled_at.strftime("%b %d, %Y at %I:%M %p UTC")
    for user_id in user_ids:
        if user_id is None or user_id == actor_id:
            continue  # the person doing the inviting doesn't need to be told
        db.add(Notification(
            user_id=user_id,
            meeting_id=meeting.id,
            title="Added to a meeting",
            message=f'You were added to "{meeting.title}", scheduled for {when}.',
            type="meeting_invite",
        ))


def _notify_meeting_started(db: AsyncSession, meeting: Meeting, *, actor_id: int) -> None:
    for participant in meeting.participants:
        if participant.user_id is None or participant.user_id == actor_id:
            continue  # the person who started it doesn't need to be told
        db.add(Notification(
            user_id=participant.user_id,
            meeting_id=meeting.id,
            title="Meeting started",
            message=f'"{meeting.title}" has started.',
            type="meeting_started",
        ))


# ─── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[MeetingOut])
async def list_meetings(
    filter: str = Query(None),
    team_id: int | None = Query(None, description="Narrow to one team's meetings (used by that team's own Meetings tab)."),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Meetings this caller can see. Owner/Admin/Team Manager/Project
    Manager (anyone who can also create meetings) see every meeting in the
    org. Everyone else — plain team members and clients — only see meetings
    they organize or have been explicitly added to as an attendee; there is
    no more org-wide-visibility or same-team broadening for them, since
    access is meant to be scoped to exactly the meetings someone was
    assigned to, not their team membership. `team_id` further narrows
    within whichever of those sets the caller can already see."""
    q = select(Meeting).where(Meeting.organization_id == tenant.organization_id)

    if not _can_manage_all_meetings(tenant):
        participant_meeting_ids = select(MeetingParticipant.meeting_id).where(MeetingParticipant.user_id == tenant.user.id)
        q = q.where(or_(
            Meeting.organizer_id == tenant.user.id,
            Meeting.id.in_(participant_meeting_ids),
        ))

    if team_id is not None:
        q = q.where(Meeting.team_id == team_id)

    if filter == "ongoing":
        q = q.where(Meeting.status == "ongoing")
    elif filter == "completed":
        q = q.where(Meeting.status == "completed")
    elif filter == "scheduled":
        q = q.where(Meeting.status == "scheduled")
    q = q.order_by(Meeting.scheduled_at.desc())
    result = await db.execute(q)
    meetings = result.scalars().all()

    team_names = await _team_names(db, {m.team_id for m in meetings if m.team_id is not None})
    return [
        MeetingOut.model_validate(m).model_copy(update={"team_name": team_names.get(m.team_id)})
        for m in meetings
    ]


@router.post("", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
async def create_meeting(
    payload: MeetingCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    # Owner/Admin, Team Manager, or Project Manager (role OR granted flag,
    # same flag-aware idiom used everywhere else in this app) — plain team
    # members and clients can join/view a meeting but not create one.
    if not (tenant.is_manager_or_above or tenant.has_project_manager_access):
        raise HTTPException(status_code=403, detail="You don't have permission to create meetings.")

    if payload.team_id is not None:
        team_repo = TeamRepository(db, tenant.organization_id)
        if await team_repo.get_by_id(payload.team_id) is None:
            raise HTTPException(status_code=404, detail="Team not found")

    # Authoritative re-check (the Create Meeting page also checks live via
    # GET /meetings/check-title as the user types) — this is the one that
    # actually blocks a race between two people typing the same name at once.
    if await _title_exists(db, tenant.organization_id, payload.title):
        raise HTTPException(status_code=409, detail="This Meeting Name exists in your organization. Please select a different name.")

    meeting = Meeting(
        team_id=payload.team_id,
        organization_id=tenant.organization_id,
        title=payload.title,
        description=payload.description,
        objective=payload.objective,
        scheduled_at=payload.scheduled_at,
        duration_minutes=payload.duration_minutes,
        meeting_type=payload.meeting_type,
        priority=payload.priority,
        visibility=payload.visibility,
        recurrence=payload.recurrence,
        location=payload.location,
        project_id=payload.project_id,
        organizer_id=payload.organizer_id or tenant.user.id,
    )
    db.add(meeting)
    await db.flush()

    seen = set()
    for user_id in payload.participant_ids:
        if user_id not in seen:
            db.add(MeetingParticipant(meeting_id=meeting.id, user_id=user_id))
            seen.add(user_id)

    _notify_added_as_attendee(db, meeting, seen, actor_id=tenant.user.id)

    # Agenda Builder — sections assembled in the create form are submitted
    # together with the meeting itself rather than requiring N follow-up
    # calls to POST /agenda.
    for i, item in enumerate(payload.agenda_items):
        db.add(MeetingAgendaItem(
            meeting_id=meeting.id,
            title=item.title,
            duration_minutes=item.duration_minutes,
            sort_order=item.sort_order or i,
            presenter_id=item.presenter_id,
        ))

    # Linked Task Integration — existing tasks the user chose to pull in
    # (e.g. from the suggested overdue/high-priority list) get a MeetingTask
    # row each; no duplicate Task rows are created. Re-scoped to this org so
    # a crafted task_id from elsewhere can't be linked in.
    if payload.linked_task_ids:
        valid_ids = (await db.execute(
            select(Task.id).where(Task.id.in_(payload.linked_task_ids), Task.organization_id == tenant.organization_id)
        )).scalars().all()
        for task_id in set(valid_ids):
            db.add(MeetingTask(meeting_id=meeting.id, task_id=task_id))

    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.get("/{meeting_id}", response_model=MeetingOut)
async def get_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await _get_meeting(meeting_id, tenant, db)


@router.patch("/{meeting_id}", response_model=MeetingOut)
async def update_meeting(
    meeting_id: int,
    payload: MeetingUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    previously_participating = {p.user_id for p in meeting.participants if p.user_id is not None}

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

        # Only whoever is newly added gets the "added to a meeting"
        # notification — re-saving the same attendee list (or a self-join
        # that folds the caller into an unrelated update) never re-notifies
        # anyone already on it.
        _notify_added_as_attendee(db, meeting, seen - previously_participating, actor_id=tenant.user.id)

    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.patch("/{meeting_id}/participants/{user_id}", response_model=MeetingParticipantOut)
async def set_participant_joined(
    meeting_id: int,
    user_id: int,
    payload: ParticipantJoinUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Toggle a participant's attendance for the live meeting. Like the rest
    of this router, host-only enforcement is left to the frontend (`canManage`)
    rather than a server-side role check, matching start/pause/end/agenda/etc."""
    meeting = await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    user_id: int,
    payload: ParticipantScoreUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Wrap-up rating: an attendee submits their own score for the meeting.
    Self-only is enforced by the frontend (only your own row's Score button
    is enabled), matching the router's usual convention."""
    meeting = await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Marks the currently-picked speaker as done (if any) and randomly picks
    the next speaker from the eligible pool who haven't gone yet. Called once
    with no current pick to start the roulette, and again each time the host
    clicks the highlighted avatar to advance."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    await _checkin_advance(meeting, db, skipped=False)
    # Re-select rather than db.refresh(): refresh() doesn't reliably cascade
    # eager-reload nested lazy="selectin" attributes (e.g. participants[*].user)
    # after expire_on_commit, which can crash response serialization.
    return await _get_meeting(meeting_id, tenant, db)


@router.post("/{meeting_id}/checkin/skip", response_model=MeetingOut)
async def checkin_skip_speaker(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Like checkin/next, but flags the passed-over speaker as `skipped`
    rather than having actually spoken, for a distinct "Skipped" label."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    if not meeting.checkin_current_participant_id:
        raise HTTPException(status_code=400, detail="No current speaker to skip.")
    await _checkin_advance(meeting, db, skipped=True)
    return await _get_meeting(meeting_id, tenant, db)


@router.post("/{meeting_id}/checkin/select", response_model=MeetingOut)
async def checkin_select_speaker(
    meeting_id: int,
    payload: SelectSpeakerRequest,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Host manually picks a specific eligible participant as the next
    speaker, bypassing the random roulette. Marks the previous current
    speaker as spoken first, same as checkin/next."""
    meeting = await _get_meeting(meeting_id, tenant, db)

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
    return await _get_meeting(meeting_id, tenant, db)


@router.post("/{meeting_id}/checkin/reset", response_model=MeetingOut)
async def checkin_reset(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Clears the speaking-order state so the check-in can be run again."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    for p in meeting.participants:
        p.spoken_at = None
        p.skipped = False
    meeting.checkin_current_participant_id = None
    await db.commit()
    return await _get_meeting(meeting_id, tenant, db)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    await db.delete(meeting)
    await db.commit()


# ─── Lifecycle ────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/start", response_model=MeetingOut)
async def start_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    if meeting.status not in ("scheduled", "paused"):
        raise HTTPException(status_code=400, detail=f"Cannot start a meeting with status '{meeting.status}'")
    if meeting.status == "scheduled" and not any(p.joined_at for p in meeting.participants):
        # A host should always be able to start solo rather than needing a
        # separate click to check themself in first — auto-join whoever is
        # starting it (adding them as a participant if they weren't one).
        starter = next((p for p in meeting.participants if p.user_id == tenant.user.id), None)
        if starter is None:
            starter = MeetingParticipant(meeting_id=meeting.id, user_id=tenant.user.id)
            db.add(starter)
            await db.flush()
        starter.joined_at = datetime.now(timezone.utc)
    meeting.status = "ongoing"
    is_first_start = meeting.started_at is None
    if is_first_start:
        meeting.started_at = datetime.now(timezone.utc)
        if meeting.current_agenda_item_id is None and meeting.agenda_items:
            first_pending = next((a for a in meeting.agenda_items if a.status != "done"), None)
            if first_pending:
                meeting.current_agenda_item_id = first_pending.id
        # Only the genuine first start notifies — resuming from a pause
        # goes through this same route but `started_at` is already set by
        # then, so attendees aren't re-notified every time the meeting is
        # paused and resumed.
        _notify_meeting_started(db, meeting, actor_id=tenant.user.id)
    meeting.paused_at = None
    await db.commit()
    return await _get_meeting(meeting_id, tenant, db)


@router.post("/{meeting_id}/pause", response_model=MeetingOut)
async def pause_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    if meeting.status != "ongoing":
        raise HTTPException(status_code=400, detail="Only ongoing meetings can be paused")
    meeting.status = "paused"
    meeting.paused_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/{meeting_id}/resume", response_model=MeetingOut)
async def resume_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    if meeting.status != "paused":
        raise HTTPException(status_code=400, detail="Only paused meetings can be resumed")
    meeting.status = "ongoing"
    meeting.paused_at = None
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/{meeting_id}/end", response_model=MeetingOut)
async def end_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    if meeting.status == "completed":
        raise HTTPException(status_code=400, detail="Meeting is already completed")
    meeting.status = "completed"
    meeting.ended_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/{meeting_id}/send-summary", status_code=status.HTTP_202_ACCEPTED)
async def send_meeting_summary(
    meeting_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """"Send email summary" — Conclude section, End Meeting. Emails every
    participant with an email address (recorded decisions + action items);
    fire-and-forget, same pattern as every other transactional email in
    this app (see app.services.background_email)."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    recipient_ids = {p.user_id for p in meeting.participants if p.user_id}
    for recipient_id in recipient_ids:
        background_tasks.add_task(bg_send_meeting_summary, meeting_id, recipient_id)
    return {"queued": len(recipient_ids)}


# ─── Agenda ───────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/agenda", response_model=AgendaItemOut, status_code=status.HTTP_201_CREATED)
async def add_agenda_item(
    meeting_id: int,
    payload: AgendaItemCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
    item = MeetingAgendaItem(meeting_id=meeting_id, **payload.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.patch("/{meeting_id}/agenda/{item_id}", response_model=AgendaItemOut)
async def update_agenda_item(
    meeting_id: int,
    item_id: int,
    payload: AgendaItemUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    item_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    items: list[AgendaReorderItem],
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    order_map = {item.id: item.sort_order for item in items}
    for agenda_item in meeting.agenda_items:
        if agenda_item.id in order_map:
            agenda_item.sort_order = order_map[agenda_item.id]
    await db.commit()
    await db.refresh(meeting)
    return meeting.agenda_items


@router.post("/{meeting_id}/agenda/next", response_model=MeetingOut)
async def advance_agenda(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Marks the current agenda item done (if any) and advances the pointer
    to the next not-done item by sort_order, or null if none remain."""
    meeting = await _get_meeting(meeting_id, tenant, db)

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
    return await _get_meeting(meeting_id, tenant, db)


@router.post("/{meeting_id}/agenda/{item_id}/select", response_model=MeetingOut)
async def select_agenda_item(
    meeting_id: int,
    item_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Jump the live "current section" pointer directly to `item_id` —
    clicking an agenda item in the sidebar, as opposed to `.../agenda/next`
    (the sequential "Next" button), which also marks the outgoing item done.
    Pure navigation: no status changes, can move forward or back freely."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    item = next((a for a in meeting.agenda_items if a.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Agenda item not found")
    meeting.current_agenda_item_id = item.id
    await db.commit()
    return await _get_meeting(meeting_id, tenant, db)


# ─── Live reactions (ephemeral, Redis-backed — see app.core.meeting_reactions) ─

@router.post("/{meeting_id}/reactions", response_model=MeetingReactionOut, status_code=status.HTTP_201_CREATED)
async def send_meeting_reaction(
    meeting_id: int,
    payload: MeetingReactionCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
    redis = await get_redis()
    entry = await push_reaction(redis, meeting_id, payload.emoji, tenant.user.id)
    return entry


@router.get("/{meeting_id}/reactions", response_model=list[MeetingReactionOut])
async def list_meeting_reactions(
    meeting_id: int,
    since: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Polled every ~2s by anyone with this meeting's live panel open,
    alongside the existing GET /meetings/{id} sync poll — `since` is the
    highest reaction id that caller has already rendered."""
    await _get_meeting(meeting_id, tenant, db)
    redis = await get_redis()
    return await list_reactions_since(redis, meeting_id, since)


# ─── Notes ────────────────────────────────────────────────────────────────────

@router.post("/{meeting_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
async def add_note(
    meeting_id: int,
    payload: NoteCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    note_id: int,
    payload: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    note_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    payload: DecisionCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    decision_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _get_meeting(meeting_id, tenant, db)
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
    meeting_id: int,
    payload: MeetingCreateTask,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    from datetime import date as dt_date
    meeting = await _get_meeting(meeting_id, tenant, db)

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
        team_id=meeting.team_id,
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


@router.post("/{meeting_id}/tasks/link", response_model=MeetingTaskOut, status_code=status.HTTP_201_CREATED)
async def link_meeting_task(
    meeting_id: int,
    payload: MeetingLinkTask,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Attach an *existing* task (e.g. a suggested overdue/high-priority
    one) to the meeting — the create-new-task counterpart above stays
    unchanged. 404s if the task isn't in this org, so a meeting can't be
    made to point at another tenant's data."""
    await _get_meeting(meeting_id, tenant, db)

    task_result = await db.execute(
        select(Task.id).where(Task.id == payload.task_id, Task.organization_id == tenant.organization_id)
    )
    if task_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Task not found")

    existing = await db.execute(
        select(MeetingTask).where(MeetingTask.meeting_id == meeting_id, MeetingTask.task_id == payload.task_id)
    )
    meeting_task = existing.scalar_one_or_none()
    if meeting_task is None:
        meeting_task = MeetingTask(meeting_id=meeting_id, task_id=payload.task_id, agenda_item_id=payload.agenda_item_id)
        db.add(meeting_task)
        await db.commit()
        await db.refresh(meeting_task)
    return meeting_task


# ─── Recording / Transcript / AI Task Extraction ───────────────────────────────
# Audio capture + speech-to-text happen entirely client-side (the browser's
# own speech recognition produces the text) — deliberately not routed
# through any specific transcription vendor, so this backend never stores
# or processes audio. The backend's job starts once there's text: store it,
# then hand it to the *existing*, already provider-agnostic AITaskExtractor
# (the same engine that already powers email/meeting-transcript task
# suggestions elsewhere in this app via `get_llm_provider()`) to pull out
# clear action items and create them as real meeting to-dos, reusing the
# exact same Task + MeetingTask creation path as create_meeting_task above.

@router.post("/{meeting_id}/recording/start", response_model=MeetingOut)
async def start_recording(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Purely an informational flag — it just lets every participant's
    meeting sync poll show a "Recording" badge. The actual audio capture is
    started/stopped in the browser regardless of this call's outcome."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    meeting.is_recording = True
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/{meeting_id}/recording/cancel", response_model=MeetingOut)
async def cancel_recording(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Discards an in-progress recording — unlike .../stop, no transcript is
    submitted and no AI extraction runs. The client-side transcript text is
    simply thrown away; this call only clears the "Recording" badge."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    meeting.is_recording = False
    await db.commit()
    await db.refresh(meeting)
    return meeting


async def _extract_and_create_tasks(
    meeting: Meeting, tenant: TenantContext, db: AsyncSession,
) -> list[ExtractedTaskSummary]:
    from app.core.auth_errors import AppException
    from app.core.task_assignment import validate_task_assignee
    from app.repositories.team_repository import TeamRepository
    from app.services.ai_task_extractor import AITaskExtractor
    from app.services.automation_tasks import resolve_user_id
    from app.repositories.user_repository import UserRepository

    if not meeting.transcript_text or not meeting.transcript_text.strip():
        return []

    # SECURITY: org-scoped candidate set only (cross-tenant automation-
    # assignee fix) — NEVER UserRepository.list_all(), which scans every
    # user in the entire database with no organization boundary. Active,
    # non-Client members of THIS organization only.
    user_candidates = await UserRepository(db).list_org_assignable_candidates(tenant.organization_id)
    known_users = [{"name": u.full_name, "email": u.email} for u, _role in user_candidates]

    # Every task this flow creates shares meeting.team_id (there is no
    # per-task team extraction here, unlike the email/transcript flow) —
    # if the meeting is team-scoped, the assignee candidate pool is
    # narrowed to that team's members for the whole meeting (Rule B:
    # a Team Task's assignee must belong to that exact Team).
    if meeting.team_id is not None:
        assignee_pool = await TeamRepository(db, tenant.organization_id).list_assignable_members(meeting.team_id)
    else:
        assignee_pool = [u for u, _role in user_candidates]

    extractor = AITaskExtractor()
    extracted_tasks, _ = await extractor.extract_tasks(
        source_type="meeting_transcript",
        source_title=meeting.title,
        source_text=meeting.transcript_text,
        known_users=known_users,
    )

    # Title-based de-dup against to-dos this meeting already has — guards
    # against re-creating the same action items if recording is stopped
    # and restarted more than once in the same meeting (the client always
    # resubmits the *full* accumulated transcript, not just the new part).
    existing_names = {mt.task.name.strip().lower() for mt in meeting.meeting_tasks if mt.task}

    summaries: list[ExtractedTaskSummary] = []
    for extracted in extracted_tasks:
        title_key = extracted.title.strip().lower()
        if not title_key or title_key in existing_names:
            continue

        assignee_id = resolve_user_id(
            assignee_pool, extracted.suggested_assignee_name, extracted.suggested_assignee_email,
        )

        # Defense-in-depth (PHASE 16): this flow builds `Task` directly
        # (db.add()), bypassing TaskRepository.create()'s own
        # validate_task_assignee() call entirely, so it must re-run the
        # same authoritative check itself before persisting — fails safe
        # to Unassigned rather than ever letting a resolution defect
        # persist a cross-tenant/Client/inactive/non-team assignee.
        if assignee_id is not None:
            try:
                await validate_task_assignee(
                    db, organization_id=tenant.organization_id,
                    assignee_id=assignee_id, team_id=meeting.team_id,
                )
            except AppException:
                assignee_id = None

        assignee = next((u for u in assignee_pool if u.id == assignee_id), None) if assignee_id else None

        task = Task(
            name=extracted.title,
            assignee_id=assignee_id,
            due_date=extracted.suggested_due_date,
            priority="medium",
            team_id=meeting.team_id,
            organization_id=tenant.organization_id,
            created_by_id=tenant.user.id,
            status="todo",
        )
        db.add(task)
        await db.flush()
        db.add(MeetingTask(meeting_id=meeting.id, task_id=task.id))

        existing_names.add(title_key)
        summaries.append(ExtractedTaskSummary(
            title=extracted.title,
            assignee_id=assignee_id,
            assignee_name=assignee.full_name if assignee else None,
            due_date=extracted.suggested_due_date,
        ))

    return summaries


@router.post("/{meeting_id}/recording/stop", response_model=TranscriptAnalyzeResult)
async def stop_recording(
    meeting_id: int,
    payload: TranscriptSubmit,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Stop + transcript + AI analysis + to-do creation as one call — the
    user's action is a single "Stop Recording" click, not a multi-step
    review-then-confirm flow, per the request that detected tasks are
    created automatically."""
    meeting = await _get_meeting(meeting_id, tenant, db)
    meeting.is_recording = False
    meeting.transcript_text = payload.text
    await db.flush()

    tasks_created = await _extract_and_create_tasks(meeting, tenant, db)

    meeting.transcript_analyzed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(meeting)
    return TranscriptAnalyzeResult(meeting=meeting, tasks_created=tasks_created)


# ─── Summary ──────────────────────────────────────────────────────────────────

@router.get("/{meeting_id}/summary", response_model=MeetingSummaryOut)
async def get_meeting_summary(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    meeting = await _get_meeting(meeting_id, tenant, db)
    return MeetingSummaryOut(
        total_duration_minutes=meeting.duration_minutes,
        completed_agenda=[a for a in meeting.agenda_items if a.status == "done"],
        pending_agenda=[a for a in meeting.agenda_items if a.status != "done"],
        decisions=meeting.decisions,
        action_items=meeting.meeting_tasks,
        notes_count=len(meeting.notes),
    )
