import logging
from app.services.llm.base import LLMProvider

logger = logging.getLogger("llm.factory")

_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = _create_provider()
    return _provider


def _create_provider() -> LLMProvider:
    from app.core.config import settings

    name = (settings.LLM_PROVIDER or "ollama").lower()
    default_model = settings.DEFAULT_MODEL or None

    logger.info("Initialising LLM provider: %s", name)

    if name == "openai":
        from app.services.llm.openai_provider import OpenAIProvider

        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return OpenAIProvider(
            api_key=settings.OPENAI_API_KEY,
            default_model=default_model or "gpt-4o-mini",
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
