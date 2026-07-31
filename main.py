from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import engine, Base
from app.core.exception_handlers import register_exception_handlers
from app.core.redis_client import close_redis, get_redis
from app.api.routes import auth, users, teams, projects, tasks, integrations, task_suggestions
from app.api.routes import chat, notifications
from app.api.routes import organization  # noqa: F401
from app.api.routes import organizations
from app.api.routes import team_news
from app.api.routes import rocks
from app.api.routes import kpi
from app.api.routes import issues
from app.api.routes import meetings
from app.api.routes import risks
from app.api.routes import reports
from app.api.routes import notes
from app.api.routes import project_invitations
from app.api.routes import task_requests
from app.api.routes import scoreboard
from app.api.routes import team_scoreboard
from app.api.routes import organization_scoreboard
from app.api.routes import billing
import app.models.issue  # noqa: F401  — register Issue
import app.models.meeting  # noqa: F401  — register Meeting models
import app.models.chat  # noqa: F401  — register models for auto table creation
import app.models.email_notification_log  # noqa: F401  — register EmailNotificationLog
import app.models.org_value  # noqa: F401  — register OrgValue
import app.models.objective  # noqa: F401  — register Objective
import app.models.org_role  # noqa: F401  — register OrgRole
import app.models.team_news  # noqa: F401  — register TeamNews
import app.models.rock  # noqa: F401  — register Rock, Milestone
import app.models.kpi  # noqa: F401  — register KPI, KPIEntry
import app.models.refresh_token  # noqa: F401  — register RefreshToken
import app.models.organization  # noqa: F401  — register Organization, OrganizationMembership, OrganizationInvitation, Subscription
import app.models.risk  # noqa: F401  — register Risk
import app.models.report  # noqa: F401  — register Report and all report snapshot/theme/branding tables
import app.models.note  # noqa: F401  — register Note
import app.models.task_request  # noqa: F401  — register TaskRequest
from contextlib import asynccontextmanager
import logging
from app.services.automation_scheduler import start_scheduler, stop_scheduler
from seed_admin import seed_admin

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()  # warm up Redis connection pool on startup

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "ALTER TABLE imported_emails ADD COLUMN IF NOT EXISTS tasks_extracted BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_transcripts ADD COLUMN IF NOT EXISTS tasks_extracted BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await conn.execute(text(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority VARCHAR(20) NOT NULL DEFAULT 'medium'"
        ))
        await conn.execute(text(
            "ALTER TABLE rocks ADD COLUMN IF NOT EXISTS icon VARCHAR(200)"
        ))
        await conn.execute(text(
            "ALTER TABLE rocks ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await conn.execute(text(
            "ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS forecast DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE kpi_entries ADD COLUMN IF NOT EXISTS notes JSONB DEFAULT '[]'"
        ))
        await conn.execute(text(
            "ALTER TABLE kpis ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE kpi_entries ALTER COLUMN value DROP NOT NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE rocks ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE kpis ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE issues ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS planned_start_date DATE"
        ))
        await conn.execute(text(
            "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS planned_end_date DATE"
        ))
        await conn.execute(text(
            "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS actual_start_date DATE"
        ))
        await conn.execute(text(
            "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS actual_end_date DATE"
        ))
        await conn.execute(text(
            "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS forecast_end_date DATE"
        ))
        await conn.execute(text(
            "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS description TEXT"
        ))
        await conn.execute(text(
            "ALTER TABLE issues ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'open'"
        ))
        await conn.execute(text(
            "ALTER TABLE issues ADD COLUMN IF NOT EXISTS resolution_plan TEXT"
        ))
        await conn.execute(text(
            "ALTER TABLE issues ADD COLUMN IF NOT EXISTS target_resolution_date DATE"
        ))
        await conn.execute(text(
            "ALTER TABLE issues ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ"
        ))
        await conn.execute(text(
            "ALTER TABLE organization_invitations ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE"
        ))
        await conn.execute(text(
            "ALTER TABLE reports ADD COLUMN IF NOT EXISTS employee_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE reports ADD COLUMN IF NOT EXISTS team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE reports ADD COLUMN IF NOT EXISTS performance_snapshot JSONB"
        ))
        await conn.execute(text(
            "ALTER TABLE org_values ALTER COLUMN icon TYPE VARCHAR(200)"
        ))
        await conn.execute(text(
            "ALTER TABLE issues ADD COLUMN IF NOT EXISTS icon VARCHAR(200)"
        ))
        await conn.execute(text(
            "ALTER TABLE team_news ADD COLUMN IF NOT EXISTS icon VARCHAR(200)"
        ))
        await conn.execute(text(
            "ALTER TABLE objectives ADD COLUMN IF NOT EXISTS icon VARCHAR(200)"
        ))
        await conn.execute(text(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS icon VARCHAR(200)"
        ))
        await conn.execute(text(
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS scoreboard_completion_weight DOUBLE PRECISION NOT NULL DEFAULT 0.35"
        ))
        await conn.execute(text(
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS scoreboard_on_time_weight DOUBLE PRECISION NOT NULL DEFAULT 0.40"
        ))
        await conn.execute(text(
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS scoreboard_overdue_weight DOUBLE PRECISION NOT NULL DEFAULT 0.25"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_participants ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_participants ADD COLUMN IF NOT EXISTS spoken_at TIMESTAMPTZ"
        ))
        await conn.execute(text(
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS checkin_current_participant_id INTEGER "
            "REFERENCES meeting_participants(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_participants ADD COLUMN IF NOT EXISTS selected BOOLEAN NOT NULL DEFAULT TRUE"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_participants ADD COLUMN IF NOT EXISTS skipped BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await conn.execute(text(
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS current_agenda_item_id INTEGER "
            "REFERENCES meeting_agenda_items(id) ON DELETE SET NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_participants ADD COLUMN IF NOT EXISTS score DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_participants ADD COLUMN IF NOT EXISTS score_note TEXT"
        ))
        await conn.execute(text(
            "ALTER TABLE meeting_participants ADD COLUMN IF NOT EXISTS scored_at TIMESTAMPTZ"
        ))
        await conn.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS billing_interval VARCHAR(20) NOT NULL DEFAULT 'monthly'"
        ))
        await conn.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await conn.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS extra_teams INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS extra_users INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_extra_teams_item_id VARCHAR(255)"
        ))
        await conn.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS stripe_extra_users_item_id VARCHAR(255)"
        ))
        # Pre-launch remap: free/professional/enterprise tiers are retired in
        # favor of just Starter/Business — no live paying customers are
        # affected by this rewrite. free -> starter (lowest paid tier);
        # professional/enterprise -> business (highest remaining tier, since
        # both were higher-capacity tiers than "starter").
        await conn.execute(text(
            "UPDATE organizations SET plan = 'starter' WHERE plan = 'free'"
        ))
        await conn.execute(text(
            "UPDATE subscriptions SET plan = 'starter' WHERE plan = 'free'"
        ))
        await conn.execute(text(
            "UPDATE organizations SET plan = 'business' WHERE plan IN ('professional', 'enterprise')"
        ))
        await conn.execute(text(
            "UPDATE subscriptions SET plan = 'business' WHERE plan IN ('professional', 'enterprise')"
        ))
        await conn.execute(text(
            "ALTER TABLE organization_memberships ADD COLUMN IF NOT EXISTS is_org_admin BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await conn.execute(text(
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS logo_url VARCHAR(500)"
        ))
        await conn.execute(text(
            "ALTER TABLE organization_memberships ADD COLUMN IF NOT EXISTS is_team_manager BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        await conn.execute(text(
            "ALTER TABLE organization_memberships ADD COLUMN IF NOT EXISTS is_project_manager BOOLEAN NOT NULL DEFAULT FALSE"
        ))

    await seed_admin()

    start_scheduler()

    yield

    stop_scheduler()

    await close_redis()
    await engine.dispose()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
    docs_url=f"{settings.API_PREFIX}/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(users.router, prefix=settings.API_PREFIX)
