"""Perception -> policy feature bridge: produces observation.environment_state for low-dim policies.

SEE-on-demand: the object is (re-)detected from the full camera frame + depth whenever the gripper is open
and the arm is above approach height (i.e. before an approach starts), and the last detection is held
while a grasp is being executed (the object may be occluded by the gripper). The SAME hold rule is applied
when recording demonstrations (scripts/record_demos.py --lowdim) so training and deployment match.
"""
from __future__ import annotations

import numpy as np

from picking.core.types import Observation
from picking.perception.segmentation import ObjectDetection, detect_colored_object


# Re-detect only when the gripper is open AND high enough not to occlude the object (home height is 0.118 m).
# The SAME rule is used when recording low-dim demos, so training and deployment see identical features.
REDETECT_MIN_TCP_Z = 0.10


class PerceptionEnvState:
    def __init__(self, camera: str, color: str, object_id: str, redetect_min_tcp_z: float = REDETECT_MIN_TCP_Z, roi=None):
        self.camera = camera
        self.color = color
        self.object_id = object_id
        self.redetect_min_tcp_z = redetect_min_tcp_z
        self.roi = roi
        self.detection: ObjectDetection | None = None
        self.n_detections = 0
        self.n_failures = 0

    def reset(self, task_info: dict | None = None) -> None:
        if task_info:
            self.color = task_info.get("color") or self.color
            self.object_id = task_info.get("target_object") or self.object_id
        self.detection = None

    def needs_frame_now(self) -> bool:
        return self.detection is None or self._may_redetect_flag

    _may_redetect_flag = True

    def _may_redetect(self, obs: Observation) -> bool:
        return obs.gripper < 0.3 and obs.tcp_pose[2] > self.redetect_min_tcp_z

    def __call__(self, obs: Observation) -> np.ndarray:
        self._may_redetect_flag = self._may_redetect(obs)
        if (self.detection is None or self._may_redetect_flag) and self.camera in obs.images and self.camera in obs.depth:
            det = detect_colored_object(obs.images[self.camera], obs.depth[self.camera], obs.extra["camera_intrinsics"], obs.extra["T_base_camera"], self.color, self.object_id, roi=self.roi, exclude_xy=obs.extra.get("tray_footprint"))
            if det is not None:
                self.detection = det
                self.n_detections += 1
            else:
                self.n_failures += 1
        if self.detection is None:
            return np.zeros(5, np.float32)
        d = self.detection
        return env_state_vector(d.position_base, d.yaw, d.yaw_period)


def env_state_vector(position, yaw: float, yaw_period: float) -> np.ndarray:
    """[x, y, z, cos(k*yaw), sin(k*yaw)] with k = 2*pi/period: invariant to the object's rotational symmetry
    (k = 4 for a square top face), so a detector reporting yaw+90deg gives the same feature."""
    k = float(np.round(2 * np.pi / max(yaw_period, 1e-6)))
    return np.array([position[0], position[1], position[2], np.cos(k * yaw), np.sin(k * yaw)], np.float32)
