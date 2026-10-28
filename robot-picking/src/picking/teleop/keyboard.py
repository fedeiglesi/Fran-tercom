"""Keyboard teleoperation -> robot-agnostic Actions (no extra hardware needed).

Keys (hold-free, one keypress = one step at the configured speed):
  w/s : +x/-x      a/d : +y/-y      q/e : +z/-z (q up, e down)
  j/l : yaw -/+    c : close gripper   o : open gripper   space : hold
  1   : slow  2 : normal  3 : fast
  r   : reset episode     S : mark SUCCESS and end     F : mark FAILURE and end
  x / ESC : ABORT (stop robot, end session)
"""
from __future__ import annotations

import select
import sys
import termios
import tty
from dataclasses import dataclass

import numpy as np

from picking.core.types import Action


@dataclass
class TeleopEvent:
    action: Action | None
    command: str | None = None  # reset | success | failure | abort | None


class KeyMapper:
    SPEEDS = {"1": 0.25, "2": 0.5, "3": 1.0}

    def __init__(self):
        self.speed = 0.5
        self.gripper = 0.0

    def map(self, key: str | None) -> TeleopEvent:
        if key is None or key == " ":
            return TeleopEvent(Action.zero(self.gripper))
        if key in self.SPEEDS:
            self.speed = self.SPEEDS[key]
            return TeleopEvent(Action.zero(self.gripper))
        if key == "c":
            self.gripper = 1.0
            return TeleopEvent(Action.zero(self.gripper))
        if key == "o":
            self.gripper = 0.0
            return TeleopEvent(Action.zero(self.gripper))
        if key == "r":
            return TeleopEvent(None, "reset")
        if key == "S":
            return TeleopEvent(None, "success")
        if key == "F":
            return TeleopEvent(None, "failure")
        if key in ("x", "\x1b"):
            return TeleopEvent(None, "abort")
        dpos = np.zeros(3)
        drot = np.zeros(3)
        m = {"w": (0, 1), "s": (0, -1), "a": (1, 1), "d": (1, -1), "q": (2, 1), "e": (2, -1)}
        if key in m:
            i, sgn = m[key]
            dpos[i] = sgn * self.speed
        elif key == "j":
            drot[2] = -self.speed
        elif key == "l":
            drot[2] = self.speed
        return TeleopEvent(Action(dpos=dpos, drot=drot, gripper=self.gripper))


class RawKeyboard:
    """Non-blocking single-key reader for a POSIX terminal (macOS / Linux)."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *a):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def get_key(self, timeout: float = 0.0) -> str | None:
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
        return None
