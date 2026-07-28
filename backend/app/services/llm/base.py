from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


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
    ) -> LLMResponse:
        """Generate text from the LLM.

        json_schema is used by providers that support structured output natively
        (e.g. Ollama format= parameter). Providers that don't support it ignore it
        and rely on the system/user prompt instructing JSON output.
        """
