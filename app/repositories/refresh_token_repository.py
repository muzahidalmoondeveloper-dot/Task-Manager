from datetime import datetime, timezone

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken


class RefreshTokenRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def save(self, token_hash: str, user_id: int, expires_at: datetime) -> RefreshToken:
        token = RefreshToken(
            token_hash=token_hash,
            user_id=user_id,
            expires_at=expires_at,
            is_revoked=False,
        )
        self.db.add(token)
        await self.db.flush()
        return token

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self.db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def revoke(self, token_hash: str) -> bool:
        token = await self.get_by_hash(token_hash)
        if not token:
            return False
        token.is_revoked = True
        token.revoked_at = datetime.now(timezone.utc)
        await self.db.flush()
        return True

    async def revoke_all_for_user(self, user_id: int) -> int:
        result = await self.db.execute(
            update(RefreshToken)
            .where(
                and_(
                    RefreshToken.user_id == user_id,
                    RefreshToken.is_revoked.is_(False),
                )
            )
            .values(is_revoked=True, revoked_at=datetime.now(timezone.utc))
        )
        await self.db.flush()
        return result.rowcount or 0
