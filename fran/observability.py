import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Dict, List, Optional


METRICS_REGISTRY: Dict[str, List[float]] = defaultdict(list)
_metrics_lock = Lock()


@contextmanager
def track_step(name: str):
    """Medir duración de un bloque y registrar la métrica."""
    start = time.time()
    try:
        yield
    finally:
        duration = time.time() - start
        with _metrics_lock:
            METRICS_REGISTRY[name].append(duration)
        logging.getLogger("fran313").info(
            "[metric] step=%s duration=%.3fs", name, duration
        )


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_time: float = 60.0
    _failure_count: int = 0
    _state: str = "closed"
    _opened_at: Optional[float] = None
    _lock: Lock = field(default_factory=Lock)

    def call(self, func: Callable[..., Any], *args, **kwargs):
        with self._lock:
            if self._state == "open":
                if self._opened_at and (time.time() - self._opened_at) >= self.recovery_time:
                    self._state = "half-open"
                else:
                    raise RuntimeError("Circuit breaker is open")

        try:
            result = func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._state = "open"
                    self._opened_at = time.time()
            raise
        else:
            with self._lock:
                self._failure_count = 0
                self._state = "closed"
                self._opened_at = None
            return result

    def reset(self):
        with self._lock:
            self._failure_count = 0
            self._state = "closed"
            self._opened_at = None


class InstrumentedClient:
    def __init__(self, logger: Optional[logging.Logger] = None, breaker: Optional[CircuitBreaker] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.breaker = breaker or CircuitBreaker()

    def run(self, name: str, func: Callable[..., Any], *args, **kwargs):
        def _call():
            return func(*args, **kwargs)

        with track_step(name):
            return self.breaker.call(_call)
