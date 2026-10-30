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
        transport_gain: float = 0.6,
        min_speed: float = 0.0,
        preshape_margin: float | None = None,
        gripper_max_opening: float = 0.045,
        descend_slow_zone: float = 0.025,
        descend_min_speed: float = 0.1,
    ):
        self.descend_slow_zone = descend_slow_zone
        self.descend_min_speed = descend_min_speed
        self.yaw_scorer = None  # optional callable(obj_pos, yaw) -> clearance score (higher is better)
        self.transport_gain = transport_gain
        self.min_speed = min_speed
        self.preshape_margin = preshape_margin  # None = approach fully open; else opening = object width + margin
        self.gripper_max_opening = gripper_max_opening
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
        self.clean_action = None
        self.lifted_z = None
        self.stall = 0
        self._last_tcp = None
        self.bad_target = False

    def done(self) -> bool:
        return self._done

    def info(self) -> dict:
        return {"retries": self.retries, "phases": self.phase_log, "final_phase": self.phase, "bad_target": self.bad_target}

    # ------------------------------------------------------------------
    def _move(self, tcp: np.ndarray, target: np.ndarray, gripper: float, yaw_target: float | None, R_cur: np.ndarray) -> Action:
        err = target - tcp
        dpos = np.clip(self.gain * err / self.max_dp, -1, 1)
        # speed floor: keep a minimum commanded speed until close to the target so that a learned imitator
        # does not learn to "creep" (vanishing actions near goals stall under small prediction errors)
        n = np.linalg.norm(dpos)
        dist = np.linalg.norm(err)
        if self.min_speed > 0 and 0 < n < self.min_speed and dist > 0.004:
            dpos = dpos / n * min(self.min_speed, dist / self.max_dp)
        drot = np.zeros(3)
        if yaw_target is not None and self.align_yaw:
            # rotate about world z so gripper x axis aligns with object yaw (mod 90 deg for a cube)
            R_des = rotvec_to_mat(np.array([0, 0, yaw_target])) @ rotvec_to_mat(np.array([np.pi, 0, 0]))
            rv = mat_to_rotvec(R_des @ R_cur.T)
            drot = np.clip(rv / self.max_dr, -1, 1)
        self.clean_action = Action(dpos=dpos.copy(), drot=drot.copy(), gripper=gripper)  # DART label
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
            period = float(priv.get("object_yaw_period", np.pi / 2))
            cands = [obj_yaw + k * period for k in range(-2, 3)]
            cands = [y for y in cands if abs(np.angle(np.exp(1j * (y - cur_yaw)))) <= np.pi / 2 + 1e-6] or cands
            if self.yaw_scorer is not None:
                # clutter / bins: prefer the grasp yaw whose pads stay clear of walls and neighbours
                self.grasp_yaw = max(cands, key=lambda y: (round(self.yaw_scorer(obj, y), 3), -abs(np.angle(np.exp(1j * (y - cur_yaw))))))
            else:
                self.grasp_yaw = min(cands, key=lambda y: abs(np.angle(np.exp(1j * (y - cur_yaw)))))
            self.grasp_target = obj.copy()
        ph = self.phase
        g_open = 0.0
        if self.preshape_margin is not None and priv.get("object_width") is not None:
            # pre-shape the gripper to the object width + margin so the pads do not sweep neighbours (clutter)
            opening = min(self.gripper_max_opening, float(priv["object_width"]) + self.preshape_margin)
            g_open = float(np.clip(1.0 - opening / self.gripper_max_opening, 0.0, 0.8))
        self.phase_steps = getattr(self, "phase_steps", 0) + 1
        if ph == "approach":
            tgt = np.array([obj[0], obj[1], grasp_z + self.approach_height])
            a = self._move(tcp, tgt, g_open, self.grasp_yaw, R_cur)
            progress = self._last_tcp is None or np.linalg.norm(tcp - self._last_tcp) > 0.0015
            self.stall = 0 if progress else self.stall + 1
            self._last_tcp = tcp.copy()
            if np.linalg.norm(tgt - tcp) < 0.004:
                self.stall = 0
                self._next("descend")
            elif self.stall >= 10:
                # unreachable target (IK/workspace): give up instead of chasing it until the timeout
                self.bad_target = True
                self._next("failed")
            return a
        if ph == "descend":
            tgt = np.array([obj[0], obj[1], grasp_z])
            a = self._move(tcp, tgt, g_open, self.grasp_yaw, R_cur)
            # decelerate in the last centimetres: with servo lag / latency a full-speed descent overshoots into
            # the table (the setpoint runs ahead of the measured pose). Proportional within `descend_slow_zone`.
            dz = tgt[2] - tcp[2]
            if 1e-4 < abs(dz) < self.descend_slow_zone:
                a.dpos[2] = float(np.sign(dz) * max(abs(dz) / self.descend_slow_zone, self.descend_min_speed))
            # stall detection: if the gripper is blocked (e.g. resting on the object edge) attempt the grasp
            # anyway; VERIFY will catch a bad grasp and trigger re-observation + retry
            progress = self._last_tcp is None or np.linalg.norm(tcp - self._last_tcp) > 0.001
            self.stall = 0 if progress else self.stall + 1
            self._last_tcp = tcp.copy()
            err = np.linalg.norm(tgt - tcp)
            # the pads are 40 mm tall: +-6 mm in height is fine. With lag the TCP oscillates around the target,
            # so also close when it has been hovering within 1 cm for a while instead of hammering the object.
            if err < 0.006 or self.stall >= 6 or (abs(dz) < 0.010 and self.phase_steps > 30):
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
            a = self._move(tcp, tgt, 1.0, 0.0, R_cur)  # yaw back to neutral: keeps IK well-conditioned near the tray
            a.dpos *= self.transport_gain  # move slower while holding (less inertial load on the grasp)
            progress = self._last_tcp is None or np.linalg.norm(tcp - self._last_tcp) > 0.001
            self.stall = 0 if progress else self.stall + 1
            self._last_tcp = tcp.copy()
            exy = np.linalg.norm(tgt[:2] - tcp[:2])
            if exy < 0.006 or (exy < 0.015 and (self.stall >= 4 or self.phase_steps > 40)):
                self.stall = 0
                self._next("verify2")
            return a
        if ph == "verify2":
            # VERIFY again after transport: object still held? else re-observe and retry (recovery)
            holding = g_state < 0.97 and self._still_holding(obs)
            if holding:
                self._next("lower")
            else:
                self.retries += 1
                if self.retries > self.max_retries:
                    self._next("failed")
                else:
                    self.grasp_target = None
                    self._next("approach")
            return self._move(tcp, tcp, 1.0 if holding else 0.0, None, R_cur)
        if ph == "lower":
            tgt = np.array([tray[0], tray[1], tray[2] + self.place_height])
            a = self._move(tcp, tgt, 1.0, 0.0, R_cur)
            dz = tgt[2] - tcp[2]
            if 1e-4 < abs(dz) < self.descend_slow_zone:
                a.dpos[2] = float(np.sign(dz) * max(abs(dz) / self.descend_slow_zone, self.descend_min_speed))
            # with servo lag the last millimetres may never be reached: release once close enough and stalled
            progress = self._last_tcp is None or np.linalg.norm(tcp - self._last_tcp) > 0.001
            self.stall = 0 if progress else self.stall + 1
            self._last_tcp = tcp.copy()
            err = np.linalg.norm(tgt - tcp)
            # 8 mm is enough to release over the tray; with lag/oscillation also give up after a step budget
            if err < 0.008 or (err < 0.015 and (self.stall >= 4 or self.phase_steps > 25)):
                self.stall = 0
                self._next("release")
            return a
        if ph == "release":
            self.wait += 1
            g_rel = 0.0
            if self.preshape_margin is not None and priv.get("object_width") is not None:
                opening = min(self.gripper_max_opening, float(priv["object_width"]) + 2 * self.preshape_margin)
                g_rel = float(np.clip(1.0 - opening / self.gripper_max_opening, 0.0, 0.8))
            a = self._move(tcp, tcp, g_rel, None, R_cur)
            if self.wait >= 3:
                self.wait = 0
                self._next("retreat")
            return a
        if ph == "retreat":
            tgt = np.array([tray[0], tray[1], tray[2] + self.place_height + 0.06])
            g_rel = 0.0
            if self.preshape_margin is not None and priv.get("object_width") is not None:
                opening = min(self.gripper_max_opening, float(priv["object_width"]) + 2 * self.preshape_margin)
                g_rel = float(np.clip(1.0 - opening / self.gripper_max_opening, 0.0, 0.8))
            a = self._move(tcp, tgt, g_rel, None, R_cur)
            if abs(tgt[2] - tcp[2]) < 0.006:
                self._next("done")
                self._done = True
            return a
        if ph == "failed":
            self._done = True
            return Action.zero(0.0)
        return Action.zero(0.0)

    def _still_holding(self, obs: Observation) -> bool:
        priv = obs.extra["privileged"]
        obj = np.asarray(priv["object_pos"], float)
        return bool(np.linalg.norm(obj[:2] - obs.tcp_pose[:2]) < 0.03 and obj[2] > self.grasp_target[2] + 0.03)

    def _next(self, phase: str) -> None:
        self.phase_log.append(phase)
        self.phase = phase
        self.phase_steps = 0
