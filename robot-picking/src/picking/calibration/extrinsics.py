"""Camera -> robot-base extrinsic calibration (T_base_camera) for a FIXED external camera.

Primary method (robust, needs no printed target on the robot): POINT CORRESPONDENCES.
  The operator jogs the robot so the gripper TCP touches N >= 6 points spread over the workspace; at each
  point we record (a) the TCP position from the robot's FK (base frame) and (b) the 3D position of the
  same physical point seen by the camera (pixel + depth, or an ArUco marker centre). Kabsch (SVD) gives
  the rigid transform with an RMS residual used as the acceptance metric.

Secondary method: OpenCV hand-eye (eye-to-hand) with an ArUco marker attached to the gripper.

Results are saved as versioned YAML with metadata (calibration/oakd_to_mycobot_vNNN.yaml).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from picking.core.transforms import inv_T, kabsch, mat_to_quat, make_T, quat_to_mat
from picking.utils.io import git_sha, load_yaml, save_yaml


@dataclass
class CalibrationResult:
    T_base_camera: np.ndarray
    rms_residual_m: float
    max_residual_m: float
    n_points: int
    method: str
    residuals_m: list[float] = field(default_factory=list)

    def to_dict(self, extra: dict | None = None) -> dict:
        T = self.T_base_camera
        return {
            "transform": {
                "frame_from": "camera_optical (OpenCV: x right, y down, z forward)",
                "frame_to": "robot_base",
                "matrix": T.tolist(),
                "translation_m": T[:3, 3].tolist(),
                "quaternion_wxyz": mat_to_quat(T[:3, :3]).tolist(),
            },
            "quality": {"rms_residual_m": self.rms_residual_m, "max_residual_m": self.max_residual_m, "n_points": self.n_points, "residuals_m": self.residuals_m},
            "method": self.method,
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "git_sha": git_sha(),
            **(extra or {}),
        }


def calibrate_from_points(points_camera: np.ndarray, points_base: np.ndarray) -> CalibrationResult:
    P = np.asarray(points_camera, float)
    Q = np.asarray(points_base, float)
    if P.shape[0] < 4:
        raise ValueError("need at least 4 point correspondences (6+ recommended)")
    T, rms = kabsch(P, Q)
    res = np.linalg.norm((T[:3, :3] @ P.T).T + T[:3, 3] - Q, axis=1)
    return CalibrationResult(T, rms, float(res.max()), int(P.shape[0]), "point_correspondences_kabsch", res.tolist())


def calibrate_hand_eye(T_base_gripper: list[np.ndarray], T_camera_marker: list[np.ndarray]) -> CalibrationResult:
    """Eye-to-hand: camera fixed in the world, marker attached to the gripper.
    Solves for T_base_camera with OpenCV's calibrateHandEye on the inverted gripper poses."""
    import cv2

    R_g2b, t_g2b, R_t2c, t_t2c = [], [], [], []
    for Tbg, Tcm in zip(T_base_gripper, T_camera_marker):
        Tgb = inv_T(Tbg)  # eye-to-hand trick: treat base as the moving "gripper"
        R_g2b.append(Tgb[:3, :3])
        t_g2b.append(Tgb[:3, 3])
        R_t2c.append(Tcm[:3, :3])
        t_t2c.append(Tcm[:3, 3])
    R, t = cv2.calibrateHandEye(R_g2b, t_g2b, R_t2c, t_t2c, method=cv2.CALIB_HAND_EYE_PARK)
    T_base_camera = make_T(t.reshape(3), R=R)
    # consistency residual: marker position in base should be constant relative to gripper
    offs = []
    for Tbg, Tcm in zip(T_base_gripper, T_camera_marker):
        offs.append((inv_T(Tbg) @ T_base_camera @ Tcm)[:3, 3])
    offs = np.array(offs)
    res = np.linalg.norm(offs - offs.mean(0), axis=1)
    return CalibrationResult(T_base_camera, float(np.sqrt((res**2).mean())), float(res.max()), len(offs), "hand_eye_park_eye_to_hand", res.tolist())


def save_calibration(result: CalibrationResult, path: Path, extra: dict | None = None) -> Path:
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} exists: calibrations are versioned, never overwritten")
    save_yaml(result.to_dict(extra), path)
    return path


def load_calibration(path: Path) -> np.ndarray:
    d = load_yaml(path)
    return np.array(d["transform"]["matrix"], float)


def next_calibration_path(base_dir: Path, name: str = "oakd_to_mycobot") -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (base_dir / f"{name}_v{n:03d}.yaml").exists():
        n += 1
    return base_dir / f"{name}_v{n:03d}.yaml"


def check_calibration_quality(result: CalibrationResult, max_rms_m: float = 0.004, max_point_m: float = 0.008) -> tuple[bool, str]:
    ok = result.rms_residual_m <= max_rms_m and result.max_residual_m <= max_point_m and result.n_points >= 4
    msg = f"rms {result.rms_residual_m*1000:.1f} mm (<= {max_rms_m*1000:.0f}), max {result.max_residual_m*1000:.1f} mm (<= {max_point_m*1000:.0f}), n={result.n_points}"
    return ok, ("PASS " if ok else "FAIL ") + msg


def camera_point_to_base(T_base_camera: np.ndarray, p_cam: np.ndarray) -> np.ndarray:
    return T_base_camera[:3, :3] @ np.asarray(p_cam, float) + T_base_camera[:3, 3]


def detect_aruco_centers(rgb: np.ndarray, dictionary: str = "DICT_4X4_50") -> dict[int, np.ndarray]:
    """Returns {marker_id: (u, v)} pixel centres."""
    import cv2

    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
    det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
    corners, ids, _ = det.detectMarkers(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
    out = {}
    if ids is None:
        return out
    for c, i in zip(corners, ids.reshape(-1)):
        out[int(i)] = c.reshape(-1, 2).mean(0)
    return out


def estimate_aruco_poses(rgb: np.ndarray, K: np.ndarray, dist: np.ndarray, marker_length_m: float, dictionary: str = "DICT_4X4_50") -> dict[int, np.ndarray]:
    """Returns {marker_id: T_camera_marker} using solvePnP (IPPE_SQUARE)."""
    import cv2

    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
    det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
    corners, ids, _ = det.detectMarkers(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
    out = {}
    if ids is None:
        return out
    s = marker_length_m / 2
    obj = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float32)
    for c, i in zip(corners, ids.reshape(-1)):
        ok, rvec, tvec = cv2.solvePnP(obj, c.reshape(-1, 2).astype(np.float32), K, dist, flags=cv2.SOLVEPNP_IPPE_SQUARE)
        if ok:
            R, _ = cv2.Rodrigues(rvec)
            out[int(i)] = make_T(tvec.reshape(3), R=R)
    return out
