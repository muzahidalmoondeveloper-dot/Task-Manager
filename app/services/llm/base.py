import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, TypeVar

logger = logging.getLogger("llm.base")

T = TypeVar("T")


@dataclass
class LLMResponse:
    text: str
    raw: dict | None = field(default=None)


class LLMProvider(ABC):
    @abstractmethod
    async def generate_text(
        self,
        *,
        system_prompt: str | None = None,
        user_prompt: str,
        temperature: float = 0.1,
        model: str | None = None,
        response_format: Literal["text", "json"] = "text",
        json_schema: dict | None = None,
        capability: str | None = None,
    ) -> LLMResponse:
        """Generate text from the LLM.

        json_schema is used by providers that support structured output natively
        (e.g. Ollama format= parameter). Providers that don't support it ignore it
        and rely on the system/user prompt instructing JSON output.

        capability (observability only — never affects routing/generation):
        a short label naming WHY this call is being made (e.g.
        "intent_detection", "planner", "general_response") — passed through
        unchanged so the concrete provider adapter can log
        provider+model+capability for every outbound request without
        logging any prompt content or secrets. Callers should always pass
        this; it defaults to None (logged as "unspecified") only so
        existing/test callers that predate this parameter don't break.
        """

    async def embed(self, texts: list[str], *, capability: str | None = None) -> list[list[float]]:
        """Embedding capability (architecture item 1 — Knowledge/RAG vector
        search), provider-agnostic: base-class default raises
        NotImplementedError so callers (knowledge_tools.py) can detect
        "this provider has no embedding capability configured" and degrade
        gracefully to keyword-only search, exactly the same honest-degrade
        pattern already used for pgvector's absence — never silently return
        fake/zero vectors that would corrupt cosine-similarity ranking.
        Providers that DO support embeddings (OllamaProvider, today) override
        this. `capability` is observability-only, see generate_text()."""
        raise NotImplementedError(f"{type(self).__name__} does not support embeddings")

    async def generate_json(
        self,
        *,
        system_prompt: str | None = None,
        user_prompt: str,
        temperature: float = 0.0,
        model: str | None = None,
        json_schema: dict | None = None,
        capability: str | None = None,
    ) -> dict:
        """Structured-output entry point (architecture Section 5/37) with a
        base-class default: one call, and — if the response isn't
        parseable JSON — one repair retry with the bad output echoed back.
        LLMGateway overrides this with a richer version that also retries
        against a fallback provider; this default is what every other
        provider (including test doubles) gets for free, so `generate_json`
        is always available regardless of which concrete provider a caller
        holds, not just when it happens to be wrapped in a Gateway.

        json_schema is forwarded to generate_text() unchanged — providers
        that support native structured output (Ollama's `format=`) use it
        to constrain generation; providers that don't simply ignore it, same
        as generate_text()'s own contract. `capability` is observability-
        only, see generate_text()."""
        from app.services.llm.json_utils import parse_llm_json

        prompt = user_prompt
        last_error: Exception | None = None
        for attempt in range(2):
            response = await self.generate_text(
                system_prompt=system_prompt, user_prompt=prompt,
                temperature=temperature, model=model, response_format="json",
                json_schema=json_schema, capability=capability,
            )
            try:
                return parse_llm_json(response.text)
            except (ValueError, TypeError) as exc:
                last_error = exc
                logger.info(
                    "generate_json: invalid JSON on attempt %d, raw=%r", attempt, response.text[:1000],
                )
                if attempt == 0:
                    prompt = (
                        f"{user_prompt}\n\n--- REPAIR REQUEST ---\n"
                        "Your previous response was not valid JSON and could not be parsed. "
                        f"Your previous (invalid) output was:\n{response.text[:1000]}\n\n"
                        "Respond again with ONLY valid JSON — no markdown code fences, no "
                        "commentary before or after, no trailing commas."
                    )
        from app.services.llm.gateway import LLMSchemaError

        raise LLMSchemaError(f"Provider never returned parseable JSON after a repair retry. Last error: {last_error}")

    async def generate_structured(
        self,
        *,
        system_prompt: str | None = None,
        user_prompt: str,
        schema: type[T],
        temperature: float = 0.0,
        model: str | None = None,
        capability: str | None = None,
    ) -> T:
        """Like generate_json(), but also validates against a Pydantic
        `schema` — base-class default (one repair retry), overridden by
        LLMGateway with a fallback-aware version. See its docstring for why
        this matters (architecture item 4/5): a response that's valid JSON
        but doesn't satisfy the schema is repaired the same way malformed
        JSON is, instead of silently reaching application code with a value
        outside the known/expected set. `capability` is observability-only,
        see generate_text()."""
        from pydantic import ValidationError

        from app.services.llm.json_utils import parse_llm_json

        prompt = user_prompt
        last_error: Exception | None = None
        for attempt in range(2):
            response = await self.generate_text(
                system_prompt=system_prompt, user_prompt=prompt,
                temperature=temperature, model=model, response_format="json",
                capability=capability,
            )
            try:
                data = parse_llm_json(response.text)
                return schema.model_validate(data)
            except (ValueError, TypeError, ValidationError) as exc:
                last_error = exc
                logger.info(
                    "generate_structured: invalid output on attempt %d (%s), raw=%r", attempt, exc, response.text[:1000],
                )
                if attempt == 0:
                    prompt = (
                        f"{user_prompt}\n\n--- REPAIR REQUEST ---\n"
                        "Your previous response did not match the required JSON schema. "
                        f"Your previous (invalid) output was:\n{response.text[:1000]}\n\n"
                        "Respond again with ONLY valid JSON — no markdown code fences, no "
                        "commentary before or after, no trailing commas."
                    )
        from app.services.llm.gateway import LLMSchemaError

        raise LLMSchemaError(f"Provider never returned schema-valid output after a repair retry. Last error: {last_error}")
