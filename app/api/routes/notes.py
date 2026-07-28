from fastapi import APIRouter, Depends
from fastapi import status as http_status
from sqlalchemy import select

from app.core.auth_errors import AppException, ErrorDef
from app.core.tenant import TenantContext, get_tenant_context
from app.models.issue import Issue
from app.models.kpi import KPI
from app.models.rock import Rock
from app.models.team_news import TeamNews
from app.repositories.note_repository import NoteRepository
from app.schemas.note import NoteCreate, NoteOut

router = APIRouter(prefix="/notes", tags=["Notes"])

_INVALID_ENTITY_TYPE = ErrorDef(code="NOTE_INVALID_ENTITY_TYPE", status=http_status.HTTP_400_BAD_REQUEST, message="Invalid entity type.")
_ENTITY_NOT_FOUND = ErrorDef(code="NOTE_ENTITY_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Item not found.")
_NOTE_NOT_FOUND = ErrorDef(code="NOTE_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Note not found.")
_NOTE_FORBIDDEN = ErrorDef(code="NOTE_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You can only delete your own notes.")

ENTITY_MODELS = {
    "rock": Rock,
    "kpi": KPI,
    "issue": Issue,
    "news": TeamNews,
}


def _serialize(note) -> NoteOut:
    author = note.author
    return NoteOut(
        id=note.id,
        entity_type=note.entity_type,
        entity_id=note.entity_id,
        text=note.text,
        author_id=note.author_id,
        author_name=(author.full_name or author.email) if author else None,
        created_at=note.created_at,
    )


async def _require_entity(tenant: TenantContext, entity_type: str, entity_id: int) -> None:
    model = ENTITY_MODELS.get(entity_type)
    if model is None:
        raise AppException(_INVALID_ENTITY_TYPE)

    stmt = select(model.id).where(model.id == entity_id, model.organization_id == tenant.organization_id)
    result = await tenant.db.execute(stmt)
    if result.scalar_one_or_none() is None:
        raise AppException(_ENTITY_NOT_FOUND)


@router.get("/counts/{entity_type}", response_model=dict[int, int])
async def get_note_counts(entity_type: str, entity_ids: str, tenant: TenantContext = Depends(get_tenant_context)):
    """Bulk note counts for a list of entities, e.g. `?entity_ids=1,2,3` — used to
    render a notes badge in list views without an N+1 fetch per row."""
    if entity_type not in ENTITY_MODELS:
        raise AppException(_INVALID_ENTITY_TYPE)
    ids = [int(x) for x in entity_ids.split(",") if x.strip().isdigit()]
    repo = NoteRepository(tenant.db, tenant.organization_id)
    return await repo.count_for_entities(entity_type, ids)


@router.get("/{entity_type}/{entity_id}", response_model=list[NoteOut])
async def list_notes(entity_type: str, entity_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    await _require_entity(tenant, entity_type, entity_id)
    repo = NoteRepository(tenant.db, tenant.organization_id)
    return [_serialize(n) for n in await repo.list_for_entity(entity_type, entity_id)]


@router.post("/{entity_type}/{entity_id}", response_model=NoteOut, status_code=http_status.HTTP_201_CREATED)
async def create_note(
    entity_type: str,
    entity_id: int,
    payload: NoteCreate,
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _require_entity(tenant, entity_type, entity_id)
    repo = NoteRepository(tenant.db, tenant.organization_id)
    note = await repo.create(entity_type, entity_id, tenant.user.id, payload.text)
    return _serialize(note)


@router.delete("/{entity_type}/{entity_id}/{note_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_note(
    entity_type: str,
    entity_id: int,
    note_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _require_entity(tenant, entity_type, entity_id)
    repo = NoteRepository(tenant.db, tenant.organization_id)
    note = await repo.get_by_id(note_id)
    if note is None or note.entity_type != entity_type or note.entity_id != entity_id:
        raise AppException(_NOTE_NOT_FOUND)
    if not tenant.is_admin_or_owner and note.author_id != tenant.user.id:
        raise AppException(_NOTE_FORBIDDEN)
    await repo.delete(note)
    return None
