"""LLM service abstraction for Fran 4.0."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI


class LLMService:
    """Thin wrapper around the OpenAI async client."""

    def __init__(self, model: Optional[str] = None) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for Fran 4.0")
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model or os.getenv("MODEL_NAME", "gpt-4o-mini")

    async def chat(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=float(kwargs.get("temperature", 0.7)),
            max_tokens=int(kwargs.get("max_tokens", 400)),
        )
        return response.choices[0].message.content or ""
