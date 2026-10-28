"""Baseline 0: scripted pick-and-place with privileged object pose (simulation only).

Purpose: validate simulator, action API, safety filter, evaluator, dataset recording and the
SEE -> ACT -> VERIFY -> RECOVER loop. It is also the demonstration generator for imitation learning.
Observations carry the privileged info in obs.extra["privileged"] = {object_pos, object_yaw, tray_pos}.
"""
from __future__ import annotations

import numpy as np

from picking.core.transforms import mat_to_rotvec, quat_to_mat, rotvec_to_mat
from picking.core.types import Action, Observation
from picking.policies.base import Policy


class ScriptedPickPlace(Policy):
    name = "heuristic_scripted"
    version = "1.1"
    needs_images = False

    def __init__(
        self,
        max_delta_pos: float,
        max_delta_rot: float,
        approach_height: float = 0.06,
        grasp_height_offset: float = 0.0,
        lift_height: float = 0.08,
        place_height: float = 0.03,
        max_retries: int = 2,
        gain: float = 1.0,
        noise_std: float = 0.0,
        align_yaw: bool = True,
        rng: np.random.Generator | None = None,
    ):
        self.max_dp = max_delta_pos
        self.max_dr = max_delta_rot
        self.approach_height = approach_height
        self.grasp_offset = grasp_height_offset
        self.lift_height = lift_height
        self.place_height = place_height
        self.max_retries = max_retries
        self.gain = gain
        self.noise_std = noise_std
        self.align_yaw = align_yaw
        self.rng = rng or np.random.default_rng(0)
        self.reset()

    def reset(self, instruction: str = "", task_info: dict | None = None) -> None:
        self.phase = "approach"
        self.retries = 0
        self.wait = 0
        self.grasp_target = None
        self.grasp_yaw = 0.0
        self._done = False
        self.phase_log: list[str] = []
        self.lifted_z = None
        self.stall = 0
        self._last_tcp = None

    def done(self) -> bool:
        return self._done

    def info(self) -> dict:
        return {"retries": self.retries, "phases": self.phase_log, "final_phase": self.phase}

    # ------------------------------------------------------------------
    def _move(self, tcp: np.ndarray, target: np.ndarray, gripper: float, yaw_target: float | None, R_cur: np.ndarray) -> Action:
        err = target - tcp
        dpos = np.clip(self.gain * err / self.max_dp, -1, 1)
        drot = np.zeros(3)
        if yaw_target is not None and self.align_yaw:
            # rotate about world z so gripper x axis aligns with object yaw (mod 90 deg for a cube)
            R_des = rotvec_to_mat(np.array([0, 0, yaw_target])) @ rotvec_to_mat(np.array([np.pi, 0, 0]))
            rv = mat_to_rotvec(R_des @ R_cur.T)
            drot = np.clip(rv / self.max_dr, -1, 1)
        if self.noise_std > 0:
            dpos = np.clip(dpos + self.rng.normal(0, self.noise_std, 3), -1, 1)
        return Action(dpos=dpos, drot=drot, gripper=gripper)

    def act(self, obs: Observation) -> Action:
        priv = obs.extra["privileged"]
        tcp = obs.tcp_pose[:3]
        R_cur = quat_to_mat(obs.tcp_pose[3:7])
        obj = np.asarray(priv["object_pos"], float)
        obj_yaw = float(priv.get("object_yaw", 0.0))
        tray = np.asarray(priv["tray_pos"], float)
        g_state = obs.gripper
        grasp_z = obj[2] + self.grasp_offset
        # choose grasp yaw closest to current gripper yaw (cube symmetric every 90 deg)
        if self.grasp_target is None:
            cur_yaw = np.arctan2(R_cur[1, 0], R_cur[0, 0])
            cands = [obj_yaw + k * np.pi / 2 for k in range(-2, 3)]
            self.grasp_yaw = min(cands, key=lambda y: abs(np.angle(np.exp(1j * (y - cur_yaw)))))
            self.grasp_target = obj.copy()
        ph = self.phase
        if ph == "approach":
            tgt = np.array([obj[0], obj[1], grasp_z + self.approach_height])
            a = self._move(tcp, tgt, 0.0, self.grasp_yaw, R_cur)
            if np.linalg.norm(tgt - tcp) < 0.004:
                self._next("descend")
            return a
        if ph == "descend":
            tgt = np.array([obj[0], obj[1], grasp_z])
            a = self._move(tcp, tgt, 0.0, self.grasp_yaw, R_cur)
            # stall detection: if the gripper is blocked (e.g. resting on the object edge) attempt the grasp
            # anyway; VERIFY will catch a bad grasp and trigger re-observation + retry
            progress = self._last_tcp is None or np.linalg.norm(tcp - self._last_tcp) > 0.001
            self.stall = 0 if progress else self.stall + 1
            self._last_tcp = tcp.copy()
            if np.linalg.norm(tgt - tcp) < 0.003 or self.stall >= 6:
                self.stall = 0
                self._next("close")
            return a
        if ph == "close":
            self.wait += 1
            a = self._move(tcp, tcp, 1.0, None, R_cur)
            if self.wait >= 4:
                self.wait = 0
                self._next("lift")
            return a
        base_z = (self.grasp_target[2] if self.grasp_target is not None else obj[2]) + self.grasp_offset
        if ph == "lift":
            tgt = np.array([tcp[0], tcp[1], base_z + self.lift_height])
            a = self._move(tcp, tgt, 1.0, None, R_cur)
            if abs(tgt[2] - tcp[2]) < 0.005:
                self._next("verify")
            return a
        if ph == "verify":
            # VERIFY: gripper must not be fully closed and the object must have risen with the TCP
            obj_lifted = obj[2] > self.grasp_target[2] + 0.03
            holding = g_state < 0.97 and obj_lifted
            if holding:
                self._next("transport")
            else:
                self.retries += 1
                if self.retries > self.max_retries:
                    self._next("failed")
                else:
                    self.grasp_target = None  # re-observe and retry
                    self._next("approach")
            return self._move(tcp, tcp, 1.0 if holding else 0.0, None, R_cur)
        if ph == "transport":
            tgt = np.array([tray[0], tray[1], base_z + self.lift_height])
            a = self._move(tcp, tgt, 1.0, None, R_cur)
            if np.linalg.norm(tgt[:2] - tcp[:2]) < 0.006:
                self._next("lower")
            return a
        if ph == "lower":
            tgt = np.array([tray[0], tray[1], tray[2] + self.place_height])
            a = self._move(tcp, tgt, 1.0, None, R_cur)
            if np.linalg.norm(tgt - tcp) < 0.004:
                self._next("release")
            return a
        if ph == "release":
            self.wait += 1
            a = self._move(tcp, tcp, 0.0, None, R_cur)
            if self.wait >= 3:
                self.wait = 0
                self._next("retreat")
            return a
        if ph == "retreat":
            tgt = np.array([tray[0], tray[1], tray[2] + self.place_height + 0.06])
            a = self._move(tcp, tgt, 0.0, None, R_cur)
            if abs(tgt[2] - tcp[2]) < 0.006:
                self._next("done")
                self._done = True
            return a
        if ph == "failed":
            self._done = True
            return Action.zero(0.0)
        return Action.zero(0.0)

    def _next(self, phase: str) -> None:
        self.phase_log.append(phase)
        self.phase = phase
