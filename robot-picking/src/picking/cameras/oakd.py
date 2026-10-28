"""Luxonis OAK-D through DepthAI (v3 API). RGB + stereo depth aligned to RGB, real intrinsics from EEPROM.

NOT YET VALIDATED ON HARDWARE in this repository: run scripts/test_oakd.py first (docs/HARDWARE.md).
The DepthAI API changed between v2 and v3; this module targets depthai>=3 and falls back with a clear
error message if the installed version differs.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from picking.cameras.base import Camera, Frame, Intrinsics

log = logging.getLogger(__name__)


class OakDCamera(Camera):
    name = "oakd"

    def __init__(self, width: int = 640, height: int = 400, fps: int = 30, align_depth: bool = True):
        self.width, self.height, self.fps = width, height, fps
        self.align_depth = align_depth
        self._pipeline = None
        self._q_rgb = None
        self._q_depth = None
        self._intr: Intrinsics | None = None
        self._seq = 0
        self.last_latency_ms = 0.0

    def start(self) -> None:
        import depthai as dai  # lazy: only on the lab machine

        major = int(str(dai.__version__).split(".")[0])
        if major < 3:
            raise RuntimeError(f"depthai {dai.__version__} found; this module targets DepthAI 3.x (pip install 'depthai>=3').")
        self._pipeline = dai.Pipeline()
        cam_rgb = self._pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
        rgb_out = cam_rgb.requestOutput((self.width, self.height), dai.ImgFrame.Type.RGB888i, fps=self.fps)
        mono_l = self._pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_B)
        mono_r = self._pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_C)
        stereo = self._pipeline.create(dai.node.StereoDepth)
        mono_l.requestOutput((self.width, self.height), fps=self.fps).link(stereo.left)
        mono_r.requestOutput((self.width, self.height), fps=self.fps).link(stereo.right)
        stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.DEFAULT)
        stereo.setLeftRightCheck(True)
        stereo.setSubpixel(True)
        if self.align_depth:
            stereo.setDepthAlign(dai.CameraBoardSocket.CAM_A)
            stereo.setOutputSize(self.width, self.height)
        self._q_rgb = rgb_out.createOutputQueue(maxSize=2, blocking=False)
        self._q_depth = stereo.depth.createOutputQueue(maxSize=2, blocking=False)
        self._pipeline.start()
        # intrinsics from the device calibration (EEPROM)
        calib = self._pipeline.getDefaultDevice().readCalibration()
        K = np.array(calib.getCameraIntrinsics(dai.CameraBoardSocket.CAM_A, self.width, self.height))
        dist = list(calib.getDistortionCoefficients(dai.CameraBoardSocket.CAM_A))
        self._intr = Intrinsics(float(K[0, 0]), float(K[1, 1]), float(K[0, 2]), float(K[1, 2]), self.width, self.height, dist)
        log.info("OAK-D started %dx%d@%d, intrinsics %s", self.width, self.height, self.fps, self._intr)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None

    def intrinsics(self) -> Intrinsics:
        assert self._intr is not None, "camera not started"
        return self._intr

    def read(self) -> Frame:
        t0 = time.time()
        rgb_msg = self._q_rgb.get()
        depth_msg = self._q_depth.tryGet() if self._q_depth is not None else None
        rgb = rgb_msg.getCvFrame()
        if rgb.ndim == 3 and rgb.shape[2] == 3:
            rgb = rgb[:, :, ::-1].copy()  # BGR -> RGB
        depth = None
        if depth_msg is not None:
            depth = depth_msg.getFrame().astype(np.float32) / 1000.0  # mm -> m
            depth[depth <= 0] = np.nan
        self._seq += 1
        self.last_latency_ms = (time.time() - t0) * 1000
        ts = rgb_msg.getTimestamp().total_seconds() if hasattr(rgb_msg, "getTimestamp") else time.time()
        return Frame(rgb=rgb, depth=depth, timestamp=float(ts), intrinsics=self._intr, seq=self._seq)
