"""Kinematic mock adapters: no physics, exact tracking. Used by unit tests and for the UR10e stand-in."""
from __future__ import annotations

import time

import numpy as np

from picking.core.types import Pose, RobotState
from picking.robots.base import RobotAdapter


class KinematicMockAdapter(RobotAdapter):
    """Wraps any object with fk(q)/ik(T,q0)/lower/upper/home and tracks commanded joints exactly."""

    name = "kinematic_mock"

    def __init__(self, kin, name: str = "kinematic_mock"):
        self.kin = kin
        self.name = name
        self.q = np.array(kin.home, float)
        self.gripper = 0.0
        self.connected = False
        self.ik_failures = 0
        self.commands: list[dict] = []

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    @property
    def joint_limits(self):
        return self.kin.lower, self.kin.upper

    def fk(self, q):
        return self.kin.fk(q)

    def get_state(self) -> RobotState:
        return RobotState(joints=self.q.copy(), tcp=Pose.from_matrix(self.kin.fk(self.q)), gripper=self.gripper, timestamp=time.time())

    def move_to_joints(self, q, gripper=None, blocking=True):
        self.q = np.clip(np.asarray(q, float), self.kin.lower, self.kin.upper)
        if gripper is not None:
            self.gripper = float(gripper)
        self.commands.append({"type": "joints", "q": self.q.copy(), "gripper": self.gripper})

    def set_tcp_target(self, T_target, gripper, blocking=True) -> bool:
        q, ok, pe, re = self.kin.ik(T_target, self.q)
        if not ok:
            self.ik_failures += 1
            return False
        self.q = q
        self.gripper = float(gripper)
        self.commands.append({"type": "tcp", "q": q.copy(), "gripper": self.gripper})
        return True

    def stop(self):
        self.commands.append({"type": "stop"})

    def home(self):
        self.move_to_joints(self.kin.home, gripper=0.0)

    def reset_targets(self):
        self.q = np.array(self.kin.home, float)
        self.gripper = 0.0
        self.ik_failures = 0
