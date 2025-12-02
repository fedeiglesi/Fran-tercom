"""Abstracción del cliente LLM para Fran 4.0."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from fran_v4 import config


class LLMService:
    """Wrapper del cliente OpenAI en modo asíncrono."""

    def __init__(self, model: Optional[str] = None) -> None:
        api_key = config.OPENAI_API_KEY
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model or config.MODEL_NAME

    async def chat(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=float(kwargs.get("temperature", 0.7)),
            max_tokens=int(kwargs.get("max_tokens", 400)),
        )
        return response.choices[0].message.content or ""
