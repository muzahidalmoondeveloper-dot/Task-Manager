from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from itsdangerous import URLSafeSerializer

from app.core.config import settings


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MICROSOFT_USERINFO_URL = "https://graph.microsoft.com/v1.0/me"


async def refresh_microsoft_token(refresh_token: str) -> dict:
    token_url = MICROSOFT_TOKEN_URL.format(
        tenant=settings.MICROSOFT_TENANT_ID or "common"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            token_url,
            data={
                "client_id": settings.MICROSOFT_CLIENT_ID,
                "client_secret": settings.MICROSOFT_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": settings.MICROSOFT_REDIRECT_URI,
                "scope": " ".join(settings.MICROSOFT_SCOPES),
            },
        )

        if response.status_code >= 400:
            print("Microsoft token refresh failed")
            print("Status:", response.status_code)
            print("Body:", response.text)

        response.raise_for_status()
        return response.json()


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.JWT_SECRET_KEY, salt="integration-oauth")


def create_state(user_id: int, provider: str, org_id: str | None = None) -> str:
    data: dict = {"user_id": user_id, "provider": provider}
    if org_id:
        data["org_id"] = org_id
    return _serializer().dumps(data)


def read_state(state: str) -> dict:
    return _serializer().loads(state)


def google_auth_url(user_id: int, org_id: str | None = None) -> str:
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(settings.GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": create_state(user_id, "google", org_id=org_id),
    }

    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def microsoft_auth_url(user_id: int, org_id: str | None = None) -> str:
    params = {
        "client_id": settings.MICROSOFT_CLIENT_ID,
        "redirect_uri": settings.MICROSOFT_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(settings.MICROSOFT_SCOPES),
        "response_mode": "query",
        "state": create_state(user_id, "microsoft", org_id=org_id),
    }

    auth_url = MICROSOFT_AUTH_URL.format(
        tenant=settings.MICROSOFT_TENANT_ID or "common"
    )

    return f"{auth_url}?{urlencode(params)}"


async def exchange_google_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        return response.json()


async def exchange_microsoft_code(code: str) -> dict:
    token_url = MICROSOFT_TOKEN_URL.format(
        tenant=settings.MICROSOFT_TENANT_ID or "common"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            token_url,
            data={
                "code": code,
                "client_id": settings.MICROSOFT_CLIENT_ID,
                "client_secret": settings.MICROSOFT_CLIENT_SECRET,
                "redirect_uri": settings.MICROSOFT_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        return response.json()


async def get_google_profile(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


async def get_microsoft_profile(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            MICROSOFT_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


def calculate_expires_at(expires_in: int | None) -> datetime | None:
    if not expires_in:
        return None

    return datetime.now(timezone.utc) + timedelta(seconds=expires_in)