from datetime import datetime

from pydantic import BaseModel, Field, field_validator

ENTITY_TYPES = {"rock", "kpi", "issue", "news"}


class NoteCreate(BaseModel):
    text: str = Field(min_length=1)


class NoteOut(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    text: str
    author_id: int | None = None
    author_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


def validate_entity_type(entity_type: str) -> str:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of: {', '.join(sorted(ENTITY_TYPES))}")
    return entity_type
