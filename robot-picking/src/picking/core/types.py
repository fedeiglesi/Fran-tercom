"""Robot-agnostic data types shared by policy, safety, adapters, evaluator and dataset.

The policy never sees joints as its interface: it consumes `Observation` and emits `Action`
(normalized Cartesian TCP delta + gripper). Joints are carried only as auxiliary data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

import numpy as np

ACTION_DIM = 7  # dx, dy, dz, drx, dry, drz, gripper
ACTION_NAMES = ["dx", "dy", "dz", "drx", "dry", "drz", "gripper"]
PROTOCOL_VERSION = "1.0"
STATE_DIM = 10  # tcp xyz (3) + rotation 6D (6) + gripper (1)


@dataclass
class Pose:
    """TCP pose: position (m) + orientation quaternion (w,x,y,z) in the robot base frame."""

    position: np.ndarray  # (3,)
    quaternion: np.ndarray  # (4,) wxyz

    def as_matrix(self) -> np.ndarray:
        from picking.core.transforms import make_T

        return make_T(self.position, quat=self.quaternion)

    @classmethod
    def from_matrix(cls, T: np.ndarray) -> "Pose":
        from picking.core.transforms import mat_to_quat

        return cls(position=np.array(T[:3, 3], dtype=np.float64), quaternion=mat_to_quat(T[:3, :3]))

    def to_list(self) -> list[float]:
        return [*map(float, self.position), *map(float, self.quaternion)]


@dataclass
class RobotState:
    joints: np.ndarray  # (n,) rad
    tcp: Pose
    gripper: float  # 0 = fully open, 1 = fully closed (normalized)
    joint_velocities: np.ndarray | None = None
    timestamp: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """Versioned observation (what is sent to a policy, local or remote)."""

    timestamp: float
    images: dict[str, np.ndarray]  # name -> HxWx3 uint8
    tcp_pose: np.ndarray  # (7,) xyz + quaternion wxyz
    joints: np.ndarray  # (n,) rad
    gripper: float
    language_instruction: str = ""
    depth: dict[str, np.ndarray] = field(default_factory=dict)  # name -> HxW float32 metres
    protocol_version: str = PROTOCOL_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def state_vector(self) -> np.ndarray:
        """Low-dim proprioceptive state used by learned policies: tcp xyz(3) + rot6d(6) + gripper(1) = 10.

        Orientation uses the continuous 6D representation (first two columns of the rotation matrix)
        instead of a quaternion, whose sign flips around 180 deg (the straight-down grasp orientation).
        """
        from picking.core.transforms import quat_to_mat

        R = quat_to_mat(self.tcp_pose[3:7])
        rot6d = np.concatenate([R[:, 0], R[:, 1]])
        return np.concatenate([self.tcp_pose[:3], rot6d, [self.gripper]]).astype(np.float32)


@dataclass
class Action:
    """Normalized robot-agnostic action in [-1, 1]^6 x [0, 1].

    dpos/drot are fractions of the configured per-step maxima (safety config);
    gripper is a target 0 (open) .. 1 (closed).
    """

    dpos: np.ndarray  # (3,) normalized
    drot: np.ndarray  # (3,) normalized rotation vector
    gripper: float

    def to_array(self) -> np.ndarray:
        return np.concatenate([self.dpos, self.drot, [self.gripper]]).astype(np.float32)

    @classmethod
    def from_array(cls, a: np.ndarray) -> "Action":
        a = np.asarray(a, dtype=np.float64).reshape(-1)
        assert a.shape[0] == ACTION_DIM, f"action must have {ACTION_DIM} dims, got {a.shape}"
        return cls(dpos=a[:3].copy(), drot=a[3:6].copy(), gripper=float(a[6]))

    @classmethod
    def zero(cls, gripper: float = 0.0) -> "Action":
        return cls(dpos=np.zeros(3), drot=np.zeros(3), gripper=gripper)

    def is_finite(self) -> bool:
        return bool(np.all(np.isfinite(self.to_array())))


@dataclass
class ActionChunk:
    policy_version: str
    timestamp: float
    actions: np.ndarray  # (k, 7)
    protocol_version: str = PROTOCOL_VERSION
    inference_latency_s: float = 0.0


class FailureType(str, Enum):
    NONE = "NONE"
    NO_GRASP = "NO_GRASP"
    SLIP = "SLIP"
    COLLISION = "COLLISION"
    BAD_APPROACH = "BAD_APPROACH"
    BAD_TARGET = "BAD_TARGET"
    OCCLUSION = "OCCLUSION"
    DEPTH_ERROR = "DEPTH_ERROR"
    DROP = "DROP"
    WRONG_OBJECT = "WRONG_OBJECT"
    PLACE_FAIL = "PLACE_FAIL"
    TIMEOUT = "TIMEOUT"
    SAFETY_STOP = "SAFETY_STOP"
    IK_FAIL = "IK_FAIL"
    INVALID_ACTION = "INVALID_ACTION"


@dataclass
class EpisodeResult:
    episode_id: str
    success: bool
    grasp_success: bool
    place_success: bool
    collision: bool
    drop: bool
    timeout: bool
    failure_type: str
    n_steps: int
    cycle_time_s: float
    path_length_m: float
    n_retries: int
    seed: int
    task: str
    object_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d
