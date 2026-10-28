"""MuJoCo camera exposed through the Camera interface (same frame convention as the OAK-D)."""
from __future__ import annotations

import time

import numpy as np

from picking.cameras.base import Camera, Frame, Intrinsics
from picking.core.transforms import make_T

# MuJoCo camera frame: x right, y up, z backward.  OpenCV frame: x right, y down, z forward.
R_MJ_TO_CV = np.diag([1.0, -1.0, -1.0])


class SimCamera(Camera):
    def __init__(self, env, name: str | None = None):
        self.env = env
        self.name = name or env.cam_name
        self._seq = 0

    def start(self):
        pass

    def stop(self):
        pass

    def intrinsics(self) -> Intrinsics:
        i = self.env.camera_intrinsics()
        return Intrinsics(i["fx"], i["fy"], i["cx"], i["cy"], i["width"], i["height"])

    def read(self) -> Frame:
        self._seq += 1
        return Frame(rgb=self.env.render_rgb(self.name), depth=self.env.render_depth(self.name), timestamp=time.time(), intrinsics=self.intrinsics(), seq=self._seq)

    def pose_cv_in_base(self) -> np.ndarray:
        """Ground-truth T_base_camera with the camera in OpenCV convention (for calibration tests)."""
        T = self.env.camera_pose()
        return T @ make_T(np.zeros(3), R=R_MJ_TO_CV)