app.include_router(teams.router, prefix=settings.API_PREFIX)
app.include_router(projects.router, prefix=settings.API_PREFIX)
app.include_router(tasks.router, prefix=settings.API_PREFIX)
app.include_router(integrations.router, prefix=settings.API_PREFIX)
app.include_router(task_suggestions.router, prefix=settings.API_PREFIX)
app.include_router(chat.router, prefix=settings.API_PREFIX)
app.include_router(notifications.router, prefix=settings.API_PREFIX)
app.include_router(organization.router, prefix=settings.API_PREFIX)
app.include_router(team_news.router, prefix=settings.API_PREFIX)
app.include_router(rocks.router, prefix=settings.API_PREFIX)
app.include_router(kpi.router, prefix=settings.API_PREFIX)
app.include_router(issues.router, prefix=settings.API_PREFIX)
app.include_router(meetings.router, prefix=settings.API_PREFIX)
app.include_router(organizations.router, prefix=settings.API_PREFIX)
app.include_router(risks.router, prefix=settings.API_PREFIX)
app.include_router(reports.router, prefix=settings.API_PREFIX)
app.include_router(notes.router, prefix=settings.API_PREFIX)
app.include_router(project_invitations.router, prefix=settings.API_PREFIX)
app.include_router(task_requests.router, prefix=settings.API_PREFIX)
app.include_router(scoreboard.router, prefix=settings.API_PREFIX)
app.include_router(team_scoreboard.router, prefix=settings.API_PREFIX)
app.include_router(organization_scoreboard.router, prefix=settings.API_PREFIX)
app.include_router(billing.router, prefix=settings.API_PREFIX)

@app.get("/health")
async def health_check():
    return {"status": "ok"}


settings.media_root_path.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.media_root_path), name="media")


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}")
    async def serve_react_app(full_path: str):
        if full_path.startswith("api") or full_path in {"health", "docs", "openapi.json"}:
            return {"detail": "Not Found"}
        index_file = FRONTEND_DIST / "index.html"
        return FileResponse(index_file)
else:
    @app.get("/")
    async def root():
        return {"message": "Welcome to the Automated Task Manager API"}
