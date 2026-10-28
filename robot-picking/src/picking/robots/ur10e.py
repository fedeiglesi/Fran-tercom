"""UR10e adapter: interface + kinematic mock today, RTDE-backed implementation when hardware exists.

The policy API (Observation -> Action) does not change when swapping MyCobotAdapter for UR10eAdapter.
See docs/MIGRATION_UR10E.md.
"""
from __future__ import annotations

import logging

import numpy as np

from picking.robots.dh import UR10E_DH, UR10E_LIMITS, DHKinematics
from picking.robots.mock import KinematicMockAdapter

log = logging.getLogger(__name__)

UR10E_HOME = np.deg2rad([0, -90, 90, -90, -90, 0])


def ur10e_kinematics(tcp_offset=(0.0, 0.0, 0.15)) -> DHKinematics:
    return DHKinematics(UR10E_DH, UR10E_LIMITS[0], UR10E_LIMITS[1], tcp_offset=np.asarray(tcp_offset), home=UR10E_HOME)


class UR10eAdapter(KinematicMockAdapter):
    """mock=True: exact kinematic simulation (default, no hardware).
    mock=False: requires `ur_rtde` (RTDEControlInterface.servoJ / moveJ) -- NOT YET VALIDATED ON HARDWARE."""

    name = "ur10e"

    def __init__(self, cfg: dict | None = None, mock: bool = True):
        cfg = cfg or {}
        super().__init__(ur10e_kinematics(cfg.get("tcp_offset", (0.0, 0.0, 0.15))), name="ur10e_mock" if mock else "ur10e")
        self.mock = mock
        self.host = cfg.get("host", "192.168.1.100")
        self.speed = float(cfg.get("joint_speed", 0.3))
        self.accel = float(cfg.get("joint_accel", 0.3))
        self._rtde_c = None
        self._rtde_r = None

    def connect(self):
        if self.mock:
            return super().connect()
        import rtde_control, rtde_receive  # type: ignore  # lazy: only on the industrial cell

        self._rtde_c = rtde_control.RTDEControlInterface(self.host)
        self._rtde_r = rtde_receive.RTDEReceiveInterface(self.host)
        self.q = np.array(self._rtde_r.getActualQ())
        self.connected = True

    def disconnect(self):
        if not self.mock and self._rtde_c is not None:
            self._rtde_c.stopScript()
        self.connected = False

    def move_to_joints(self, q, gripper=None, blocking=True):
        super().move_to_joints(q, gripper, blocking)
        if not self.mock:
            self._rtde_c.moveJ(list(self.q), self.speed, self.accel, not blocking)

    def set_tcp_target(self, T_target, gripper, blocking=True) -> bool:
        ok = super().set_tcp_target(T_target, gripper, blocking)
        if ok and not self.mock:
            self._rtde_c.moveJ(list(self.q), self.speed, self.accel, False)
        return ok

    def stop(self):
        super().stop()
        if not self.mock and self._rtde_c is not None:
            self._rtde_c.stopJ(2.0)
