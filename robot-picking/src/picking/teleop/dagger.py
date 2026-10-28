"""DAgger-style human correction: the policy drives, the operator overrides with the keyboard.

Both the policy's action (extra.policy_action) and the executed action (action) are recorded by the
EpisodeRecorder, so a dataset built this way contains corrections on the states the policy itself visits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from picking.core.types import Action, Observation
from picking.teleop.keyboard import KeyMapper


@dataclass
class HumanCorrector:
    key_source: Callable[[], str | None]
    mapper: KeyMapper = field(default_factory=KeyMapper)
    hold_steps: int = 3  # after a keypress the human stays in control for this many steps
    n_corrections: int = 0
    n_steps: int = 0
    last_command: str | None = None
    _remaining: int = 0
    _last_action: Action | None = None

    def __call__(self, obs: Observation, policy_action: Action | None) -> Action | None:
        self.n_steps += 1
        key = self.key_source()
        if key is not None:
            ev = self.mapper.map(key)
            if ev.command:
                self.last_command = ev.command
                return None
            self._last_action = ev.action
            self._remaining = self.hold_steps
        if self._remaining > 0 and self._last_action is not None:
            self._remaining -= 1
            self.n_corrections += 1
            return self._last_action
        return None

    def correction_rate(self) -> float:
        return self.n_corrections / max(1, self.n_steps)
