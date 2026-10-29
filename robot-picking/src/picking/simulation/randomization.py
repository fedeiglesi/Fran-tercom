"""Domain randomization with physically plausible ranges (configured per curriculum level)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class EpisodeRandomization:
    objects: dict[str, dict] = field(default_factory=dict)
    camera_position: np.ndarray | None = None
    camera_lookat: np.ndarray | None = None
    light_scale: float | None = None
    table_color: np.ndarray | None = None
    # robot (servo) imperfections, inspired by the Open Duck sim2real work: backlash, latency, sensing noise, stiffness
    backlash_rad: np.ndarray | None = None  # per-joint dead band half-width
    latency_steps: int = 0  # control-period delays before a commanded target takes effect
    joint_noise_rad: float = 0.0  # gaussian noise on joint readings
    kp_scale: float = 1.0  # actuator stiffness multiplier

    def to_dict(self) -> dict:
        return {
            "objects": {k: {kk: (vv.tolist() if isinstance(vv, np.ndarray) else vv) for kk, vv in v.items()} for k, v in self.objects.items()},
            "camera_position": None if self.camera_position is None else self.camera_position.tolist(),
            "light_scale": self.light_scale,
            "table_color": None if self.table_color is None else self.table_color.tolist(),
            "backlash_deg": None if self.backlash_rad is None else np.rad2deg(self.backlash_rad).round(3).tolist(),
            "latency_steps": self.latency_steps,
            "joint_noise_deg": float(np.rad2deg(self.joint_noise_rad)),
            "kp_scale": self.kp_scale,
        }


def sample_randomization(cfg: dict, rng: np.random.Generator, enabled: bool) -> EpisodeRandomization:
    rc = cfg.get("randomization", {})
    er = EpisodeRandomization()
    for o in cfg["scene"]["objects"]:
        pos = np.array(o["position"], dtype=float)
        yaw = float(o.get("yaw_deg", 0.0))
        mass = float(o["mass"])
        fric = float(o.get("friction", 1.0))
        color = None
        if enabled:
            rx, ry = rc.get("object_xy_range", [0, 0])
            pos[0] += rng.uniform(-rx, rx)
            pos[1] += rng.uniform(-ry, ry)
            yr = rc.get("object_yaw_range_deg", 0.0)
            yaw += rng.uniform(-yr, yr)
            m0, m1 = rc.get("object_mass_range", [mass, mass])
            mass = float(rng.uniform(m0, m1))
            f0, f1 = rc.get("object_friction_range", [fric, fric])
            fric = float(rng.uniform(f0, f1))
            cn = rc.get("color_noise", 0.0)
            if cn > 0:
                c = np.array(o["color"], dtype=float)
                c[:3] = np.clip(c[:3] + rng.uniform(-cn, cn, 3), 0, 1)
                color = c
        er.objects[o["id"]] = {"position": pos, "yaw_deg": yaw, "mass": mass, "friction": fric, "color": color}
    if enabled:
        cpn = rc.get("camera_pos_noise", 0.0)
        crn = np.deg2rad(rc.get("camera_rot_noise_deg", 0.0))
        if cpn > 0 or crn > 0:
            cam = cfg["camera"]
            pos = np.array(cam["position"], dtype=float) + rng.uniform(-cpn, cpn, 3)
            look = np.array(cam["lookat"], dtype=float)
            dist = np.linalg.norm(look - pos)
            look = look + rng.uniform(-crn, crn, 3) * dist
            er.camera_position, er.camera_lookat = pos, look
        ln = rc.get("light_noise", 0.0)
        if ln > 0:
            er.light_scale = float(rng.uniform(1 - ln, 1 + ln))
        rob = rc.get("robot", {})
        if rob:
            b = float(rob.get("backlash_deg_max", 0.0))
            if b > 0:
                er.backlash_rad = np.deg2rad(rng.uniform(0.0, b, 6))
            lmax = int(rob.get("latency_steps_max", 0))
            if lmax > 0:
                er.latency_steps = int(rng.integers(0, lmax + 1))
            er.joint_noise_rad = float(np.deg2rad(rng.uniform(0.0, float(rob.get("joint_noise_deg_max", 0.0)))))
            k0, k1 = rob.get("kp_scale_range", [1.0, 1.0])
            er.kp_scale = float(rng.uniform(k0, k1))
        tn = rc.get("table_color_noise", 0.0)
        if tn > 0:
            c = np.array(cfg["scene"]["table_color"], dtype=float)
            c[:3] = np.clip(c[:3] + rng.uniform(-tn, tn, 3), 0, 1)
            er.table_color = c
    return er
