import logging

import httpx

from app.services.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger("llm.gemini")


class GeminiProvider(LLMProvider):
    PROVIDER_NAME = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, default_model: str = "gemini-1.5-flash"):
        self._api_key = api_key
        self._default_model = default_model

    async def generate_text(
        self,
        *,
        system_prompt=None,
        user_prompt,
        temperature=0.1,
        model=None,
        response_format="text",
        json_schema=None,
        capability=None,
    ) -> LLMResponse:
        generation_config: dict = {"temperature": temperature}

        if response_format == "json":
            generation_config["responseMimeType"] = "application/json"

        payload: dict = {
            "contents": [{"parts": [{"text": user_prompt}], "role": "user"}],
            "generationConfig": generation_config,
        }

        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        resolved_model = model or self._default_model

        # Observability only — no secrets (the API key is sent only as a
        # query param on the actual request below, never logged), no
        # prompt/response content.
        logger.info(
            "LLM call | provider=%s model=%s capability=%s response_format=%s",
            self.PROVIDER_NAME, resolved_model, capability or "unspecified", response_format,
        )

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{self.BASE_URL}/models/{resolved_model}:generateContent",
                params={"key": self._api_key},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return LLMResponse(text=text, raw=data)
