
from datetime import datetime

from pydantic import BaseModel


class IntegrationAccountRead(BaseModel):
    id: int
    provider: str
    account_email: str
    scopes: list[str] | None
    is_active: bool
    created_at: datetime

    model_config = {
        "from_attributes": True,
    }