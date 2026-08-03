import httpx

from app.services.llm.base import LLMProvider, LLMResponse

_JSON_INSTRUCTION = (
    "\n\nRespond with ONLY valid JSON — no markdown code fences, no commentary, "
    "no text before or after the JSON object."
)


class AnthropicProvider(LLMProvider):
    BASE_URL = "https://api.anthropic.com/v1"
    ANTHROPIC_VERSION = "2023-06-01"
    MAX_OUTPUT_TOKENS = 4096

    def __init__(self, api_key: str, default_model: str = "claude-sonnet-5"):
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
        # Anthropic's Messages API has no native JSON-mode toggle (unlike
        # OpenAI's response_format / Gemini's responseMimeType) — the
        # documented workaround is a strong system-prompt instruction, then
        # the caller's own _parse_json_safe repairs stray formatting.
        effective_system = system_prompt or ""
        if response_format == "json":
            effective_system = (effective_system or "") + _JSON_INSTRUCTION

        payload: dict = {
            "model": model or self._default_model,
            "max_tokens": self.MAX_OUTPUT_TOKENS,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if effective_system:
            payload["system"] = effective_system

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{self.BASE_URL}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": self.ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        return LLMResponse(text=text, raw=data)
