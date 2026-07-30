import time

from fastapi import Depends, Request
from fastapi.security import HTTPBearer

from app.core.auth_errors import TokenError
from app.core.security import decode_access_token
from app.core.token_cache import TokenCache, get_token_cache


class AccessTokenBearer(HTTPBearer):
    """Dependency that validates the Bearer token and checks the JTI blacklist.

    Returns the decoded JWT payload so callers can access jti/exp/sub.
    """

    def __init__(self, auto_error: bool = True):
        super().__init__(auto_error=auto_error)

    async def __call__(  # type: ignore[override]
        self,
        request: Request,
        token_cache: TokenCache = Depends(get_token_cache),
    ) -> dict:
        creds = await super().__call__(request)

        if not creds or creds.scheme.lower() != "bearer":
            raise TokenError.invalid()

        payload = decode_access_token(creds.credentials)

        if not payload:
            raise TokenError.invalid()

        exp = payload.get("exp")
        if exp and int(exp) < int(time.time()):
            raise TokenError.expired()

        jti = payload.get("jti")
        if not jti:
            raise TokenError.invalid("Token is missing a unique identifier.")

        if await token_cache.is_access_token_blacklisted(jti):
            raise TokenError.invalid("Token has been revoked.")

        return payload
