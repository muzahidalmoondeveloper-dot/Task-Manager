import logging
from app.services.llm.base import LLMProvider

logger = logging.getLogger("llm.factory")

_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = _build_gateway()
    return _provider


def _build_gateway() -> LLMProvider:
    """Assembles the primary provider and, if LLM_FALLBACK_PROVIDER is set,
    wraps both in the circuit-breaker Gateway (spec Section 37). Both names
    come only from environment variables — never from a request, a user
    setting, or any admin-facing control — per the requirement that model
    configuration stays developer-only."""
    from app.core.config import settings

    primary = _create_provider(settings.LLM_PROVIDER, settings.DEFAULT_MODEL)

    fallback_name = (settings.LLM_FALLBACK_PROVIDER or "").strip().lower()
    if not fallback_name:
        return primary

    if fallback_name == (settings.LLM_PROVIDER or "ollama").lower():
        logger.warning("LLM_FALLBACK_PROVIDER is the same as LLM_PROVIDER — ignoring fallback")
        return primary

    try:
        fallback = _create_provider(fallback_name, settings.FALLBACK_MODEL)
    except Exception as exc:
        logger.warning(
            "Could not initialise fallback LLM provider %s (%s) — running without fallback",
            fallback_name, exc,
        )
        return primary

    from app.services.llm.gateway import LLMGateway

    logger.info("LLM Gateway active: primary=%s fallback=%s", settings.LLM_PROVIDER, fallback_name)
    return LLMGateway(primary, fallback)


def _create_provider(name: str, default_model: str | None) -> LLMProvider:
    from app.core.config import settings

    name = (name or "ollama").lower()
    default_model = default_model or None

    logger.info("Initialising LLM provider: %s", name)

    if name == "openai":
        from app.services.llm.openai_provider import OpenAIProvider

        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return OpenAIProvider(
            api_key=settings.OPENAI_API_KEY,
            default_model=default_model or "gpt-4o-mini",
        )

    if name == "anthropic":
        from app.services.llm.anthropic_provider import AnthropicProvider

        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        return AnthropicProvider(
            api_key=settings.ANTHROPIC_API_KEY,
            default_model=default_model or "claude-sonnet-5",
        )

    if name == "gemini":
        from app.services.llm.gemini_provider import GeminiProvider

        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        return GeminiProvider(
            api_key=settings.GEMINI_API_KEY,
            default_model=default_model or "gemini-1.5-flash",
        )

    if name == "generic":
        from app.services.llm.generic_provider import GenericCloudModelProvider

        if not settings.GENERIC_LLM_API_URL:
            raise RuntimeError("GENERIC_LLM_API_URL is required when LLM_PROVIDER=generic")
        return GenericCloudModelProvider(
            api_url=settings.GENERIC_LLM_API_URL,
            api_key=settings.GENERIC_LLM_API_KEY,
            default_model=default_model or "default",
        )

    # default: ollama
    from app.services.llm.ollama_provider import OllamaProvider

    return OllamaProvider(
        host=settings.OLLAMA_HOST,
        api_key=settings.OLLAMA_API_KEY,
        default_model=default_model or settings.OLLAMA_MODEL,
    )
