"""Provider-agnostic LLM Gateway (spec Section 37, bounded) — wraps the
configured primary provider with a simple circuit breaker and an optional
fallback provider. Which providers/models are used is decided entirely by
environment variables (LLM_PROVIDER, LLM_FALLBACK_PROVIDER, *_MODEL) at
process start — there is no runtime or admin-facing knob to change the
active model, per the requirement that model configuration stays
developer-only."""

import json
import logging
import time
from typing import Callable, TypeVar

from pydantic import ValidationError

from app.services.llm.base import LLMProvider, LLMResponse
from app.services.llm.json_utils import parse_llm_json

T = TypeVar("T")

logger = logging.getLogger("llm.gateway")

_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 60
_MAX_REPAIR_ATTEMPTS = 1  # one repair retry per provider, per architecture Section 5/37


class LLMSchemaError(Exception):
    """Raised by LLMGateway.generate_json() when no configured provider —
    including one repair retry per provider — ever produced parseable JSON.
    Callers must catch this and return a graceful, typed user-facing
    message; it must never propagate as an unhandled 500 (architecture
    Section 5: "Structured LLM gateway validation, repair, retry, and
    fallback for schema-invalid responses")."""


def _build_repair_prompt(original_user_prompt: str, invalid_output: str, reason: str) -> str:
    return (
        f"{original_user_prompt}\n\n"
        "--- REPAIR REQUEST ---\n"
        f"Your previous response {reason}. "
        f"Your previous (invalid) output was:\n{invalid_output[:1000]}\n\n"
        "Respond again with ONLY valid JSON — no markdown code fences, no "
        "commentary before or after, no trailing commas."
    )


class CircuitBreaker:
    """CLOSED (normal) -> OPEN (primary skipped for cooldown) -> HALF-OPEN
    (cooldown elapsed, one trial call allowed) -> CLOSED on success."""

    def __init__(self, failure_threshold: int = _FAILURE_THRESHOLD, cooldown_seconds: int = _COOLDOWN_SECONDS):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown_seconds:
            return False  # half-open: let the next call through as a trial
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_at = time.monotonic()


