"""Gmail API adapter (Automation Pipeline Audit follow-up, Phase 2).

Mirrors MicrosoftGraphService's shape deliberately — same
`ensure_fresh_token`/token-refresh pattern, same "return provider-native
dicts, let automation_tasks.py normalize them" responsibility split — so
the two adapters plug into the exact same shared pipeline
(`app.services.automation_tasks.sync_*_data_for_user` ->
`IntegrationRepository.upsert_imported_email` ->
`analyze_pending_sources_for_user`) without a parallel business-logic
path. Gmail has no Teams-equivalent meeting/transcript surface, so this
adapter only ever produces `ImportedEmail` rows.
"""

import base64
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationAccount
from app.services.integrations.oauth import calculate_expires_at, refresh_google_token

logger = logging.getLogger("integrations.gmail")

GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1"

_REFRESH_BUFFER = timedelta(minutes=5)

_MESSAGE_LIST_PAGE_SIZE = 25
# Bounded per sync — matches Microsoft's existing $top=25 per-account cap
# (see MicrosoftGraphService.list_messages). A mailbox with more than
# this many new messages in one incremental window simply gets the rest
# on the next sync tick rather than this call growing unbounded.
_MAX_MESSAGES_PER_SYNC = 25


class GmailService:
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

        token_data = await refresh_google_token(self.account.refresh_token)
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
        """Returns normalized-ish message dicts already shaped like
        Microsoft Graph's message objects (`id`, `subject`, `from`,
        `toRecipients`, `receivedDateTime`, `bodyPreview`, `body`) —
        deliberately, so IntegrationRepository.upsert_imported_email() and
        analyze_pending_sources_for_user() need no Gmail-specific branch
        at all. The actual Gmail API shape (`payload.headers`, base64url
        body, `internalDate` epoch-ms) is translated here, at the
        adapter boundary, exactly where a provider adapter should own
        provider-specific shape differences."""
        # Gmail's `q` search operates on whole days in the account's own
        # interpretation of "after:"/"before:" (Unix seconds) — matches
        # the existing Microsoft adapter's date-range fetch semantics
        # closely enough for the incremental-sync window this is used
        # for (see automation_tasks.sync_gmail_data_for_user).
        query = f"after:{int(start_at.timestamp())} before:{int(end_at.timestamp())}"

        async with httpx.AsyncClient(timeout=60) as client:
            list_response = await client.get(
                f"{GMAIL_BASE_URL}/users/me/messages",
                headers=self.headers,
                params={"q": query, "maxResults": _MESSAGE_LIST_PAGE_SIZE},
            )
            list_response.raise_for_status()
            message_ids = [m["id"] for m in (list_response.json().get("messages") or [])][:_MAX_MESSAGES_PER_SYNC]

            messages: list[dict] = []
            for message_id in message_ids:
                detail_response = await client.get(
                    f"{GMAIL_BASE_URL}/users/me/messages/{message_id}",
                    headers=self.headers,
                    params={"format": "full"},
                )
                detail_response.raise_for_status()
                messages.append(self._normalize_message(detail_response.json()))

        return messages

    def _normalize_message(self, raw: dict) -> dict:
        payload = raw.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in (payload.get("headers") or []) if h.get("name")}

        received_at = None
        internal_date = raw.get("internalDate")
        if internal_date:
            try:
                received_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc).isoformat()
            except (ValueError, OverflowError):
                received_at = None

        body_text = self._extract_body_text(payload)

        return {
            "id": raw["id"],
            "subject": headers.get("subject"),
            "from": {"emailAddress": {"address": self._extract_email_address(headers.get("from"))}},
            "toRecipients": [
                {"emailAddress": {"address": addr}}
                for addr in self._split_addresses(headers.get("to"))
            ],
            "receivedDateTime": received_at,
            "bodyPreview": raw.get("snippet"),
            "body": {"content": body_text},
        }

    @staticmethod
    def _extract_email_address(header_value: str | None) -> str | None:
        if not header_value:
            return None
        # "Display Name <addr@example.com>" or bare "addr@example.com".
        if "<" in header_value and ">" in header_value:
            return header_value.split("<", 1)[1].split(">", 1)[0].strip() or None
        return header_value.strip() or None

    @classmethod
    def _split_addresses(cls, header_value: str | None) -> list[str]:
        if not header_value:
            return []
        return [addr for addr in (cls._extract_email_address(part) for part in header_value.split(",")) if addr]

    @staticmethod
    def _extract_body_text(payload: dict) -> str | None:
        """Prefers text/plain; falls back to a naive-stripped text/html
        part. Gmail's MIME tree can nest multipart/alternative inside
        multipart/mixed (attachments) — this walks one level of `parts`
        recursively rather than assuming a flat structure."""
        def _decode(data: str | None) -> str | None:
            if not data:
                return None
            try:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
            except (ValueError, UnicodeDecodeError):
                return None

        def _walk(node: dict) -> dict[str, str]:
            found: dict[str, str] = {}
            mime_type = node.get("mimeType", "")
            body_data = (node.get("body") or {}).get("data")
            if mime_type in ("text/plain", "text/html") and body_data:
                decoded = _decode(body_data)
                if decoded:
                    found.setdefault(mime_type, decoded)
            for part in node.get("parts") or []:
                child = _walk(part)
                for k, v in child.items():
                    found.setdefault(k, v)
            return found

        parts = _walk(payload)
        if "text/plain" in parts:
            return parts["text/plain"]
        if "text/html" in parts:
            import re
            return re.sub(r"<[^>]+>", " ", parts["text/html"])
        return None
