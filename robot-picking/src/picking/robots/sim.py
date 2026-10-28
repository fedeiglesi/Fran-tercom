"""RobotAdapter implementation on top of the MuJoCo environment."""
from __future__ import annotations

import time

import numpy as np

from picking.core.types import Pose, RobotState
from picking.robots.base import RobotAdapter
from picking.robots.kinematics import Kinematics
from picking.simulation.env import ContactSummary, PickPlaceEnv


class SimRobotAdapter(RobotAdapter):
    name = "sim_mycobot_280"

    def __init__(self, env: PickPlaceEnv, kin: Kinematics | None = None):
        self.env = env
        self.kin = kin or Kinematics(env.cfg["robot"])
        self.last_contacts: ContactSummary = ContactSummary()
        self._target_q = env.home_q.copy()
        self._gripper_cmd = 0.0
        self.ik_failures = 0
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    @property
    def joint_limits(self):
        return self.kin.lower, self.kin.upper

    def fk(self, q):
        return self.kin.fk(q)

    def get_state(self) -> RobotState:
        T = self.env.tcp_matrix()
        return RobotState(
            joints=self.env.joint_positions(),
            tcp=Pose.from_matrix(T),
            gripper=self.env.gripper_state(),
            joint_velocities=self.env.joint_velocities(),
            timestamp=time.time(),
            extra={"sim_time": self.env.sim_time(), "gripper_opening_m": self.env.gripper_opening(), "gripper_cmd": self._gripper_cmd},
        )

    def gripper_opening_m(self):
        return self.env.gripper_opening()

    def move_to_joints(self, q, gripper=None, blocking=True) -> None:
        self._target_q = np.asarray(q, dtype=float).copy()
        if gripper is not None:
            self._gripper_cmd = float(gripper)
        self.env.set_joint_targets(self._target_q, self._gripper_cmd)
        n = self.env.substeps * (10 if blocking else 1)
        self.last_contacts = self.env.advance(n)

    def set_tcp_target(self, T_target, gripper, blocking=True) -> bool:
        q, ok, pe, re = self.kin.ik(T_target, self._target_q)
        if not ok:
            self.ik_failures += 1
            # hold current target, still advance physics so time passes consistently
            self._gripper_cmd = float(gripper)
            self.env.set_joint_targets(self._target_q, self._gripper_cmd)
            self.last_contacts = self.env.advance()
            return False
        self._target_q = q
        self._gripper_cmd = float(gripper)
        self.env.set_joint_targets(q, self._gripper_cmd)
        self.last_contacts = self.env.advance()
        return True

    def stop(self) -> None:
        # hold: command the current measured joints
        self._target_q = self.env.joint_positions()
        self.env.set_joint_targets(self._target_q, self._gripper_cmd)
        self.last_contacts = self.env.advance()

    def home(self) -> None:
        self.move_to_joints(self.env.home_q, gripper=0.0, blocking=True)

    def reset_targets(self) -> None:
        self._target_q = self.env.home_q.copy()
        self._gripper_cmd = 0.0
        self.ik_failures = 0
        self.last_contacts = ContactSummary()
