import httpx

from app.services.llm.base import LLMProvider, LLMResponse


class GenericCloudModelProvider(LLMProvider):
    """OpenAI-compatible generic provider for self-hosted or third-party cloud models."""

    def __init__(self, api_url: str, api_key: str | None = None, default_model: str = "default"):
        self._api_url = api_url.rstrip("/")
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
    ) -> LLMResponse:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: dict = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": temperature,
        }

        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        headers: dict = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{self._api_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        text = data["choices"][0]["message"]["content"]
        return LLMResponse(text=text, raw=data)
