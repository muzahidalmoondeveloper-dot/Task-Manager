import logging

from ollama import AsyncClient

from app.services.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger("llm.ollama")


class OllamaProvider(LLMProvider):
    PROVIDER_NAME = "ollama"

    def __init__(
        self,
        host: str,
        api_key: str | None = None,
        default_model: str = "gpt-oss:120b",
        embed_model: str = "nomic-embed-text",
    ):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = AsyncClient(host=host, headers=headers)
        self._default_model = default_model
        self._embed_model = embed_model

    async def embed(self, texts: list[str], *, capability: str | None = None) -> list[list[float]]:
        """Architecture item 1 — real embeddings via Ollama's /api/embed,
        confirmed working in this environment with the `nomic-embed-text`
        model (274MB, pulled specifically for this — the chat models
        gpt-oss/qwen2 don't serve embeddings unless the server is started
        with --embeddings, which this deployment doesn't control)."""
        if not texts:
            return []
        logger.info(
            "LLM call | provider=%s model=%s capability=%s op=embed count=%d",
            self.PROVIDER_NAME, self._embed_model, capability or "unspecified", len(texts),
        )
        response = await self._client.embed(model=self._embed_model, input=texts)
        embeddings = response.get("embeddings") if isinstance(response, dict) else getattr(response, "embeddings", None)
        if not embeddings:
            raise RuntimeError("Ollama /api/embed returned no embeddings")
        return [list(vec) for vec in embeddings]

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
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        format_arg = None
        if response_format == "json":
            format_arg = json_schema if json_schema else "json"

        resolved_model = model or self._default_model
        # Observability only — no secrets, no prompt/response content
        # logged, just which provider+model actually served this call and
        # why it was made (capability). API key is only ever sent as a
        # request header to Ollama's own client above, never logged here.
        logger.info(
            "LLM call | provider=%s model=%s capability=%s response_format=%s",
            self.PROVIDER_NAME, resolved_model, capability or "unspecified", response_format,
        )

        response = await self._client.chat(
            model=resolved_model,
            messages=messages,
            format=format_arg,
            options={"temperature": temperature},
        )

        text = response.get("message", {}).get("content", "")
        return LLMResponse(text=text, raw=dict(response))
