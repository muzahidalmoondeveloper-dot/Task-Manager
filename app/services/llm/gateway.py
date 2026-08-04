"""Provider-agnostic LLM Gateway (spec Section 37, bounded) — wraps the
configured primary provider with a simple circuit breaker and an optional
fallback provider. Which providers/models are used is decided entirely by
environment variables (LLM_PROVIDER, LLM_FALLBACK_PROVIDER, *_MODEL) at
process start — there is no runtime or admin-facing knob to change the
active model, per the requirement that model configuration stays
developer-only."""

import logging
import time

from app.services.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger("llm.gateway")

_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 60


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
                logger.warning("Primary LLM provider failed (%s) — falling back", exc)
        else:
            logger.info("LLM circuit breaker open — routing straight to fallback provider")

        return await self._fallback.generate_text(**kwargs)
