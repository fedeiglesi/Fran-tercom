"""Deterministic safety layer. Independent from any learned policy; the policy cannot disable it.

    safe = safety.filter(action, state)
    if safe is None: robot.stop()
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np

from picking.core.transforms import rotvec_to_mat
from picking.core.types import Action, RobotState
from picking.robots.base import ActionScale, target_from_delta

log = logging.getLogger(__name__)


@dataclass
class SafetyEvent:
    step: int
    reason: str
    detail: str = ""


@dataclass
class SafetyFilter:
    cfg: dict
    kin_fk: object | None = None  # callable q -> T (for joint-limit prediction, optional)
    events: list[SafetyEvent] = field(default_factory=list)
    n_clamped: int = 0
    n_rejected: int = 0
    step: int = 0
    last_valid_time: float = field(default_factory=time.time)

    def __post_init__(self):
        ws = self.cfg["workspace"]
        self.lo = np.array([ws["x"][0], ws["y"][0], ws["z"][0]], float)
        self.hi = np.array([ws["x"][1], ws["y"][1], ws["z"][1]], float)
        self.max_tilt = np.deg2rad(self.cfg["orientation"]["max_tilt_from_down_deg"])
        ps = self.cfg["per_step"]
        self.scale = ActionScale(max_delta_pos=float(ps["max_delta_pos"]), max_delta_rot=float(ps["max_delta_rot"]))
        self.max_tcp_speed = float(self.cfg["velocity"]["max_tcp_speed"])
        self.action_timeout = float(self.cfg["comm"]["action_timeout_s"])
        self.max_stale = float(self.cfg["comm"]["max_stale_action_age_s"])

    # ------------------------------------------------------------------
    def _reject(self, reason: str, detail: str = "") -> None:
        self.n_rejected += 1
        self.events.append(SafetyEvent(self.step, reason, detail))
        log.warning("SAFETY REJECT [%s] %s", reason, detail)

    def filter(self, action: Action | None, state: RobotState, dt: float, action_timestamp: float | None = None) -> Action | None:
        """Return a safe (possibly clamped) action, or None if the robot must STOP/HOLD."""
        self.step += 1
        now = time.time()
        if action is None:
            if now - self.last_valid_time > self.action_timeout:
                self._reject("ACTION_TIMEOUT", f"no action for {now - self.last_valid_time:.2f}s")
            return None
        if action_timestamp is not None and (now - action_timestamp) > self.max_stale:
            self._reject("STALE_ACTION", f"age {now - action_timestamp:.3f}s")
            return None
        a = action.to_array()
        if a.shape != (7,) or not np.all(np.isfinite(a)):
            self._reject("INVALID_VALUES", str(a))
            return None
        # 1. clamp normalized magnitudes
        dpos = np.clip(action.dpos, -1, 1)
        drot = np.clip(action.drot, -1, 1)
        g = float(np.clip(action.gripper, 0, 1))
        if np.any(np.abs(action.dpos) > 1) or np.any(np.abs(action.drot) > 1):
            self.n_clamped += 1
        # 2. speed limit (metres per step)
        dp_m = dpos * self.scale.max_delta_pos
        speed = np.linalg.norm(dp_m) / dt
        if speed > self.max_tcp_speed:
            dp_m *= self.max_tcp_speed / speed
            self.n_clamped += 1
        # 3. workspace: clamp the *target* into the box
        T = state.tcp.as_matrix()
        p_target = T[:3, 3] + dp_m
        p_clamped = np.clip(p_target, self.lo, self.hi)
        if np.any(p_clamped != p_target):
            self.n_clamped += 1
            dp_m = p_clamped - T[:3, 3]
        # if the current pose is already outside the workspace by a lot -> stop
        if np.any(T[:3, 3] < self.lo - 0.03) or np.any(T[:3, 3] > self.hi + 0.03):
            self._reject("OUT_OF_WORKSPACE", f"tcp {T[:3,3]}")
            return None
        # 4. orientation cone: keep gripper z axis near straight down
        dr = drot * self.scale.max_delta_rot
        R_new = rotvec_to_mat(dr) @ T[:3, :3]
        z_axis = R_new[:, 2]
        tilt = np.arccos(np.clip(-z_axis[2], -1, 1))
        if tilt > self.max_tilt:
            # reject only the rotational part
            dr = np.zeros(3)
            self.n_clamped += 1
            cur_tilt = np.arccos(np.clip(-T[:3, 2][2], -1, 1))
            if cur_tilt > self.max_tilt + 0.2:
                self._reject("ORIENTATION_OUT_OF_CONE", f"tilt {np.rad2deg(cur_tilt):.1f} deg")
                return None
        self.last_valid_time = now
        return Action(dpos=dp_m / self.scale.max_delta_pos, drot=dr / self.scale.max_delta_rot, gripper=g)

    def target_pose_is_safe(self, T_target: np.ndarray) -> bool:
        p = T_target[:3, 3]
        if np.any(p < self.lo) or np.any(p > self.hi):
            return False
        tilt = np.arccos(np.clip(-T_target[:3, 2][2], -1, 1))
        return bool(tilt <= self.max_tilt)

    def summary(self) -> dict:
        return {"n_clamped": self.n_clamped, "n_rejected": self.n_rejected, "events": [e.__dict__ for e in self.events[-20:]]}
