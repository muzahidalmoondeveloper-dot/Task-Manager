from datetime import datetime
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


class ChatMessageRequest(BaseModel):
    message: str
    session_id: int | None = None


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
