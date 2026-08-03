import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIChangeSet(Base):
    """A frozen, previewable set of pending changes for a higher-risk chat
    write action — nothing is applied to the domain tables until the user
    confirms this exact row (see app/services/copilot/change_sets.py)."""
    __tablename__ = "ai_change_sets"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # {"<task_id>": "<updated_at isoformat captured at preview time>"} — the
    # optimistic-lock snapshot re-checked before execution.
    captured_versions_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    affected_summary: Mapped[str] = mapped_column(Text, nullable=False)

    # pending | confirmed | executed | cancelled | expired | stale
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIOperation(Base):
    """A completed, reversible write action — holds the inverse action so it
    can be undone within its expiry window (see app/services/copilot/undo.py)."""
    __tablename__ = "ai_operations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    inverse_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    reversible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    undone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AITopic(Base):
    """A rolling, LLM-maintained summary of what a chat session's current
    subject is about — supplements (does not replace) raw message history."""
    __tablename__ = "ai_topics"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    topic_type: Mapped[str] = mapped_column(String(50), nullable=False, default="general", server_default="general")
    linked_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    linked_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)

    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    open_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # active | paused | completed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", server_default="active")
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIApprovalRequest(Base):
    """Multi-level approval (spec Section 47) — created instead of a plain
    change-set when a tool's risk requires a human other than the requester
    to sign off. approver_role decides who may act on it: "team_manager"
    (must additionally manage target_team_id) or "admin" (org admin/owner)."""
    __tablename__ = "ai_approval_requests"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    change_set_id: Mapped[int] = mapped_column(ForeignKey("ai_change_sets.id", ondelete="CASCADE"), nullable=False)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    approver_role: Mapped[str] = mapped_column(String(20), nullable=False)  # team_manager | admin
    target_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # pending | approved | rejected | expired
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AISavedMemory(Base):
    """Saved user memory (spec Section 32.4) — stable, user-confirmed
    preferences only (language, report format, etc.), never current
    operational facts. One row per (user, key)."""
    __tablename__ = "ai_saved_memories"
    __table_args__ = ()

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(nullable=False, default=1.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AIEvaluationResult(Base):
    """Evaluation Framework (spec Section 50, bounded slice) — one row per
    golden-set case run, so regressions in intent/tool selection accuracy
    are visible over time instead of only checked ad hoc."""
    __tablename__ = "ai_evaluation_results"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    suite: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    case_name: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actual_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AIToolExecution(Base):
    """One row per copilot tool call (read or write) — the bounded audit
    trail slice of the spec's full observability suite."""
    __tablename__ = "ai_tool_executions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    tool_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False, default="R0")
    policy_decision: Mapped[str] = mapped_column(String(30), nullable=False, default="allow")
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # One trace_id per chat turn (request) — correlates every tool call
    # (reads and writes) made while handling that single message.
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
