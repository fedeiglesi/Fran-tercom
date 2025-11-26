from typing import Any, Dict, Optional
import logging

from fran.observability import CircuitBreaker, InstrumentedClient


class LLMClient(InstrumentedClient):
    def __init__(self, openai_client: Any, logger: Optional[logging.Logger] = None, breaker: Optional[CircuitBreaker] = None):
        super().__init__(logger=logger, breaker=breaker)
        self.openai_client = openai_client

    def completion(self, **kwargs):
        return self.run("llm.completion", self.openai_client.chat.completions.create, **kwargs)


class HttpClient(InstrumentedClient):
    def __init__(
        self,
        timeout: float,
        headers: Optional[Dict[str, str]] = None,
        logger: Optional[logging.Logger] = None,
        breaker: Optional[CircuitBreaker] = None,
    ):
        super().__init__(logger=logger, breaker=breaker)
        self.timeout = timeout
        self.headers = headers or {}

    def get(self, session, url: str):
        return self.run(
            "http.get",
            session.get,
            url,
            timeout=self.timeout,
            headers=self.headers,
        )
