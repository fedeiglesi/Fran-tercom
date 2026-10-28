"""Multimodal grasp verification: gripper closed-but-not-empty + object no longer at its resting place."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from picking.perception.segmentation import color_mask, largest_blob


@dataclass
class GraspVerdict:
    holding: bool
    gripper_signal: bool
    visual_signal: bool | None
    gripper_opening_m: float | None
    detail: str


def verify_grasp(
    gripper_state: float,
    gripper_opening_m: float | None,
    min_object_width_m: float,
    rgb_before: np.ndarray | None = None,
    rgb_after: np.ndarray | None = None,
    color: str | None = None,
    pixel_before: np.ndarray | None = None,
    moved_px_threshold: float = 8.0,
) -> GraspVerdict:
    """gripper_signal: fingers stopped before fully closing (something is between the pads).
    visual_signal: the object's blob moved away from where it rested before the grasp (it was lifted)."""
    if gripper_opening_m is not None:
        g_sig = gripper_opening_m > min_object_width_m * 0.5
    else:
        g_sig = 0.3 < gripper_state < 0.97
    v_sig = None
    if rgb_before is not None and rgb_after is not None and color is not None and pixel_before is not None:
        blob, cent = largest_blob(color_mask(rgb_after, color))
        if blob is None:
            v_sig = True  # object not visible anymore where it was (occluded by gripper / lifted)
        else:
            v_sig = bool(np.linalg.norm(np.asarray(cent) - np.asarray(pixel_before)) > moved_px_threshold)
    holding = bool(g_sig and (v_sig is None or v_sig))
    return GraspVerdict(holding, bool(g_sig), v_sig, gripper_opening_m, f"gripper={g_sig} visual={v_sig}")
