"""Dataset schema (LeRobotDataset v3 features) shared by sim and real recording.

Keys follow LeRobot conventions so that LeRobot policies consume the dataset directly:
  observation.images.<camera>   uint8 HxWx3  (PNG when use_videos=False)
  observation.state             float32 (10,) tcp xyz + rotation 6D + gripper  <- policy input
  observation.joints            float32 (6,)  auxiliary joint positions (rad)
  observation.depth.<camera>    float32 HxW   metres (optional)
  action                        float32 (7,)  normalized robot-agnostic action executed (after safety)
  extra.*                       auxiliary (policy raw action, absolute target sent, joint targets, privileged pose)
"""
from __future__ import annotations

from picking.core.types import ACTION_NAMES

STATE_NAMES = ["tcp_x", "tcp_y", "tcp_z", "r11", "r21", "r31", "r12", "r22", "r32", "gripper"]
POSE7_NAMES = ["x", "y", "z", "qw", "qx", "qy", "qz"]
JOINT_NAMES = [f"joint{i}" for i in range(1, 7)]
SCHEMA_VERSION = "1.0"


ENV_STATE_NAMES = ["obj_x", "obj_y", "obj_z", "obj_yaw_cos", "obj_yaw_sin"]
ENV_STATE_DIM = 5


def build_features(camera_names: list[str], image_hw: tuple[int, int], with_depth: bool = False, use_videos: bool = False, with_env_state: bool = False) -> dict:
    h, w = image_hw
    img_dtype = "video" if use_videos else "image"
    feats: dict = {}
    for cam in camera_names:
        feats[f"observation.images.{cam}"] = {"dtype": img_dtype, "shape": (h, w, 3), "names": ["height", "width", "channels"]}
        if with_depth:
            feats[f"observation.depth.{cam}"] = {"dtype": "float32", "shape": (h, w), "names": None}
    feats["observation.state"] = {"dtype": "float32", "shape": (10,), "names": STATE_NAMES}
    if with_env_state:
        # explicit perception features (object pose from the perception module; in sim: privileged pose + noise)
        feats["observation.environment_state"] = {"dtype": "float32", "shape": (ENV_STATE_DIM,), "names": ENV_STATE_NAMES}
    feats["observation.joints"] = {"dtype": "float32", "shape": (6,), "names": JOINT_NAMES}
    feats["action"] = {"dtype": "float32", "shape": (7,), "names": ACTION_NAMES}
    feats["extra.policy_action"] = {"dtype": "float32", "shape": (7,), "names": ACTION_NAMES}
    feats["extra.target_tcp_pose"] = {"dtype": "float32", "shape": (7,), "names": POSE7_NAMES}
    feats["extra.target_joints"] = {"dtype": "float32", "shape": (6,), "names": JOINT_NAMES}
    feats["extra.object_pose"] = {"dtype": "float32", "shape": (7,), "names": POSE7_NAMES}
    feats["extra.safety_rejected"] = {"dtype": "float32", "shape": (1,), "names": None}
    feats["extra.corrected"] = {"dtype": "float32", "shape": (1,), "names": None}  # 1 = human override (DAgger)
    return feats
