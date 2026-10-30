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
        self.height_stride = int(kw.pop("height_stride", 1))
        self.sku_identifier = kw.pop("sku_identifier", None)
        self.wanted_sku = None
        self.last_labels = []
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
            self.wanted_sku = task_info.get("sku")
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
        if self.detector == "sku":
            # order fulfilment: instance detections inside the source bin labelled by the SKU identifier
            # (placeholder oracle for the learned detector, see perception/sku.py); pick the wanted SKU
            labelled = self.sku_identifier.detect(obs.extra["sku_true_objects"], obs.extra["sku_of_object"])
            self._install_yaw_scorer(obs, labelled)
            self.last_labels = [(l, d.position_base.round(3).tolist()) for d, l in labelled]
            wanted = [d for d, l in labelled if l == self.wanted_sku]
            det = wanted[0] if wanted else None
        elif self.detector == "height":
            det = detect_by_height(rgb, depth, obs.extra["camera_intrinsics"], obs.extra["T_base_camera"], self.object_id, roi=self.roi, exclude_xy=obs.extra.get("tray_footprint"), workspace_xy=((0.08, 0.30), (-0.22, 0.22)), stride=self.height_stride)
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
            "object_width": self.detection.width_m if self.detection is not None else None,
            "object_height": self.detection.height_m if self.detection is not None else None,
            "object_grasp_depth": self.detection.grasp_depth_frac if self.detection is not None else None,
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

    def _install_yaw_scorer(self, obs: Observation, labelled) -> None:
        """One geometric model for the grasp yaw choice and the pre-shaped opening: the two pads are 8 mm thick
        boxes, 16 mm wide, at +-(opening/2 .. opening/2 + 8 mm) along the closing axis. `clearance(obj, yaw,
        opening)` = the smallest distance of any pad corner to the bin walls (inside) and to the other detected
        objects (minus 18 mm). The yaw scorer rates each candidate at the MINIMUM opening (width + 2 mm); the
        limiter then opens as far as a 5 mm clearance allows."""
        c, hs = obs.extra["source_bin_footprint"]
        others = [d.position_base[:2] for d, _ in labelled]
        margin = (self.preshape_margin or 0.012)

        def pad_corners(obj, yaw, opening, sgn):
            ax = np.array([np.cos(yaw), np.sin(yaw)]); perp = np.array([-ax[1], ax[0]])
            r_in, r_out = opening / 2, opening / 2 + 0.008  # pad thickness 8 mm, half width 8 mm
            return [np.asarray(obj[:2]) + sgn * r * ax + e * 0.008 * perp for r in (r_in, r_out) for e in (-1, 1)]

        def clearance(obj, yaw, opening):
            worst = 1.0
            for sgn in (-1, 1):
                for q in pad_corners(obj, yaw, opening, sgn):
                    worst = min(worst, hs[0] - abs(q[0] - c[0]), hs[1] - abs(q[1] - c[1]))
                    for o in others:
                        if np.linalg.norm(o - obj[:2]) > 0.005:
                            worst = min(worst, float(np.linalg.norm(q - o)) - 0.018)
            return float(worst)

        def width_of():
            return float(self.detection.width_m or 0.025) if self.detection is not None else 0.025

        def scorer(obj, yaw):
            # prefer a yaw whose FULL pre-shaped opening is clear; otherwise rank by the clearance of the tightest
            # opening (penalised, so any yaw that fits comfortably wins)
            w = width_of()
            full = clearance(obj, yaw, min(self.gripper_max_opening, w + margin))
            return full if full >= 0.005 else clearance(obj, yaw, w + 0.002) - 0.1

        def limiter(obj, yaw, width):
            # largest opening (1 mm steps) whose pads stay 5 mm clear of the walls and 18 mm from neighbour centres
            best = width + 0.002
            for opening in np.arange(width + 0.002, min(self.gripper_max_opening, width + margin) + 1e-9, 0.001):
                if clearance(obj, yaw, opening) >= 0.005:
                    best = opening
            return float(best)

        self.yaw_scorer = scorer
        self.opening_limiter = limiter if getattr(self, "use_opening_limiter", True) else None

    def _still_holding(self, obs: Observation) -> bool:
        opening = obs.extra.get("gripper_opening_m")
        return bool(opening is None or opening > 0.015 * 0.5)

    def info(self) -> dict:
        d = super().info()
        d.update({"n_detect_fail": self.n_detect_fail, "verdicts": self.verdicts[-5:], "last_detection": None if self.detection is None else {"pos": self.detection.position_base.tolist(), "yaw": self.detection.yaw, "n_px": self.detection.n_pixels}, "sku_labels": self.last_labels[:6]})
        return d
