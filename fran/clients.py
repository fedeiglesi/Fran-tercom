"""
Clients for HTTP and LLM interactions with circuit breaker pattern
"""
import time
import logging
from contextlib import contextmanager
from typing import Dict, List, Any, Optional


class CircuitBreaker:
    """
    Simple circuit breaker pattern implementation
    """
    def __init__(self, failure_threshold: int = 5, recovery_time: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failure_count = 0
        self.last_failure_time = None
        self.is_open = False

    def record_failure(self):
        """Record a failure"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.is_open = True

    def record_success(self):
        """Record a success"""
        self.failure_count = 0
        self.is_open = False
        self.last_failure_time = None

    def can_execute(self) -> bool:
        """Check if circuit can execute"""
        if not self.is_open:
            return True

        if self.last_failure_time is None:
            return True

        elapsed = time.time() - self.last_failure_time
        if elapsed >= self.recovery_time:
            self.is_open = False
            self.failure_count = 0
            return True

        return False


class HttpClient:
    """
    HTTP client with circuit breaker support
    """
    def __init__(self, timeout: int = 30, headers: Optional[Dict[str, str]] = None,
                 logger: Optional[logging.Logger] = None, breaker: Optional[CircuitBreaker] = None):
        self.timeout = timeout
        self.headers = headers or {}
        self.logger = logger or logging.getLogger(__name__)
        self.breaker = breaker

    def get(self, requests_list: List[Dict[str, Any]], base_url: str) -> Dict[str, Any]:
        """
        Make HTTP GET request
        """
        if self.breaker and not self.breaker.can_execute():
            if self.logger:
                self.logger.error("Circuit breaker is open")
            return {"error": "Circuit breaker open"}

        try:
            import requests
            response = requests.get(base_url, timeout=self.timeout, headers=self.headers)
            response.raise_for_status()

            if self.breaker:
                self.breaker.record_success()

            return response.json()

        except Exception as e:
            if self.breaker:
                self.breaker.record_failure()
            if self.logger:
                self.logger.error(f"HTTP request failed: {e}")
            return {"error": str(e)}


class LLMClient:
    """
    OpenAI LLM client with circuit breaker support
    """
    def __init__(self, client, logger: Optional[logging.Logger] = None,
                 breaker: Optional[CircuitBreaker] = None):
        self.client = client
        self.logger = logger or logging.getLogger(__name__)
        self.breaker = breaker

    def completion(self, model: str, messages: List[Dict[str, str]],
                   temperature: float = 0.1, response_format: Optional[Dict] = None) -> Any:
        """
        Get completion from OpenAI API
        """
        if self.breaker and not self.breaker.can_execute():
            if self.logger:
                self.logger.error("Circuit breaker is open")
            raise RuntimeError("Circuit breaker is open")

        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }

            if response_format:
                kwargs["response_format"] = response_format

            response = self.client.chat.completions.create(**kwargs)

            if self.breaker:
                self.breaker.record_success()

            return response

        except Exception as e:
            if self.breaker:
                self.breaker.record_failure()
            if self.logger:
                self.logger.error(f"LLM completion failed: {e}")
            raise
