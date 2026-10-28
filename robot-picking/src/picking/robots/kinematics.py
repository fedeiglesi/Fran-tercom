"""Forward / inverse kinematics for the myCobot 280 using a contact-free MuJoCo model.

The same class serves the simulated and the real robot adapters, so Cartesian targets are
converted to joint targets identically in both worlds (important for sim-to-real).
"""
from __future__ import annotations

import numpy as np

from picking.core.transforms import make_T, mat_to_rotvec, quat_to_mat
from picking.simulation.mjcf import JOINT_LIMITS, kinematic_model_xml


class Kinematics:
    def __init__(self, robot_cfg: dict):
        import mujoco

        self._mj = mujoco
        self.model = mujoco.MjModel.from_xml_string(kinematic_model_xml(robot_cfg))
        self.data = mujoco.MjData(self.model)
        self.site_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, "tcp")
        self.n = 6
        self.lower = np.array([lo for lo, _ in JOINT_LIMITS])
        self.upper = np.array([hi for _, hi in JOINT_LIMITS])
        self.home = np.deg2rad(np.asarray(robot_cfg.get("home_joints_deg", [0, 0, 0, 0, 0, 0]), dtype=float))

    def fk(self, q: np.ndarray) -> np.ndarray:
        """4x4 TCP pose in the base frame."""
        self.data.qpos[: self.n] = q
        self._mj.mj_kinematics(self.model, self.data)
        R = self.data.site_xmat[self.site_id].reshape(3, 3).copy()
        p = self.data.site_xpos[self.site_id].copy()
        return make_T(p, R=R)

    def jacobian(self, q: np.ndarray) -> np.ndarray:
        self.data.qpos[: self.n] = q
        self._mj.mj_kinematics(self.model, self.data)
        self._mj.mj_comPos(self.model, self.data)
        jp = np.zeros((3, self.model.nv))
        jr = np.zeros((3, self.model.nv))
        self._mj.mj_jacSite(self.model, self.data, jp, jr, self.site_id)
        return np.vstack([jp[:, : self.n], jr[:, : self.n]])

    def ik(
        self,
        T_target: np.ndarray,
        q0: np.ndarray,
        max_iters: int = 100,
        pos_tol: float = 5e-4,
        rot_tol: float = 2e-3,
        damping: float = 1e-3,
        step_scale: float = 0.8,
        rot_weight: float = 0.3,
        nullspace_gain: float = 0.02,
    ) -> tuple[np.ndarray, bool, float, float]:
        """Damped least-squares IK with joint-limit clamping and a weak null-space pull to `home`.

        Returns (q, converged, pos_err_m, rot_err_rad).
        """
        q = np.clip(np.array(q0, dtype=np.float64), self.lower, self.upper)
        p_t = T_target[:3, 3]
        R_t = T_target[:3, :3]
        pos_err = rot_err = np.inf
        for _ in range(max_iters):
            T = self.fk(q)
            dp = p_t - T[:3, 3]
            dR = mat_to_rotvec(R_t @ T[:3, :3].T)  # world-frame rotation error
            pos_err = float(np.linalg.norm(dp))
            rot_err = float(np.linalg.norm(dR))
            if pos_err < pos_tol and rot_err < rot_tol:
                return q, True, pos_err, rot_err
            err = np.concatenate([dp, rot_weight * dR])
            J = self.jacobian(q)
            J[3:] *= rot_weight
            JJt = J @ J.T + damping * np.eye(6)
            dq = J.T @ np.linalg.solve(JJt, err)
            # null-space: pull toward home posture (keeps elbow-up solutions, avoids flips)
            N = np.eye(self.n) - np.linalg.pinv(J) @ J
            dq += nullspace_gain * (N @ (self.home - q))
            max_step = 0.3
            nrm = np.linalg.norm(dq)
            if nrm > max_step:
                dq *= max_step / nrm
            q = np.clip(q + step_scale * dq, self.lower, self.upper)
        T = self.fk(q)
        pos_err = float(np.linalg.norm(p_t - T[:3, 3]))
        rot_err = float(np.linalg.norm(mat_to_rotvec(R_t @ T[:3, :3].T)))
        return q, (pos_err < pos_tol and rot_err < rot_tol), pos_err, rot_err

    def ik_pose(self, position, quaternion, q0):
        return self.ik(make_T(position, R=quat_to_mat(quaternion)), q0)
