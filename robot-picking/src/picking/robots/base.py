"""RobotAdapter: the only place where a concrete robot (sim / myCobot / UR10e) is touched.

Policies, safety, datasets and evaluation depend on this interface only.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

import numpy as np

from picking.core.transforms import make_T, rotvec_to_mat
from picking.core.types import Action, Pose, RobotState


@dataclass
class ActionScale:
    """Denormalization constants: policy action in [-1,1] -> metres / radians per step."""

    max_delta_pos: float
    max_delta_rot: float

    def denormalize(self, action: Action) -> tuple[np.ndarray, np.ndarray, float]:
        dpos = np.clip(action.dpos, -1, 1) * self.max_delta_pos
        drot = np.clip(action.drot, -1, 1) * self.max_delta_rot
        return dpos, drot, float(np.clip(action.gripper, 0, 1))


def target_from_delta(T_current: np.ndarray, dpos: np.ndarray, drot: np.ndarray) -> np.ndarray:
    """Apply a Cartesian delta expressed in the robot BASE frame to the current TCP pose."""
    R_new = rotvec_to_mat(drot) @ T_current[:3, :3]
    p_new = T_current[:3, 3] + dpos
    return make_T(p_new, R=R_new)


class RobotAdapter(abc.ABC):
    """Common interface. All poses are TCP poses in the robot base frame (metres, radians)."""

    name: str = "abstract"
    n_joints: int = 6

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def get_state(self) -> RobotState: ...

    @abc.abstractmethod
    def move_to_joints(self, q: np.ndarray, gripper: float | None = None, blocking: bool = True) -> None: ...

    @abc.abstractmethod
    def set_tcp_target(self, T_target: np.ndarray, gripper: float, blocking: bool = True) -> bool:
        """Command an absolute TCP pose (4x4) + gripper. Returns False if IK failed (robot must not move)."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Immediately hold position. Must be safe to call at any time, repeatedly."""

    @abc.abstractmethod
    def home(self) -> None: ...

    max_down_lead: float = 0.008  # [m] maximum commanded descent below the measured TCP height per step
    max_setpoint_lead: float = 0.03  # [m] re-anchor to the measurement only when the setpoint runs this far ahead (1.5 cm caused hover: with 0.2 s latency the lead hits the cap while descending, re-anchoring behind the real command)

    def commanded_tcp(self) -> np.ndarray | None:
        """Last commanded TCP pose (4x4) if the adapter tracks setpoints, else None."""
        return None

    def apply_action(self, action: Action, scale: ActionScale) -> bool:
        """Delta action -> absolute target -> set_tcp_target.

        The delta is applied to the last COMMANDED pose (setpoint) when available, not to the measured pose:
        with command latency or servo lag, deltas applied to a lagging measurement cancel out and the robot
        stalls (observed with 0.2 s latency in simulation). If the setpoint has run away from the measured
        pose (blocked motion, safety hold) the reference is re-anchored to the measurement."""
        state = self.get_state()
        T_meas = state.tcp.as_matrix()
        T_ref = self.commanded_tcp()
        if T_ref is None or np.linalg.norm(T_ref[:3, 3] - T_meas[:3, 3]) > self.max_setpoint_lead:
            T_ref = T_meas
        dpos, drot, g = scale.denormalize(action)
        T_target = target_from_delta(T_ref, dpos, drot)
        # anisotropic lead: never command the TCP more than `max_down_lead` BELOW the measured height, so a
        # lagging servo cannot be driven into the table while descending (lateral lead stays free)
        T_target[2, 3] = max(T_target[2, 3], T_meas[2, 3] - self.max_down_lead)
        return self.set_tcp_target(T_target, g)

    def gripper_opening_m(self) -> float | None:
        return None

    @property
    def joint_limits(self) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError

    def fk(self, q: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def tcp_pose(self) -> Pose:
        return self.get_state().tcp
