"""
Observability utilities for metrics tracking
"""
import time
import logging
from contextlib import contextmanager
from collections import defaultdict
from typing import Optional


# Global metrics registry
METRICS_REGISTRY = defaultdict(list)


@contextmanager
def track_step(step_name: str, logger: Optional[logging.Logger] = None):
    """
    Context manager to track step execution time
    Usage:
        with track_step("my_step"):
            # do something
    """
    start_time = time.time()
    logger_instance = logger or logging.getLogger(__name__)

    try:
        yield
    finally:
        duration = time.time() - start_time
        metric = {"name": step_name, "duration": duration}
        METRICS_REGISTRY[step_name].append(metric)
        logger_instance.info(f"[metric] step={step_name} duration={duration:.3f}s")


def get_metrics(step_name: Optional[str] = None) -> dict:
    """
    Get metrics from registry
    """
    if step_name:
        return {step_name: METRICS_REGISTRY.get(step_name, [])}
    return dict(METRICS_REGISTRY)


def clear_metrics():
    """
    Clear metrics registry
    """
    METRICS_REGISTRY.clear()
