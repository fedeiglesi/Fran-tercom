"""Baseline 0 for the REAL world: camera perception (colour + depth + calibration) feeds the scripted
pick-and-place state machine. No privileged state. SEE -> ACT -> VERIFY -> RECOVER with re-observation.

Observation requirements: obs.images[cam] full-frame RGB (not the cropped policy image), obs.depth[cam] in
metres, obs.extra["camera_intrinsics"], obs.extra["T_base_camera"], obs.extra["tray_pos"].
"""
from __future__ import annotations

import logging

import numpy as np

from picking.core.types import Action, Observation
from picking.perception.segmentation import detect_by_height, detect_colored_object
from picking.perception.verification import verify_grasp
from picking.policies.heuristic import ScriptedPickPlace

log = logging.getLogger(__name__)


class PerceptionScriptedPickPlace(ScriptedPickPlace):
    name = "perception_scripted"
    version = "1.0"
    needs_images = True

    def __init__(self, *args, camera: str, color: str = "red", object_id: str = "red_cube", known_height_m: float | None = None, roi=None, detector: str = "color", **kw):
        self.camera = camera
        self.detector = detector  # "color" | "height" (colour-agnostic: anything above the table)
        self.color = color
        self.object_id = object_id
        self.known_height = known_height_m
        self.roi = roi
        self.detection = None
        self.last_rgb = None
        self.n_detect_fail = 0
        self.verdicts = []
        super().__init__(*args, **kw)

    def reset(self, instruction: str = "", task_info: dict | None = None) -> None:
        super().reset(instruction, task_info)
        if task_info:
            self.color = task_info.get("color") or self.color
            self.object_id = task_info.get("target_object") or self.object_id
        self.detection = None
        self.last_rgb = None
        self.n_detect_fail = 0
        self.verdicts = []
        self.pixel_before = None

    def needs_frame_now(self) -> bool:
        return (self.grasp_target is None and self.phase == "approach") or self.phase in ("verify", "verify2")

    def _observe(self, obs: Observation) -> bool:
        rgb = obs.images[self.camera]
        depth = obs.depth.get(self.camera)
        if depth is None:
            return False
        if self.detector == "height":
            det = detect_by_height(rgb, depth, obs.extra["camera_intrinsics"], obs.extra["T_base_camera"], self.object_id, roi=self.roi, exclude_xy=obs.extra.get("tray_footprint"), workspace_xy=((0.08, 0.30), (-0.22, 0.22)))
        else:
            det = detect_colored_object(rgb, depth, obs.extra["camera_intrinsics"], obs.extra["T_base_camera"], self.color, self.object_id, known_height_m=self.known_height, roi=self.roi, exclude_xy=obs.extra.get("tray_footprint"))
        if det is None:
            self.n_detect_fail += 1
            return False
        self.detection = det
        self.pixel_before = det.pixel
        self.last_rgb = rgb
        return True

    def act(self, obs: Observation) -> Action:
        # SEE: (re-)observe whenever a new grasp attempt starts
        if self.grasp_target is None and self.phase == "approach":
            if not self._observe(obs):
                if self.n_detect_fail > 10:
                    self.phase = "failed"
                    self._done = True
                return Action.zero(0.0)
        priv = {
            "object_pos": self.detection.position_base if self.detection is not None else np.zeros(3),
            "object_yaw": self.detection.yaw if self.detection is not None else 0.0,
            "object_yaw_period": self.detection.yaw_period if self.detection is not None else np.pi / 2,
            "tray_pos": np.asarray(obs.extra["tray_pos"], float),
        }
        if self.phase == "verify":
            # VERIFY with real signals instead of privileged object height
            opening = obs.extra.get("gripper_opening_m")
            vd = verify_grasp(obs.gripper, opening, min_object_width_m=0.015, rgb_before=self.last_rgb, rgb_after=obs.images[self.camera], color=self.color, pixel_before=self.pixel_before)
            self.verdicts.append(vd.detail)
            priv["object_pos"] = np.array([*obs.tcp_pose[:2], (self.grasp_target[2] + 1.0) if vd.holding else self.grasp_target[2]])  # drive the base-class verify logic
        obs.extra["privileged"] = priv
        return super().act(obs)

    def _still_holding(self, obs: Observation) -> bool:
        opening = obs.extra.get("gripper_opening_m")
        return bool(opening is None or opening > 0.015 * 0.5)

    def info(self) -> dict:
        d = super().info()
        d.update({"n_detect_fail": self.n_detect_fail, "verdicts": self.verdicts[-5:], "last_detection": None if self.detection is None else {"pos": self.detection.position_base.tolist(), "yaw": self.detection.yaw, "n_px": self.detection.n_pixels}})
        return d
