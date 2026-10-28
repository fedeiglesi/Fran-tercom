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

    def apply_action(self, action: Action, scale: ActionScale) -> bool:
        """Convenience: delta action -> absolute target -> set_tcp_target."""
        state = self.get_state()
        dpos, drot, g = scale.denormalize(action)
        T_target = target_from_delta(state.tcp.as_matrix(), dpos, drot)
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
