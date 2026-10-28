from __future__ import annotations

import abc

from picking.core.types import Action, Observation


class Policy(abc.ABC):
    name: str = "abstract"
    version: str = "0"
    needs_images: bool = True

    def reset(self, instruction: str = "", task_info: dict | None = None) -> None:  # noqa: D401
        """Called at the start of every episode."""

    @abc.abstractmethod
    def act(self, obs: Observation) -> Action | None:
        """Return the next action, or None to signal HOLD (e.g. remote timeout)."""

    def done(self) -> bool:
        """Optional: policy declares the task complete (used by scripted policies)."""
        return False

    def info(self) -> dict:
        return {}
