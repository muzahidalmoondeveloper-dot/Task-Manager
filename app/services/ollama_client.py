from ollama import AsyncClient

from app.core.config import settings


class OllamaCloudClient:
    def __init__(self):
        headers = {}

        if settings.OLLAMA_API_KEY:
            headers["Authorization"] = f"Bearer {settings.OLLAMA_API_KEY}"

        self.client = AsyncClient(
            host=settings.OLLAMA_HOST,
            headers=headers,
        )

    async def chat(
        self,
        *,
        messages: list[dict],
        model: str | None = None,
        stream: bool = False,
        format: str | dict | None = None,
        options: dict | None = None,
    ) -> dict:
        return await self.client.chat(
            model=model or settings.OLLAMA_MODEL,
            messages=messages,
            stream=stream,
            format=format,
            options=options or {"temperature": 0.1},
        )