import httpx

from app.services.llm.base import LLMProvider, LLMResponse


class OpenAIProvider(LLMProvider):
    BASE_URL = "https://api.openai.com/v1"

    def __init__(self, api_key: str, default_model: str = "gpt-4o-mini"):
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

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        text = data["choices"][0]["message"]["content"]
        return LLMResponse(text=text, raw=data)
