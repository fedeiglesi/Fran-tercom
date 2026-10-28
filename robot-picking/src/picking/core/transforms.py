"""Rigid-body transform utilities (numpy only, no external deps).

Conventions used across the whole project:
  * positions in metres, angles in radians (the robot adapters convert to mm/deg at the edge).
  * rotations stored as unit quaternions in (w, x, y, z) order (MuJoCo order).
  * a pose is a 4x4 homogeneous matrix or a `Pose` (position + quaternion wxyz).
  * rotation vectors (axis-angle, rad) are the policy-facing orientation delta.
"""
from __future__ import annotations

import numpy as np

_EPS = 1e-10


def quat_normalize(q: np.ndarray) -> np.ndarray:
    q = np.asarray(q, dtype=np.float64)
    n = np.linalg.norm(q)
    if n < _EPS:
        return np.array([1.0, 0.0, 0.0, 0.0])
    return q / n


def quat_to_mat(q: np.ndarray) -> np.ndarray:
    w, x, y, z = quat_normalize(q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def mat_to_quat(R: np.ndarray) -> np.ndarray:
    R = np.asarray(R, dtype=np.float64)
    tr = np.trace(R)
    if tr > 0:
        s = np.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    q = np.array([w, x, y, z])
    if q[0] < 0:
        q = -q
    return quat_normalize(q)


def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array(
        [
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        ]
    )


def quat_conj(q: np.ndarray) -> np.ndarray:
    return np.array([q[0], -q[1], -q[2], -q[3]])


def rotvec_to_mat(rv: np.ndarray) -> np.ndarray:
    rv = np.asarray(rv, dtype=np.float64)
    th = np.linalg.norm(rv)
    if th < _EPS:
        return np.eye(3)
    k = rv / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K


def mat_to_rotvec(R: np.ndarray) -> np.ndarray:
    R = np.asarray(R, dtype=np.float64)
    cos_th = np.clip((np.trace(R) - 1) / 2, -1.0, 1.0)
    th = np.arccos(cos_th)
    if th < 1e-7:
        return np.zeros(3)
    if abs(np.pi - th) < 1e-6:
        # near pi: axis from symmetric part
        A = (R + np.eye(3)) / 2
        axis = np.sqrt(np.clip(np.diag(A), 0, None))
        # fix signs
        i = int(np.argmax(axis))
        for j in range(3):
            if j != i and A[i, j] < 0:
                axis[j] = -axis[j]
        axis = axis / (np.linalg.norm(axis) + _EPS)
        return axis * th
    axis = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * np.sin(th))
    return axis * th


def rotvec_to_quat(rv: np.ndarray) -> np.ndarray:
    return mat_to_quat(rotvec_to_mat(rv))


def quat_to_rotvec(q: np.ndarray) -> np.ndarray:
    return mat_to_rotvec(quat_to_mat(q))


def euler_xyz_to_mat(rx: float, ry: float, rz: float) -> np.ndarray:
    """Fixed-axis XYZ (roll about x, pitch about y, yaw about z): R = Rz @ Ry @ Rx (URDF rpy convention)."""
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def mat_to_euler_xyz(R: np.ndarray) -> np.ndarray:
    """Inverse of euler_xyz_to_mat (returns rx, ry, rz)."""
    R = np.asarray(R, dtype=np.float64)
    sy = -R[2, 0]
    sy = np.clip(sy, -1.0, 1.0)
    ry = np.arcsin(sy)
    if abs(np.cos(ry)) > 1e-6:
        rx = np.arctan2(R[2, 1], R[2, 2])
        rz = np.arctan2(R[1, 0], R[0, 0])
    else:  # gimbal lock
        rx = np.arctan2(-R[1, 2], R[1, 1])
        rz = 0.0
    return np.array([rx, ry, rz])


def make_T(pos: np.ndarray, R: np.ndarray | None = None, quat: np.ndarray | None = None) -> np.ndarray:
    T = np.eye(4)
    if R is None:
        R = quat_to_mat(quat) if quat is not None else np.eye(3)
    T[:3, :3] = R
    T[:3, 3] = np.asarray(pos, dtype=np.float64)
    return T


def inv_T(T: np.ndarray) -> np.ndarray:
    R = T[:3, :3]
    p = T[:3, 3]
    Ti = np.eye(4)
    Ti[:3, :3] = R.T
    Ti[:3, 3] = -R.T @ p
    return Ti


def T_from_urdf(xyz, rpy) -> np.ndarray:
    return make_T(np.asarray(xyz, dtype=np.float64), R=euler_xyz_to_mat(*rpy))


def rotation_error(R_a: np.ndarray, R_b: np.ndarray) -> float:
    """Angle (rad) between two rotations."""
    return float(np.linalg.norm(mat_to_rotvec(R_a.T @ R_b)))


def kabsch(P: np.ndarray, Q: np.ndarray) -> tuple[np.ndarray, float]:
    """Rigid transform T (4x4) mapping points P (Nx3) onto Q (Nx3): Q ~= R P + t.

    Returns (T, rms_residual). Used by camera->robot calibration from point correspondences.
    """
    P = np.asarray(P, dtype=np.float64)
    Q = np.asarray(Q, dtype=np.float64)
    assert P.shape == Q.shape and P.shape[0] >= 3
    cp, cq = P.mean(0), Q.mean(0)
    H = (P - cp).T @ (Q - cq)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    D = np.diag([1, 1, d])
    R = Vt.T @ D @ U.T
    t = cq - R @ cp
    T = make_T(t, R=R)
    res = (R @ P.T).T + t - Q
    rms = float(np.sqrt((res**2).sum(1).mean()))
    return T, rms
