import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fran.observability import CircuitBreaker, METRICS_REGISTRY, track_step


def test_track_step_records_duration():
    METRICS_REGISTRY.clear()
    with track_step("demo"):
        time.sleep(0.01)

    assert "demo" in METRICS_REGISTRY
    assert METRICS_REGISTRY["demo"][0] > 0


def test_circuit_breaker_opens_and_recovers():
    breaker = CircuitBreaker(failure_threshold=2, recovery_time=0.1)
    calls = {"count": 0}

    def _fail():
        calls["count"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        breaker.call(_fail)
    with pytest.raises(ValueError):
        breaker.call(_fail)

    with pytest.raises(RuntimeError):
        breaker.call(lambda: "should not run")

    time.sleep(0.12)
    assert breaker.call(lambda: "ok") == "ok"
    assert calls["count"] == 2
