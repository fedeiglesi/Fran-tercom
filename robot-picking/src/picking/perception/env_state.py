"""Perception -> policy feature bridge: produces observation.environment_state for low-dim policies.

SEE-on-demand: the object is (re-)detected from the full camera frame + depth whenever the gripper is open
and the arm is above approach height (i.e. before an approach starts), and the last detection is held
while a grasp is being executed (the object may be occluded by the gripper).
"""
from __future__ import annotations

import numpy as np

from picking.core.types import Observation
from picking.perception.segmentation import ObjectDetection, detect_colored_object


class PerceptionEnvState:
    def __init__(self, camera: str, color: str, object_id: str, redetect_min_tcp_z: float = 0.05, roi=None):
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
        return np.array([d.position_base[0], d.position_base[1], d.position_base[2], np.cos(d.yaw), np.sin(d.yaw)], np.float32)
