import uuid

from sqlalchemy.ext.asyncio import AsyncSession


class TenantRepository:
    """Base class for all organization-scoped repositories.

    Every subclass receives the active organization_id at construction time.
    This makes it structurally impossible to forget the org filter — every
    query method simply uses ``self.org_id``.
    """

    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        self.db = db
        self.org_id = org_id
