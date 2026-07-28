from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AuthError, TokenError
from app.core.database import get_db
from app.core.security import decode_access_token
from app.core.token_cache import TokenCache, get_token_cache
from app.models.user import User
from app.repositories.user_repository import UserRepository

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
) -> User:
    if credentials is None:
        raise TokenError.invalid("Authentication credentials were not provided.")

    payload = decode_access_token(credentials.credentials)

    if payload is None:
        raise TokenError.invalid()

    jti = payload.get("jti")
    if jti and await token_cache.is_access_token_blacklisted(jti):
        raise TokenError.invalid("Token has been revoked.")

    user_id_raw = payload.get("sub")
    if user_id_raw is None:
        raise TokenError.invalid("Token is missing a subject claim.")

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        raise TokenError.invalid()

    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(user_id)

    if user is None:
        raise AuthError.user_not_found()

    if not user.is_active:
        raise AuthError.account_inactive()

    return user
