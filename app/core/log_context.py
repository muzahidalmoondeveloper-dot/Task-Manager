"""Per-request logging context for the chatbot.

The chat service handles many overlapping conversations concurrently, and a
plain `%(asctime)s | %(levelname)s | %(message)s` format makes it impossible
to tell, from the terminal, which log lines belong to which chat turn once
more than one request is in flight — every existing `logger.info(...)` call
in chat_service.py (and its copilot/ submodules) just interleaves.

This uses `contextvars` (not thread-locals — the app is async, and multiple
requests share the event loop) so a trace/session/user/org id set once at the
top of `ChatService.handle_message()` is automatically attached to *every*
log line emitted while handling that turn — including from helper methods,
`app.services.copilot.*`, and fire-and-forget `asyncio.create_task(...)` work
(contextvars are snapshotted into a task at creation time, so background
title-generation/topic-update calls still carry the right trace id even
though they keep running after the HTTP response is sent).

Usage:
    from app.core.log_context import set_chat_context

    set_chat_context(trace_id=trace_id, user_id=user.id, org_id=org_id)
    ...
    set_chat_context(session_id=session.id)  # fill in once known

No call site elsewhere has to change — `ChatContextFilter` (registered once
on the root handler in main.py) reads these vars and stamps them onto every
`LogRecord`, defaulting to "-" for log lines emitted outside a chat turn.
"""

from __future__ import annotations

import contextvars
import logging

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("chat_trace_id", default=None)
_session_id: contextvars.ContextVar[object] = contextvars.ContextVar("chat_session_id", default=None)
_user_id: contextvars.ContextVar[object] = contextvars.ContextVar("chat_user_id", default=None)
_org_id: contextvars.ContextVar[object] = contextvars.ContextVar("chat_org_id", default=None)


def set_chat_context(
    *,
    trace_id: str | None = None,
    session_id: object = None,
    user_id: object = None,
    org_id: object = None,
) -> None:
    """Set whichever fields are known so far — call again later in the same
    turn (e.g. once the session is created) to fill in the rest."""
    if trace_id is not None:
        _trace_id.set(trace_id)
    if session_id is not None:
        _session_id.set(session_id)
    if user_id is not None:
        _user_id.set(user_id)
    if org_id is not None:
        _org_id.set(org_id)


def clear_chat_context() -> None:
    _trace_id.set(None)
    _session_id.set(None)
    _user_id.set(None)
    _org_id.set(None)


class ChatContextFilter(logging.Filter):
    """Attach the current chat context (if any) to every LogRecord so the
    format string in main.py can print it. Safe to register on the root
    handler — logs from unrelated code paths just get "-" placeholders."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get() or "-"
        record.chat_session_id = _session_id.get() or "-"
        record.chat_user_id = _user_id.get() or "-"
        record.chat_org_id = _org_id.get() or "-"
        return True
