from datetime import datetime
from typing import Literal
from pydantic import BaseModel


class ChatMessageRead(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionRead(BaseModel):
    id: int
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionWithMessages(ChatSessionRead):
    messages: list[ChatMessageRead] = []


class PageContext(BaseModel):
    """Validated UI/page context (architecture item 9) — what the user was
    actually looking at when they sent this message, e.g. a task's detail
    page. A closed set of page_type values (model_config extra="forbid" on
    the request itself keeps this from becoming an arbitrary free-text
    channel) lets deictic references ("mark this done", "who's on this
    team") resolve deterministically to the record on screen instead of
    falling through to fuzzy name matching or an ambiguity prompt — but only
    when the reference is genuinely deictic; an explicitly named entity in
    the message always takes precedence over page context (see
    chat_service.py's _resolve_deictic_entity_id)."""

    page_type: Literal["task", "project", "team", "rock", "issue", "meeting", "client_request", "other"]
    entity_id: int | None = None


class ChatMessageRequest(BaseModel):
    message: str
    session_id: int | None = None
    page_context: PageContext | None = None


class ChatAction(BaseModel):
    type: str
    label: str
    payload: dict = {}


class ChatMessageResponse(BaseModel):
    session_id: int
    user_message: ChatMessageRead
    assistant_message: ChatMessageRead
    actions: list[ChatAction] = []


class ApprovalRequestRead(BaseModel):
    id: int
    session_id: int
    change_set_id: int
    requested_by_id: int
    approver_role: str
    reason: str
    status: str
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}
