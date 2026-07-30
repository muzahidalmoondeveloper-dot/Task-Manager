from ollama import AsyncClient

from app.services.llm.base import LLMProvider, LLMResponse


class OllamaProvider(LLMProvider):
    def __init__(self, host: str, api_key: str | None = None, default_model: str = "gpt-oss:120b"):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = AsyncClient(host=host, headers=headers)
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

        format_arg = None
        if response_format == "json":
            format_arg = json_schema if json_schema else "json"

        response = await self._client.chat(
            model=model or self._default_model,
            messages=messages,
            format=format_arg,
            options={"temperature": temperature},
        )

        text = response.get("message", {}).get("content", "")
        return LLMResponse(text=text, raw=dict(response))
