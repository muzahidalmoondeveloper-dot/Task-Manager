from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationAccount
from app.services.integrations.oauth import calculate_expires_at, refresh_microsoft_token


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

_REFRESH_BUFFER = timedelta(minutes=5)


class MicrosoftGraphService:
    def __init__(self, account: IntegrationAccount):
        self.account = account

    async def ensure_fresh_token(self, db: AsyncSession) -> None:
        if not self.account.refresh_token:
            return

        expires_at = self.account.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        needs_refresh = expires_at is None or expires_at <= datetime.now(timezone.utc) + _REFRESH_BUFFER

        if not needs_refresh:
            return

        token_data = await refresh_microsoft_token(self.account.refresh_token)

        self.account.access_token = token_data["access_token"]
        if "refresh_token" in token_data:
            self.account.refresh_token = token_data["refresh_token"]
        self.account.expires_at = calculate_expires_at(token_data.get("expires_in"))

        await db.commit()

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.account.access_token}",
            "Accept": "application/json",
        }

    async def list_messages(self, start_at: datetime, end_at: datetime) -> list[dict]:
        filter_query = (
            f"receivedDateTime ge {start_at.isoformat()} "
            f"and receivedDateTime lt {end_at.isoformat()}"
        )

        url = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages"

        params = {
            "$top": "25",
            "$orderby": "receivedDateTime desc",
            "$filter": filter_query,
            "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body",
        }

    

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()

        return data.get("value", [])

    async def list_calendar_events(self, start_at: datetime, end_at: datetime) -> list[dict]:
        url = f"{GRAPH_BASE_URL}/me/calendarView"

        params = {
            "startDateTime": start_at.isoformat(),
            "endDateTime": end_at.isoformat(),
            "$top": "50",
            "$select": (
                "id,subject,organizer,attendees,start,end,"
                "onlineMeeting,onlineMeetingUrl,webLink"
            ),
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()

        return data.get("value", [])
        
    async def find_online_meeting_by_join_url(self, join_url: str) -> dict | None:
        url = f"{GRAPH_BASE_URL}/me/onlineMeetings"

        escaped_join_url = join_url.replace("'", "''")

        params = {
            "$filter": f"joinWebUrl eq '{escaped_join_url}'",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=self.headers, params=params)

            if response.status_code >= 400:
                print("find_online_meeting_by_join_url failed")
                print("Status:", response.status_code)
                print("Body:", response.text)

            response.raise_for_status()
            data = response.json()

        values = data.get("value", [])
        return values[0] if values else None

    async def list_transcripts_for_online_meeting(self, online_meeting_id: str) -> list[dict]:
        encoded_id = quote(online_meeting_id, safe="")
        url = f"{GRAPH_BASE_URL}/me/onlineMeetings/{encoded_id}/transcripts"

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()

        return data.get("value", [])

    async def get_transcript_content(
        self,
        online_meeting_id: str,
        transcript_id: str,
    ) -> str:
        encoded_meeting_id = quote(online_meeting_id, safe="")
        encoded_transcript_id = quote(transcript_id, safe="")

        url = (
            f"{GRAPH_BASE_URL}/me/onlineMeetings/"
            f"{encoded_meeting_id}/transcripts/{encoded_transcript_id}/content"
        )

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                url,
                headers={
                    **self.headers,
                    "Accept": "text/vtt",
                },
            )
            response.raise_for_status()

        return response.text