"""Generic Denavit-Hartenberg kinematics (standard DH) with numeric Jacobian + damped-LS IK.

Used for robots we do not (yet) have a MuJoCo model for, e.g. the UR10e mock adapter.
"""
from __future__ import annotations

import numpy as np

from picking.core.transforms import mat_to_rotvec


def dh_transform(theta: float, d: float, a: float, alpha: float) -> np.ndarray:
    ct, st, ca, sa = np.cos(theta), np.sin(theta), np.cos(alpha), np.sin(alpha)
    return np.array([[ct, -st * ca, st * sa, a * ct], [st, ct * ca, -ct * sa, a * st], [0, sa, ca, d], [0, 0, 0, 1]])


class DHKinematics:
    def __init__(self, dh: list[tuple[float, float, float]], lower: np.ndarray, upper: np.ndarray, tcp_offset: np.ndarray | None = None, home: np.ndarray | None = None):
        """dh: list of (d, a, alpha) per joint (standard DH, theta = joint variable)."""
        self.dh = dh
        self.n = len(dh)
        self.lower = np.asarray(lower, float)
        self.upper = np.asarray(upper, float)
        self.T_tcp = np.eye(4)
        if tcp_offset is not None:
            self.T_tcp[:3, 3] = tcp_offset
        self.home = np.zeros(self.n) if home is None else np.asarray(home, float)

    def fk(self, q: np.ndarray) -> np.ndarray:
        T = np.eye(4)
        for i, (d, a, alpha) in enumerate(self.dh):
            T = T @ dh_transform(q[i], d, a, alpha)
        return T @ self.T_tcp

    def jacobian(self, q: np.ndarray, eps: float = 1e-6) -> np.ndarray:
        T0 = self.fk(q)
        J = np.zeros((6, self.n))
        for i in range(self.n):
            dq = np.array(q, float)
            dq[i] += eps
            T1 = self.fk(dq)
            J[:3, i] = (T1[:3, 3] - T0[:3, 3]) / eps
            J[3:, i] = mat_to_rotvec(T1[:3, :3] @ T0[:3, :3].T) / eps
        return J

    def ik(self, T_target: np.ndarray, q0: np.ndarray, max_iters: int = 200, pos_tol: float = 5e-4, rot_tol: float = 2e-3, damping: float = 1e-3) -> tuple[np.ndarray, bool, float, float]:
        q = np.clip(np.array(q0, float), self.lower, self.upper)
        for _ in range(max_iters):
            T = self.fk(q)
            dp = T_target[:3, 3] - T[:3, 3]
            dR = mat_to_rotvec(T_target[:3, :3] @ T[:3, :3].T)
            pe, re = np.linalg.norm(dp), np.linalg.norm(dR)
            if pe < pos_tol and re < rot_tol:
                return q, True, float(pe), float(re)
            err = np.concatenate([dp, 0.5 * dR])
            J = self.jacobian(q)
            J[3:] *= 0.5
            dq = J.T @ np.linalg.solve(J @ J.T + damping * np.eye(6), err)
            nrm = np.linalg.norm(dq)
            if nrm > 0.3:
                dq *= 0.3 / nrm
            q = np.clip(q + 0.8 * dq, self.lower, self.upper)
        T = self.fk(q)
        pe = float(np.linalg.norm(T_target[:3, 3] - T[:3, 3]))
        re = float(np.linalg.norm(mat_to_rotvec(T_target[:3, :3] @ T[:3, :3].T)))
        return q, False, pe, re


# Universal Robots UR10e nominal DH parameters (metres, radians), from UR's published kinematics.
UR10E_DH = [
    (0.1807, 0.0, np.pi / 2),
    (0.0, -0.6127, 0.0),
    (0.0, -0.57155, 0.0),
    (0.17415, 0.0, np.pi / 2),
    (0.11985, 0.0, -np.pi / 2),
    (0.11655, 0.0, 0.0),
]
UR10E_LIMITS = (np.full(6, -2 * np.pi), np.full(6, 2 * np.pi))
