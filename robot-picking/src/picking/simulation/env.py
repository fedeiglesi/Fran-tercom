"""MuJoCo pick-and-place environment for the myCobot 280 with an external fixed camera.

The environment is deliberately "dumb": it owns physics, rendering and privileged state access.
Robot control goes through `picking.robots.sim.SimRobotAdapter`, success/failure logic lives in
`picking.evaluation.evaluator`, and randomization in `picking.simulation.randomization`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from picking.core.transforms import make_T, mat_to_quat, quat_to_mat
from picking.simulation.mjcf import JOINT_NAMES, camera_quat_lookat, scene_xml
from picking.simulation.randomization import EpisodeRandomization, sample_randomization


from picking.utils.gl import select_gl_backend  # noqa: E402,F401


@dataclass
class ContactSummary:
    robot_table: bool = False
    robot_tray: bool = False
    robot_object: bool = False  # arm link (not gripper) touching an object
    palm_object: bool = False  # grasped object pressing against the gripper body: normal, not a collision
    robot_self: bool = False
    finger_object: dict[str, int] = field(default_factory=dict)  # object id -> number of pads in contact
    finger_table: bool = False
    max_force: float = 0.0


class PickPlaceEnv:
    def __init__(self, cfg: dict, render: bool = True):
        select_gl_backend(cfg["sim"].get("gl_backend", "auto"))
        import mujoco

        self.mj = mujoco
        self.cfg = cfg
        self.render_enabled = render
        self.timestep = float(cfg["sim"]["timestep"])
        self.control_hz = float(cfg["sim"]["control_hz"])
        self.substeps = int(round(1.0 / (self.control_hz * self.timestep)))
        self.max_steps = int(cfg["sim"]["max_steps"])
        self.xml = scene_xml(cfg)
        self.model = mujoco.MjModel.from_xml_string(self.xml)
        self.data = mujoco.MjData(self.model)
        self._nominal = {
            "geom_rgba": self.model.geom_rgba.copy(),
            "body_mass": self.model.body_mass.copy(),
            "geom_friction": self.model.geom_friction.copy(),
            "light_diffuse": self.model.light_diffuse.copy(),
            "cam_pos": self.model.cam_pos.copy(),
            "cam_quat": self.model.cam_quat.copy(),
            "actuator_gainprm": self.model.actuator_gainprm.copy(),
            "actuator_biasprm": self.model.actuator_biasprm.copy(),
        }
        self._backlash = None
        self._backlash_offset = np.zeros(6)
        self._last_cmd = None
        self._cmd_queue: list[tuple[np.ndarray, float]] = []
        self._latency = 0
        self._joint_noise = 0.0
        self._noise_rng = np.random.default_rng(0)
        self.cam_name = cfg["camera"]["name"]
        self.cam_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, self.cam_name)
        self.width, self.height = int(cfg["camera"]["width"]), int(cfg["camera"]["height"])
        self.renderer = None
        self.depth_renderer = None
        if render:
            self.renderer = mujoco.Renderer(self.model, self.height, self.width)
            self.depth_renderer = mujoco.Renderer(self.model, self.height, self.width)
            self.depth_renderer.enable_depth_rendering()
        self.joint_ids = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, n) for n in JOINT_NAMES]
        self.qpos_adr = [self.model.jnt_qposadr[j] for j in self.joint_ids]
        self.qvel_adr = [self.model.jnt_dofadr[j] for j in self.joint_ids]
        self.act_ids = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, f"act_{n}") for n in JOINT_NAMES]
        self.grip_act = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, "act_gripper")
        self.finger_left_qadr = self.model.jnt_qposadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, "finger_left")]
        self.min_open = 0.004  # pad half thickness: fingers touch at this joint value
        self.half_open = float(cfg["robot"]["gripper_max_opening"]) / 2 + self.min_open  # joint value fully open
        self.tcp_site = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, "tcp")
        self.tray_site = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, "tray_center")
        self.object_ids = [o["id"] for o in cfg["scene"]["objects"]]
        self.object_body = {o: mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, o) for o in self.object_ids}
        self.object_geom = {o: mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, f"{o}_geom") for o in self.object_ids}
        self.object_qadr = {
            o: self.model.jnt_qposadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"{o}_free")]
            for o in self.object_ids
        }
        self.object_vadr = {
            o: self.model.jnt_dofadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, f"{o}_free")]
            for o in self.object_ids
        }
        # geom classification for contact analysis
        self.geom_table = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, "table")
        self.geom_floor = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, "floor")
        self.geom_tray = {mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, n) for n in ["tray_floor", "tray_w0", "tray_w1", "tray_w2", "tray_w3"]}
        self.bins = {b["id"]: b for b in cfg["scene"].get("bins", [])}
        for bid in self.bins:
            for suffix in ("_floor", "_w0", "_w1", "_w2", "_w3"):
                self.geom_tray.add(mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, bid + suffix))  # walls count as "tray" for collisions
        self.bin_site = {bid: mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, bid + "_center") for bid in self.bins}
        self.dest_bin_id = "tray"  # where the current item must be placed (tray or a bin id)
        self.place_slot = None  # explicit place position (x, y) inside the destination, if chosen
        self.order: list[str] = []  # level 7: SKUs to pick in sequence
        self.order_task = cfg["scene"].get("order_task")
        self.geom_pads = {mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, n): n for n in ["pad_left", "pad_right"]}
        self.geom_gripper_body = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, "gripper_col")
        self.geom_robot = set()
        for g in range(self.model.ngeom):
            name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, g) or ""
            if name.endswith("_col") or name in ("pad_left", "pad_right"):
                self.geom_robot.add(g)
        self.geom_obj_to_id = {g: o for o, g in self.object_geom.items()}
        for g in range(self.model.ngeom):
            b = self.model.geom_bodyid[g]
            for o, bid in self.object_body.items():
                if b == bid:
                    self.geom_obj_to_id[g] = o
        self.home_q = np.deg2rad(np.asarray(cfg["robot"]["home_joints_deg"], dtype=float))
        self.gravity_comp = bool(cfg["robot"].get("gravity_compensation", True))
        self.rng = np.random.default_rng(0)
        self.episode_rand: EpisodeRandomization | None = None
        self.one_object = bool(cfg["scene"].get("one_object_per_episode", False))
        self.parking = np.asarray(cfg["scene"].get("parking_position", [-0.35, 0.0, 0.05]), float)
        self.object_specs = {o["id"]: o for o in cfg["scene"]["objects"]}
        self.current_target = cfg["task"].get("target_object", self.object_ids[0])
        if self.current_target == "random":
            self.current_target = self.object_ids[0]
        self.present_objects = list(self.object_ids)
        self.step_count = 0
        self.sim_time_at_reset = 0.0
        self.last_seed = 0

    # ------------------------------------------------------------------ reset / randomization
    def reset(self, seed: int | None = None, randomize: bool | None = None) -> None:
        mj = self.mj
        if seed is not None:
            self.rng = np.random.default_rng(seed)
            self.last_seed = int(seed)
        rcfg = self.cfg.get("randomization", {})
        enabled = rcfg.get("enabled", False) if randomize is None else randomize
        mj.mj_resetData(self.model, self.data)
        # restore nominal model params then apply randomization
        self.model.geom_rgba[:] = self._nominal["geom_rgba"]
        self.model.body_mass[:] = self._nominal["body_mass"]
        self.model.geom_friction[:] = self._nominal["geom_friction"]
        self.model.light_diffuse[:] = self._nominal["light_diffuse"]
        self.model.cam_pos[:] = self._nominal["cam_pos"]
        self.model.cam_quat[:] = self._nominal["cam_quat"]
        self.episode_rand = sample_randomization(self.cfg, self.rng, enabled)
        er = self.episode_rand
        n_present = int(self.cfg["scene"].get("objects_per_episode", 1))
        self.place_slot = None
        if self.order_task:
            self._reset_order_task(er)
        elif self.one_object or n_present > 1:
            # choose the episode's target (+ distractors with distinct colours); park the others out of sight
            fixed = self.cfg["task"].get("target_object", "random")
            self.current_target = fixed if fixed in self.object_ids else self.object_ids[int(self.rng.integers(len(self.object_ids)))]
            present = [self.current_target]
            if n_present > 1:
                used_colors = {self.object_specs[self.current_target].get("color_name")}
                pool = [o for o in self.object_ids if o != self.current_target]
                self.rng.shuffle(pool)
                for o in pool:
                    c = self.object_specs[o].get("color_name")
                    if len(present) >= n_present:
                        break
                    if c in used_colors:
                        continue  # colour-based perception needs unique colours per scene (level 5 start)
                    used_colors.add(c)
                    present.append(o)
                # non-overlapping positions inside the pick area (away from the tray side)
                area = self.cfg["scene"].get("multi_object_area", {"x": [0.14, 0.24], "y": [-0.02, 0.16]})
                min_sep = float(self.cfg["scene"].get("multi_object_min_separation", 0.06))
                placed = []
                for o in present:
                    for _ in range(200):
                        pos = np.array([self.rng.uniform(*area["x"]), self.rng.uniform(*area["y"])])
                        if all(np.linalg.norm(pos - q) >= min_sep for q in placed):
                            break
                    placed.append(pos)
                    er.objects[o]["position"][:2] = pos
            self.present_objects = present
            for k, oid in enumerate(self.object_ids):
                if oid not in present:
                    er.objects[oid]["position"] = self.parking + np.array([0.0, 0.05 * k - 0.2, 0.0])
        elif self.cfg["task"].get("target_object", "random") != "random":
            self.current_target = self.cfg["task"]["target_object"]
        for oid, spec in er.objects.items():
            self.model.body_mass[self.object_body[oid]] = spec["mass"]
            for g, o2 in self.geom_obj_to_id.items():
                if o2 == oid:
                    self.model.geom_friction[g, 0] = spec["friction"]
            if spec.get("color") is not None:
                for g, o2 in self.geom_obj_to_id.items():
                    if o2 == oid:
                        self.model.geom_rgba[g] = spec["color"]
        mj.mj_setConst(self.model, self.data)  # recompute inertia after mass change (resets qpos -> do it first)
        for oid, spec in er.objects.items():
            adr = self.object_qadr[oid]
            self.data.qpos[adr : adr + 3] = spec["position"]
            yaw = np.deg2rad(spec["yaw_deg"])
            self.data.qpos[adr + 3 : adr + 7] = mat_to_quat(
                np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
            )
        if er.camera_position is not None:
            self.model.cam_pos[self.cam_id] = er.camera_position
            self.model.cam_quat[self.cam_id] = camera_quat_lookat(er.camera_position, er.camera_lookat)
        if er.light_scale is not None:
            self.model.light_diffuse[:] = self._nominal["light_diffuse"] * er.light_scale
        if er.table_color is not None:
            self.model.geom_rgba[self.geom_table] = er.table_color
        # robot imperfections
        self.model.actuator_gainprm[:] = self._nominal["actuator_gainprm"]
        self.model.actuator_biasprm[:] = self._nominal["actuator_biasprm"]
        if er.kp_scale != 1.0:
            for a in self.act_ids:
                self.model.actuator_gainprm[a, 0] *= er.kp_scale
                self.model.actuator_biasprm[a, 1] *= er.kp_scale
        self._backlash = er.backlash_rad
        self._backlash_offset = np.zeros(6)
        self._last_cmd = None
        self._cmd_queue = []
        self._latency = int(er.latency_steps)
        self._joint_noise = float(er.joint_noise_rad)
        self._noise_rng = np.random.default_rng(self.last_seed + 1234)
        # robot at home, gripper open
        for i, adr in enumerate(self.qpos_adr):
            self.data.qpos[adr] = self.home_q[i]
        self.data.qpos[self.finger_left_qadr] = self.half_open
        self.data.ctrl[self.act_ids] = self.home_q
        self.data.ctrl[self.grip_act] = self.half_open
        mj.mj_forward(self.model, self.data)
        # settle objects
        for _ in range(int(0.3 / self.timestep)):
            mj.mj_step(self.model, self.data)
        self.step_count = 0
        self.sim_time_at_reset = self.data.time

    # ------------------------------------------------------------------ control
    def _reset_order_task(self, er) -> None:
        """Level 7: fill the source bin with order items + distractors, pre-fill the order bin, define the order."""
        ot = self.order_task
        src, dst = ot["source_bin"], ot["order_bin"]
        self.dest_bin_id = dst
        n_src, n_pre, n_order = int(ot.get("n_source_items", 5)), int(ot.get("n_prefilled", 2)), int(ot.get("order_size", 2))
        ids = list(self.object_ids)
        self.rng.shuffle(ids)
        order = ids[:n_order]
        distractors = ids[n_order:n_src]
        prefilled = ids[n_src : n_src + n_pre]
        self.order = order
        self.current_target = order[0]
        self.present_objects = order + distractors + prefilled

        wall_margin = float(ot.get("wall_margin", 0.008))  # extra clearance between an item and the bin walls
        separation = float(ot.get("separation", 0.012))  # extra clearance between items

        def place(bid, objs):
            c, hs = self.bin_center_nominal(bid), self.bin_inner_halfsize(bid)
            placed = []
            for o in objs:
                spec = self.object_specs[o]
                r = float(spec.get("footprint_radius", 0.022 if spec["type"] == "compound" else max(spec["size"][:2])))
                pos = c[:2].copy()
                ok = False
                for attempt in range(600):
                    relax = 1.0 if attempt < 300 else 0.6  # if the bin is too crowded, accept tighter spacing
                    lo = np.array([c[0] - hs[0] + r + wall_margin * relax, c[1] - hs[1] + r + wall_margin * relax])
                    hi = np.array([c[0] + hs[0] - r - wall_margin * relax, c[1] + hs[1] - r - wall_margin * relax])
                    pos = np.array([self.rng.uniform(min(lo[0], hi[0]), max(lo[0], hi[0])), self.rng.uniform(min(lo[1], hi[1]), max(lo[1], hi[1]))])
                    if all(np.linalg.norm(pos - q) >= r + rq + separation * relax for q, rq in placed):
                        ok = True
                        break
                if not ok:
                    self.placement_failures = getattr(self, "placement_failures", 0) + 1
                placed.append((pos, r))
                er.objects[o]["position"][:2] = pos
                er.objects[o]["position"][2] = float(er.objects[o]["position"][2]) + 0.004  # on the bin floor

        place(src, order + distractors)
        place(dst, prefilled)
        for k, oid in enumerate(self.object_ids):
            if oid not in self.present_objects:
                er.objects[oid]["position"] = self.parking + np.array([0.0, 0.05 * k - 0.3, 0.0])

    def set_joint_targets(self, q: np.ndarray, gripper: float | None = None) -> None:
        q = np.asarray(q, dtype=np.float64).copy()
        if self._backlash is not None:
            # mechanical play (dead band of half-width b): the output y follows the command x only once x has
            # moved past the band, i.e. y = x - b when pushing up, x + b when pushing down, unchanged otherwise
            if self._last_cmd is None:
                self._last_cmd = q.copy()
            y = self._last_cmd
            y = np.where(q - y > self._backlash, q - self._backlash, np.where(q - y < -self._backlash, q + self._backlash, y))
            self._last_cmd = y
            q = y
        if self._latency > 0:
            # command latency: the target takes effect `latency` control periods later (applied in advance())
            self._cmd_queue.append((self.step_count + self._latency, q))
        else:
            self.data.ctrl[self.act_ids] = q
        if gripper is not None:
            # gripper 0 = open, 1 = closed  -> finger_left target = half_open * (1 - g)
            self.data.ctrl[self.grip_act] = self.min_open + (self.half_open - self.min_open) * (1.0 - float(np.clip(gripper, 0, 1)))

    def advance(self, n_substeps: int | None = None) -> ContactSummary:
        """Advance physics by one control period; returns aggregated contact information over the period."""
        n = self.substeps if n_substeps is None else n_substeps
        summary = ContactSummary()
        while self._cmd_queue and self._cmd_queue[0][0] <= self.step_count:
            self.data.ctrl[self.act_ids] = self._cmd_queue.pop(0)[1]
        for _ in range(n):
            if self.gravity_comp:
                # emulate a well-tuned servo: cancel gravity/Coriolis on the arm joints so the
                # position actuators have no steady-state sag (the real myCobot servos hold position)
                self.data.qfrc_applied[self.qvel_adr] = self.data.qfrc_bias[self.qvel_adr]
            self.mj.mj_step(self.model, self.data)
            self._accumulate_contacts(summary)
        self.step_count += 1
        return summary

    def _accumulate_contacts(self, s: ContactSummary) -> None:
        d, m = self.data, self.model
        f = np.zeros(6)
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = c.geom1, c.geom2
            r1, r2 = g1 in self.geom_robot, g2 in self.geom_robot
            if not (r1 or r2):
                continue
            self.mj.mj_contactForce(m, d, i, f)
            force = abs(f[0])
            s.max_force = max(s.max_force, force)
            if r1 and r2:
                s.robot_self = True
                continue
            rg, og = (g1, g2) if r1 else (g2, g1)
            is_pad = rg in self.geom_pads
            if og == self.geom_table or og == self.geom_floor:
                if is_pad:
                    s.finger_table = True
                else:
                    s.robot_table = True
            elif og in self.geom_tray:
                if is_pad:
                    s.finger_table = True  # compliant pad touching a wall/floor: collision only above the force threshold
                else:
                    s.robot_tray = True
            elif og in self.geom_obj_to_id:
                oid = self.geom_obj_to_id[og]
                if is_pad:
                    s.finger_object[oid] = s.finger_object.get(oid, 0) | (1 if self.geom_pads[rg] == "pad_left" else 2)
                elif rg == self.geom_gripper_body:
                    s.palm_object = True
                else:
                    s.robot_object = True

    # ------------------------------------------------------------------ state access
    def joint_positions(self) -> np.ndarray:
        q = np.array([self.data.qpos[a] for a in self.qpos_adr])
        if self._joint_noise > 0:
            q = q + self._noise_rng.normal(0.0, self._joint_noise, 6)
        return q

    def joint_velocities(self) -> np.ndarray:
        return np.array([self.data.qvel[a] for a in self.qvel_adr])

    def gripper_state(self) -> float:
        """0 = fully open .. 1 = fully closed (from actual finger position)."""
        x = self.data.qpos[self.finger_left_qadr]
        return float(np.clip(1.0 - (x - self.min_open) / (self.half_open - self.min_open), 0.0, 1.0))

    def gripper_opening(self) -> float:
        """Distance between pad inner faces [m]."""
        return float(max(0.0, 2 * (self.data.qpos[self.finger_left_qadr] - self.min_open)))

    def tcp_matrix(self) -> np.ndarray:
        R = self.data.site_xmat[self.tcp_site].reshape(3, 3).copy()
        return make_T(self.data.site_xpos[self.tcp_site].copy(), R=R)

    def object_pose(self, oid: str) -> np.ndarray:
        adr = self.object_qadr[oid]
        p = self.data.qpos[adr : adr + 3].copy()
        q = self.data.qpos[adr + 3 : adr + 7].copy()
        return make_T(p, R=quat_to_mat(q))

    def object_com(self, oid: str) -> np.ndarray:
        """Centre of mass of the object body in the base frame (= body origin for simple primitives)."""
        return self.data.xipos[self.object_body[oid]].copy()

    def object_velocity(self, oid: str) -> np.ndarray:
        adr = self.object_vadr[oid]
        return self.data.qvel[adr : adr + 6].copy()

    def object_half_extent_z(self, oid: str) -> float:
        spec = self.object_specs.get(oid, {})
        if spec.get("type") == "compound":
            return float(spec.get("half_height", 0.01))
        g = self.object_geom[oid]
        t = self.model.geom_type[g]
        size = self.model.geom_size[g]
        if t == self.mj.mjtGeom.mjGEOM_BOX:
            return float(size[2])
        if t in (self.mj.mjtGeom.mjGEOM_CYLINDER, self.mj.mjtGeom.mjGEOM_CAPSULE):
            return float(size[1] + (size[0] if t == self.mj.mjtGeom.mjGEOM_CAPSULE else 0))
        if t == self.mj.mjtGeom.mjGEOM_ELLIPSOID:
            return float(size[2])
        return float(size[0])

    def target_spec(self) -> dict:
        return self.object_specs[self.current_target]

    def instruction(self) -> str:
        t = self.cfg["task"]
        if "instruction_template" in t:
            return t["instruction_template"].format(name=self.target_spec().get("name", self.current_target))
        return t["instruction"]

    def bin_center(self, bid: str) -> np.ndarray:
        if bid == "tray":
            return self.data.site_xpos[self.tray_site].copy()
        return self.data.site_xpos[self.bin_site[bid]].copy()

    def bin_center_nominal(self, bid: str) -> np.ndarray:
        spec = self.cfg["scene"]["tray"] if bid == "tray" else self.bins[bid]
        return np.asarray(spec["position"], float)

    def bin_inner_halfsize(self, bid: str) -> np.ndarray:
        spec = self.cfg["scene"]["tray"] if bid == "tray" else self.bins[bid]
        iw, ih = spec["inner_size"]
        return np.array([iw / 2, ih / 2])

    def bin_footprint(self, bid: str) -> tuple[np.ndarray, np.ndarray]:
        spec = self.cfg["scene"]["tray"] if bid == "tray" else self.bins[bid]
        return self.bin_center(bid)[:2].copy(), self.bin_inner_halfsize(bid) + spec.get("wall_thickness", 0.004)

    def objects_in_bin(self, bid: str) -> list[str]:
        c, hs = self.bin_center(bid), self.bin_inner_halfsize(bid)
        out = []
        for o in getattr(self, "present_objects", self.object_ids):
            p = self.object_pose(o)[:3, 3]
            if np.all(np.abs(p[:2] - c[:2]) <= hs) and p[2] < c[2] + 0.08:
                out.append(o)
        return out

    def free_slot(self, bid: str, grid: int = 2) -> np.ndarray:
        """Place position inside `bid`: the grid cell farthest from the objects already inside."""
        c, hs = self.bin_center(bid), self.bin_inner_halfsize(bid)
        occupied = [self.object_pose(o)[:3, 3][:2] for o in self.objects_in_bin(bid) if o != self.current_target]
        best, best_d = c[:2].copy(), -1.0
        for i in range(grid):
            for j in range(grid):
                cell = c[:2] + np.array([-hs[0] + hs[0] * (2 * i + 1) / grid, -hs[1] + hs[1] * (2 * j + 1) / grid])
                d = min([np.linalg.norm(cell - q) for q in occupied], default=1.0)
                if d > best_d:
                    best, best_d = cell, d
        return np.array([best[0], best[1], c[2]])

    def tray_center(self) -> np.ndarray:
        """Centre of the CURRENT destination (tray or order bin), or the explicit place slot if one was chosen."""
        if self.place_slot is not None:
            return np.array([self.place_slot[0], self.place_slot[1], self.bin_center(self.dest_bin_id)[2]])
        return self.bin_center(self.dest_bin_id)

    def tray_footprint(self) -> tuple[np.ndarray, np.ndarray]:
        """(centre xy, half-size xy incl. walls) of the current destination, for perception masking."""
        return self.bin_footprint(self.dest_bin_id)

    def tray_inner_halfsize(self) -> np.ndarray:
        return self.bin_inner_halfsize(self.dest_bin_id)

    def object_in_tray(self, oid: str, margin: float = 0.0) -> bool:
        """Object rests inside the CURRENT destination container (tray or order bin)."""
        p = self.object_pose(oid)[:3, 3]
        c = self.bin_center(self.dest_bin_id)
        hs = self.tray_inner_halfsize()
        inside_xy = np.all(np.abs(p[:2] - c[:2]) <= hs - margin)
        z_ok = (p[2] - self.object_half_extent_z(oid)) < c[2] + 0.02 and p[2] > c[2] - 0.01
        return bool(inside_xy and z_ok)

    def camera_pose(self) -> np.ndarray:
        """Camera pose in the robot base frame (sim ground truth for calibration tests)."""
        R = self.data.cam_xmat[self.cam_id].reshape(3, 3).copy()
        return make_T(self.data.cam_xpos[self.cam_id].copy(), R=R)

    def camera_intrinsics(self) -> dict:
        fovy = np.deg2rad(self.model.cam_fovy[self.cam_id])
        fy = 0.5 * self.height / np.tan(fovy / 2)
        return {"fx": fy, "fy": fy, "cx": self.width / 2, "cy": self.height / 2, "width": self.width, "height": self.height}

    # ------------------------------------------------------------------ rendering
    def render_rgb(self, camera: str | None = None) -> np.ndarray:
        assert self.renderer is not None, "env created with render=False"
        self.renderer.update_scene(self.data, camera=camera or self.cam_name)
        return self.renderer.render().copy()

    def render_depth(self, camera: str | None = None) -> np.ndarray:
        assert self.depth_renderer is not None
        self.depth_renderer.update_scene(self.data, camera=camera or self.cam_name)
        return self.depth_renderer.render().astype(np.float32)

    def sim_time(self) -> float:
        return float(self.data.time - self.sim_time_at_reset)

    def close(self) -> None:
        if self.renderer is not None:
            self.renderer.close()
            self.depth_renderer.close()
            self.renderer = self.depth_renderer = None