class LLMGateway(LLMProvider):
    """Wraps a primary provider; if it fails (or its circuit is open) and a
    fallback provider is configured, retries once against the fallback."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider | None = None):
        self._primary = primary
        self._fallback = fallback
        self._breaker = CircuitBreaker()

    async def generate_text(self, **kwargs) -> LLMResponse:
        # capability flows through **kwargs unchanged to whichever provider
        # actually ends up serving the call (see LLMProvider.generate_text()'s
        # docstring) — pulled out here only for the routing-decision log
        # lines below, never stripped from kwargs itself.
        capability = kwargs.get("capability") or "unspecified"

        if self._fallback is None:
            # No fallback configured — the circuit breaker has nothing to
            # switch to, so always call the primary directly rather than
            # raising outright (that would take down every chat message).
            return await self._primary.generate_text(**kwargs)

        if not self._breaker.is_open:
            try:
                response = await self._primary.generate_text(**kwargs)
                self._breaker.record_success()
                return response
            except Exception as exc:
                self._breaker.record_failure()
                logger.warning("Primary LLM provider failed (capability=%s): %s — falling back", capability, exc)
        else:
            logger.info("LLM circuit breaker open (capability=%s) — routing straight to fallback provider", capability)

        return await self._fallback.generate_text(**kwargs)

    async def embed(self, texts: list[str], *, capability: str | None = None) -> list[list[float]]:
        """Delegates to the primary provider's embed() (architecture item 1),
        falling back to the fallback provider only if the primary raises
        NotImplementedError (no embedding capability) — a provider simply
        not supporting embeddings is not a circuit-breaker-tracked failure,
        so this bypasses the breaker entirely, same reasoning as
        generate_json()'s schema-invalid-response handling below.
        `capability` is observability-only, see LLMProvider.generate_text()."""
        try:
            return await self._primary.embed(texts, capability=capability)
        except NotImplementedError:
            if self._fallback is not None:
                return await self._fallback.embed(texts, capability=capability)
            raise

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
        """Structured-output entry point (architecture Section 5/37):
        schema-invalid (unparseable) responses are repaired via one retry
        with the model's own bad output echoed back, then — if still
        invalid — the same original+repair sequence is tried against the
        fallback provider (if configured), before finally raising
        LLMSchemaError. This is a *distinct* failure path from
        generate_text()'s circuit breaker: a provider that responds
        successfully but with malformed JSON has not "failed" in the
        exception sense the breaker tracks, so every call here is still
        attempted against the primary regardless of breaker state — a
        schema-invalid response is not evidence the provider is down.
        `capability` is observability-only, see LLMProvider.generate_text().
        """
        return await self._generate_validated(
            system_prompt=system_prompt, user_prompt=user_prompt, temperature=temperature, model=model,
            validate=parse_llm_json, invalid_reason="was not valid JSON and could not be parsed",
            json_schema=json_schema, capability=capability,
        )

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
        """Like generate_json(), but also validates the parsed JSON against
        a Pydantic `schema` (architecture item 4/5 — replacing the fragile
        untyped-dict + string-equality intent/sub-intent dispatch with a
        closed, validated contract). A response that's valid JSON but
        doesn't match the schema (wrong intent value, missing field, wrong
        type) is repaired exactly like malformed JSON is — the repair
        prompt includes the specific validation errors, not just "invalid
        JSON" — instead of silently reaching chat_service.py's if/elif
        chain with a garbage value that falls through to _handle_general
        with zero signal anything went wrong. `capability` is observability-
        only, see LLMProvider.generate_text().
        """
        def _validate(text: str) -> T:
            data = parse_llm_json(text)
            try:
                return schema.model_validate(data)
            except ValidationError as exc:
                # Re-raised as ValueError so _generate_validated's single
                # except clause covers both "not JSON" and "valid JSON, but
                # doesn't match the schema" uniformly.
                raise ValueError(f"did not match the required schema: {exc.errors()}") from exc

        return await self._generate_validated(
            system_prompt=system_prompt, user_prompt=user_prompt, temperature=temperature, model=model,
            validate=_validate, invalid_reason="did not match the required JSON schema", capability=capability,
        )

    async def _generate_validated(
        self,
        *,
        system_prompt: str | None,
        user_prompt: str,
        temperature: float,
        model: str | None,
        validate: Callable[[str], T],
        invalid_reason: str,
        json_schema: dict | None = None,
        capability: str | None = None,
    ) -> T:
        providers: list[tuple[str, LLMProvider]] = [("primary", self._primary)]
        if self._fallback is not None:
            providers.append(("fallback", self._fallback))

        last_error: Exception | None = None
        for label, provider in providers:
            prompt = user_prompt
            for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
                try:
                    response = await provider.generate_text(
                        system_prompt=system_prompt, user_prompt=prompt,
                        temperature=temperature, model=model, response_format="json",
                        json_schema=json_schema, capability=capability,
                    )
                except Exception as exc:
                    last_error = exc
                    if label == "primary":
                        self._breaker.record_failure()
                    logger.warning(
                        "generate_structured: %s provider raised on attempt %d (capability=%s): %s",
                        label, attempt, capability or "unspecified", exc,
                    )
                    break  # provider itself errored — move to the next provider, not a repair retry

                try:
                    result = validate(response.text)
                except (json.JSONDecodeError, ValueError) as exc:
                    last_error = exc
                    logger.info(
                        "generate_structured: %s provider output invalid on attempt %d (%s), raw=%r",
                        label, attempt, exc, response.text[:1000],
                    )
                    if attempt < _MAX_REPAIR_ATTEMPTS:
                        prompt = _build_repair_prompt(user_prompt, response.text, invalid_reason)
                        continue
                    break  # repair budget exhausted for this provider — move to the next provider

                if label == "primary":
                    self._breaker.record_success()
                return result

        raise LLMSchemaError(
            f"No LLM provider returned output satisfying the requirement after retries/fallback. Last error: {last_error}"
        )
