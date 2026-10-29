"""Simple, robust object localisation from RGB + depth: colour segmentation -> 3D in the robot base frame.

This is the perception used by the perception-based Baseline 0 (no learning). Real motorcycle parts will
need learned segmentation (open-vocabulary detectors) behind the same `ObjectDetection` interface.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from picking.cameras.base import Intrinsics


@dataclass
class ObjectDetection:
    object_id: str
    pixel: np.ndarray  # (u, v) centroid in the full frame
    position_base: np.ndarray  # (3,) estimated object CENTRE in base frame
    top_position_base: np.ndarray  # (3,) top-surface point in base frame
    yaw: float  # rad, in base frame (from the mask's principal axis)
    height_m: float
    n_pixels: int
    confidence: float
    yaw_period: float = np.pi / 2  # symmetry of valid grasp yaws: pi/2 square/round, pi elongated
    width_m: float | None = None  # top-face short side (grasp width) when known


# HSV ranges (OpenCV H in [0,180]) for the simple coloured objects
COLOR_RANGES = {
    "red": [((0, 120, 70), (10, 255, 255)), ((170, 120, 70), (180, 255, 255))],
    "green": [((40, 80, 60), (85, 255, 255))],
    "blue": [((95, 100, 60), (130, 255, 255))],
    "yellow": [((20, 100, 100), (35, 255, 255))],
}


def color_mask(rgb: np.ndarray, color: str) -> np.ndarray:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    m = np.zeros(rgb.shape[:2], np.uint8)
    for lo, hi in COLOR_RANGES[color]:
        m |= cv2.inRange(hsv, np.array(lo), np.array(hi))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return m


def largest_blob(mask: np.ndarray, min_pixels: int = 15) -> tuple[np.ndarray | None, np.ndarray | None]:
    blobs = blobs_by_size(mask, min_pixels)
    return blobs[0] if blobs else (None, None)


def blobs_by_size(mask: np.ndarray, min_pixels: int = 15) -> list[tuple[np.ndarray, np.ndarray]]:
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_pixels:
            out.append((stats[i, cv2.CC_STAT_AREA], (labels == i).astype(np.uint8), cents[i]))
    out.sort(key=lambda t: -t[0])
    return [(b, c) for _, b, c in out]


def deproject(intr: Intrinsics, u: float, v: float, z: float) -> np.ndarray:
    return np.array([(u - intr.cx) * z / intr.fx, (v - intr.cy) * z / intr.fy, z])


def to_base(T_base_camera: np.ndarray, p_cam: np.ndarray) -> np.ndarray:
    return T_base_camera[:3, :3] @ p_cam + T_base_camera[:3, 3]


def detect_colored_object(
    rgb: np.ndarray,
    depth: np.ndarray,
    intr: Intrinsics,
    T_base_camera: np.ndarray,
    color: str,
    object_id: str,
    table_z: float = 0.0,
    known_height_m: float | None = None,
    roi: tuple[int, int, int, int] | None = None,
    exclude_xy: tuple[np.ndarray, np.ndarray] | None = None,
    top_band_m: float = 0.003,
) -> ObjectDetection | None:
    """Localise the largest blob of `color`. Centre estimate = top-surface point minus half the height,
    where height = top z above the table (from depth) unless `known_height_m` is given."""
    mask = color_mask(rgb, color)
    if roi is not None:
        x0, y0, x1, y1 = roi
        m2 = np.zeros_like(mask)
        m2[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
        mask = m2
    for blob, cent in blobs_by_size(mask):
        ys, xs = np.nonzero(blob)
        zs = depth[ys, xs]
        ok = np.isfinite(zs) & (zs > 0.05)
        if ok.sum() < 5:
            continue
        # transform every valid blob pixel to the base frame and keep the TOP FACE (highest z band), which is
        # unbiased w.r.t. the viewing direction (a nearest-depth selection would be biased toward the camera)
        u = xs[ok].astype(np.float64)
        v = ys[ok].astype(np.float64)
        z = zs[ok].astype(np.float64)
        pts_cam = np.stack([(u - intr.cx) * z / intr.fx, (v - intr.cy) * z / intr.fy, z], axis=1)
        pts_all = (T_base_camera[:3, :3] @ pts_cam.T).T + T_base_camera[:3, 3]
        z_max = float(np.percentile(pts_all[:, 2], 90))
        sel_top = pts_all[:, 2] > z_max - top_band_m
        pts_base = pts_all[sel_top]
        sel = np.zeros_like(ok)
        sel[np.nonzero(ok)[0][sel_top]] = True
        top = pts_base.mean(0)
        top[2] = z_max
        if exclude_xy is not None:
            c, hs = exclude_xy
            if np.all(np.abs(top[:2] - c) <= hs + 0.01):
                continue  # this blob is the destination tray (or something inside it), not a pick target
        break
    else:
        return None
    height = float(max(top[2] - table_z, 0.005)) if known_height_m is None else float(known_height_m)
    centre = top.copy()
    centre[2] = table_z + height / 2
    # yaw of the SHORT side of the top face (the gripper must close across the narrow dimension)
    xy = pts_base[:, :2]
    if len(xy) >= 5:
        pts_mm = ((xy - xy.mean(0)) * 1000.0).astype(np.float32)
        (_, _), (w_r, h_r), ang = cv2.minAreaRect(pts_mm)
        yaw = float(np.deg2rad(ang))  # direction of the rect's "width" side
        if w_r > h_r:
            yaw += np.pi / 2  # width is the long side -> take the perpendicular (short) side
        yaw = float(np.angle(np.exp(1j * yaw)))
        ratio = min(w_r, h_r) / max(w_r, h_r, 1e-6)
        yaw_period = np.pi / 2 if ratio > 0.8 else np.pi
        width_m = float(min(w_r, h_r)) / 1000.0
    else:
        yaw = 0.0
        yaw_period = np.pi / 2
        width_m = None
    conf = float(min(1.0, sel.sum() / 50.0))
    return ObjectDetection(object_id, np.asarray(cent, float), centre, top, yaw, height, int(sel.sum()), conf, yaw_period, width_m)


def detect_by_height(
    rgb: np.ndarray,
    depth: np.ndarray,
    intr: Intrinsics,
    T_base_camera: np.ndarray,
    object_id: str = "object",
    table_z: float = 0.0,
    min_height_m: float = 0.004,
    max_height_m: float = 0.06,
    roi: tuple[int, int, int, int] | None = None,
    exclude_xy: tuple[np.ndarray, np.ndarray] | None = None,
    workspace_xy: tuple[tuple[float, float], tuple[float, float]] | None = None,
    stride: int = 2,
    min_pixels: int = 15,
) -> ObjectDetection | None:
    """Colour-agnostic localisation: everything that rises above the table plane inside the workspace is an
    object candidate (works for black / untextured parts as long as depth is valid). Returns the largest blob
    not inside the excluded (tray) footprint. Same output type as detect_colored_object."""
    h, w = depth.shape
    ys, xs = np.mgrid[0:h:stride, 0:w:stride]
    ys, xs = ys.ravel(), xs.ravel()
    zs = depth[ys, xs]
    ok = np.isfinite(zs) & (zs > 0.05)
    if roi is not None:
        x0, y0, x1, y1 = roi
        ok &= (xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1)
    u, v, z = xs[ok].astype(np.float64), ys[ok].astype(np.float64), zs[ok].astype(np.float64)
    pts_cam = np.stack([(u - intr.cx) * z / intr.fx, (v - intr.cy) * z / intr.fy, z], axis=1)
    pts = (T_base_camera[:3, :3] @ pts_cam.T).T + T_base_camera[:3, 3]
    # height band: above the table but below the arm/gripper (which hovers at >= 0.10 m when observing)
    above = (pts[:, 2] > table_z + min_height_m) & (pts[:, 2] < table_z + max_height_m)
    if workspace_xy is not None:
        (xa, xb), (ya, yb) = workspace_xy
        above &= (pts[:, 0] >= xa) & (pts[:, 0] <= xb) & (pts[:, 1] >= ya) & (pts[:, 1] <= yb)
    mask = np.zeros((h // stride + 1, w // stride + 1), np.uint8)
    mask[(ys[ok][above] // stride), (xs[ok][above] // stride)] = 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    for blob, cent in blobs_by_size(mask, min_pixels=max(3, min_pixels // (stride * stride))):
        by, bx = np.nonzero(blob)
        sel_idx = np.zeros(len(pts), bool)
        # map blob pixels back to point indices
        key = set(zip((by * stride).tolist(), (bx * stride).tolist()))
        for i, (yy, xx) in enumerate(zip(ys[ok], xs[ok])):
            if (int(yy), int(xx)) in key:
                sel_idx[i] = True
        p = pts[sel_idx & above]
        if len(p) < 5:
            continue
        z_max = float(np.percentile(p[:, 2], 90))
        top = p[p[:, 2] > z_max - 0.003]
        centre_xy = top[:, :2].mean(0)
        if exclude_xy is not None:
            c, hs = exclude_xy
            if np.all(np.abs(centre_xy - c) <= hs + 0.01):
                continue
        height = max(z_max - table_z, 0.005)
        centre = np.array([centre_xy[0], centre_xy[1], table_z + height / 2])
        top_pt = np.array([centre_xy[0], centre_xy[1], z_max])
        xy = top[:, :2]
        yaw, period, width_m = 0.0, np.pi / 2, None
        if len(xy) >= 5:
            pts_mm = ((xy - xy.mean(0)) * 1000.0).astype(np.float32)
            (_, _), (w_r, h_r), ang = cv2.minAreaRect(pts_mm)
            yaw = float(np.deg2rad(ang))
            if w_r > h_r:
                yaw += np.pi / 2
            yaw = float(np.angle(np.exp(1j * yaw)))
            period = np.pi / 2 if min(w_r, h_r) / max(w_r, h_r, 1e-6) > 0.8 else np.pi
            width_m = float(min(w_r, h_r)) / 1000.0
        return ObjectDetection(object_id, np.asarray(cent, float) * stride, centre, top_pt, yaw, float(height), int(len(top)), 1.0, period, width_m)
    return None
