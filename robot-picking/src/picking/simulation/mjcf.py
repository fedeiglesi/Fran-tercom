"""Programmatic MJCF generation for the myCobot 280 M5 pick-and-place scene.

Kinematic chain, joint limits and link frames are transcribed from the official URDF
(elephantrobotics/mycobot_ros: mycobot_description/urdf/mycobot_280_m5/mycobot_280_m5.urdf).
Visual meshes are the official DAE files converted to OBJ (scripts/convert_dae_to_obj.py).
The gripper is a simplified parallel gripper (two sliding pads) whose geometry approximates the
adaptive gripper; its real dimensions must be measured on hardware (docs/HARDWARE.md).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from picking.core.transforms import euler_xyz_to_mat, mat_to_quat

ASSETS = Path(__file__).parent / "assets"
MESHES = ASSETS / "meshes"

# (name, pos xyz, rpy) of each child body relative to its parent, from the URDF joint origins.
# joint i rotates body i+1 about its local z axis.
URDF_CHAIN = [
    ("link2", [0, 0, 0.13156], [0, 0, 0]),
    ("link3", [0, 0, 0], [0, 1.5708, -1.5708]),
    ("link4", [-0.1104, 0, 0], [0, 0, 0]),
    ("link5", [-0.096, 0, 0.06462], [0, 0, -1.5708]),
    ("link6", [0, -0.07318, 0], [1.5708, -1.5708, 0]),
    ("flange", [0, 0.0456, 0], [-1.5708, 0, 0]),
]
# joint limits (rad) from the official URDF; conservative vs. the ±165° marketing figure.
JOINT_LIMITS = [
    (-2.9321, 2.9321),
    (-2.4434, 2.4434),
    (-2.6179, 2.6179),
    (-2.6179, 2.6179),
    (-2.7052, 2.7925),
    (-3.14, 3.14159),
]
JOINT_NAMES = [f"joint{i}" for i in range(1, 7)]
# visual mesh origins (pos, rpy) per link, from the URDF <visual><origin>
MESH_VISUALS = {
    "link1": ("joint1", [0, 0, 0], [0, 0, -1.5708]),
    "link2": ("joint2", [0, 0, -0.06096], [0, 0, -1.5708]),
    "link3": ("joint3", [0, 0, 0.03256], [0, -1.5708, 0]),
    "link4": ("joint4", [0, 0, 0.03056], [0, -1.5708, 0]),
    "link5": ("joint5", [0, 0, -0.03356], [-1.5708, 0, 0]),
    "link6": ("joint6", [0, 0, -0.038], [0, 0, 0]),
    "flange": ("joint7", [0, 0, -0.012], [0, 0, 0]),
}
# approximate link masses (kg): total arm ~0.86 kg per datasheet, distributed.
LINK_MASS = {"link1": 0.0, "link2": 0.16, "link3": 0.18, "link4": 0.16, "link5": 0.12, "link6": 0.10, "flange": 0.04}


def _q(rpy) -> str:
    return " ".join(f"{v:.6f}" for v in mat_to_quat(euler_xyz_to_mat(*rpy)))


def _v(x) -> str:
    return " ".join(f"{float(v):.6f}" for v in x)


def camera_quat_lookat(pos, lookat, up=(0, 0, 1)) -> np.ndarray:
    """MuJoCo camera looks along its -z axis, +y is up in the image."""
    pos = np.asarray(pos, float)
    lookat = np.asarray(lookat, float)
    fwd = lookat - pos
    fwd /= np.linalg.norm(fwd)
    z = -fwd
    x = np.cross(np.asarray(up, float), z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z], axis=1)
    return mat_to_quat(R)


def robot_xml(cfg: dict, with_gripper: bool = True, kinematic_only: bool = False) -> str:
    """Body tree of the robot, to be placed in <worldbody>."""
    kp = cfg.get("actuator_kp", 60.0)
    damping = cfg.get("joint_damping", 1.5)
    tcp = cfg.get("tcp_offset", [0, 0, 0.065])
    pad_fric = cfg.get("gripper_pad_friction", 2.0)
    max_open = cfg.get("gripper_max_opening", 0.045)  # INNER opening between pad faces when fully open
    half_open = max_open / 2 + 0.004  # finger joint value (pad centre); pads are 4 mm half-thick
    col = "" if kinematic_only else 'class="arm_col"'
    vis = 'class="arm_vis"'

    def mesh_geom(link):
        m, p, rpy = MESH_VISUALS[link]
        return f'<geom {vis} mesh="{m}" pos="{_v(p)}" quat="{_q(rpy)}"/>'

    def col_geom(link):
        if kinematic_only:
            return ""
        m, p, rpy = MESH_VISUALS[link]
        return f'<geom {col} name="{link}_col" mesh="{m}" pos="{_v(p)}" quat="{_q(rpy)}"/>'

    body_open = []
    body_open.append('<body name="link1" pos="0 0 0">')
    body_open.append(f'<geom {vis} mesh="joint1" pos="0 0 0" quat="{_q([0, 0, -1.5708])}"/>')
    body_open.append(
        '<geom class="arm_col" name="link1_col" type="cylinder" size="0.045 0.036" pos="0 0 0.036"/>'
        if not kinematic_only
        else ""
    )
    names = ["link2", "link3", "link4", "link5", "link6", "flange"]
    for i, (name, pos, rpy) in enumerate(URDF_CHAIN):
        lo, hi = JOINT_LIMITS[i]
        body_open.append(f'<body name="{name}" pos="{_v(pos)}" quat="{_q(rpy)}">')
        body_open.append(
            f'<joint name="joint{i+1}" type="hinge" axis="0 0 1" range="{lo:.4f} {hi:.4f}" '
            f'damping="{damping}" armature="0.01" frictionloss="0.05"/>'
        )
        body_open.append(f'<inertial pos="0 0 0" mass="{LINK_MASS[name]}" diaginertia="2e-4 2e-4 1e-4"/>')
        body_open.append(mesh_geom(name))
        body_open.append(col_geom(name))
    # flange frame = joint6 output; TCP site below it
    body_open.append('<site name="flange" pos="0 0 0" size="0.003" rgba="0 1 0 0.5"/>')
    body_open.append(f'<site name="tcp" pos="{_v(tcp)}" size="0.004" rgba="1 0 1 0.6"/>')
    if with_gripper:
        # simplified parallel gripper: base block + two pads sliding along flange x
        body_open.append(
            '<body name="gripper" pos="0 0 0.013">'
            f'<inertial pos="0 0 0.02" mass="0.08" diaginertia="2e-5 2e-5 2e-5"/>'
            f'<geom {vis} type="box" size="0.022 0.014 0.014" pos="0 0 0" rgba="0.92 0.92 0.92 1"/>'
            + (
                f'<geom class="arm_col" name="gripper_col" type="box" size="0.022 0.014 0.014" pos="0 0 0"/>'
                if not kinematic_only
                else ""
            )
        )
        for side, sign in (("left", 1), ("right", -1)):
            lo, hi = (0.004, half_open) if sign > 0 else (-half_open, -0.004)
            body_open.append(
                f'<body name="finger_{side}" pos="0 0 0.014">'
                f'<joint name="finger_{side}" type="slide" axis="1 0 0" range="{lo:.4f} {hi:.4f}" '
                f'damping="2.0" armature="0.005"/>'
                f'<inertial pos="0 0 0.02" mass="0.02" diaginertia="5e-6 5e-6 5e-6"/>'
                f'<geom {vis} type="box" size="0.004 0.008 0.02" pos="{-sign*0.0:.4f} 0 0.018" rgba="0.2 0.2 0.2 1"/>'
                + (
                    f'<geom class="pad_col" name="pad_{side}" type="box" size="0.004 0.008 0.02" '
                    f'pos="0 0 0.018" friction="{pad_fric} 0.02 0.001"/>'
                    if not kinematic_only
                    else ""
                )
                + "</body>"
            )
        body_open.append("</body>")  # gripper
    closes = "</body>" * (len(names) + 1)
    actuators = "\n".join(
        f'<position name="act_{JOINT_NAMES[i]}" joint="{JOINT_NAMES[i]}" kp="{kp}" '
        f'ctrlrange="{JOINT_LIMITS[i][0]:.4f} {JOINT_LIMITS[i][1]:.4f}" forcerange="-15 15"/>'
        for i in range(6)
    )
    if with_gripper:
        actuators += (
            f'\n<position name="act_gripper" joint="finger_left" kp="{cfg.get("gripper_kp", 400.0)}" ctrlrange="0.004 {half_open:.4f}" '
            'forcerange="-8 8"/>'
        )
        equality = (
            '<equality><joint name="finger_mirror" joint1="finger_right" joint2="finger_left" '
            'polycoef="0 -1 0 0 0"/></equality>'
        )
    else:
        equality = ""
    excludes = (
        "<contact>"
        + "".join(
            f'<exclude body1="{a}" body2="{b}"/>'
            for a, b in [
                ("link1", "link2"), ("link2", "link3"), ("link3", "link4"), ("link4", "link5"),
                ("link5", "link6"), ("link6", "flange"), ("flange", "gripper"), ("link1", "link3"),
                ("gripper", "finger_left"), ("gripper", "finger_right"), ("finger_left", "finger_right"),
                ("link6", "gripper"), ("link5", "gripper"),
            ]
        )
        + "</contact>"
    )
    return "\n".join(body_open) + closes, actuators, equality, excludes


def mesh_assets() -> str:
    names = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint7"]
    return "\n".join(f'<mesh name="{n}" file="{(MESHES / (n + ".obj")).as_posix()}"/>' for n in names)


def scene_xml(cfg: dict, object_overrides: dict | None = None, camera_override: dict | None = None) -> str:
    """Full scene: robot on a table, tray, objects, external fixed camera, lights."""
    rcfg = cfg["robot"]
    scfg = cfg["scene"]
    ccfg = dict(cfg["camera"])
    if camera_override:
        ccfg.update(camera_override)
    ts = cfg["sim"]["timestep"]
    bodies, actuators, equality, excludes = robot_xml(rcfg)
    ts_x, ts_y, ts_z = scfg["table_size"]
    tray = scfg["tray"]
    tx, ty, tz = tray["position"]
    iw, ih = tray["inner_size"]
    wh, wt = tray["wall_height"], tray["wall_thickness"]
    tray_rgba = _v(tray["color"])
    tray_geoms = (
        f'<geom name="tray_floor" type="box" size="{iw/2+wt:.4f} {ih/2+wt:.4f} 0.002" pos="0 0 0.002" rgba="{tray_rgba}"/>'
        f'<geom name="tray_w0" type="box" size="{wt:.4f} {ih/2+wt:.4f} {wh/2:.4f}" pos="{iw/2+wt/2:.4f} 0 {wh/2:.4f}" rgba="{tray_rgba}"/>'
        f'<geom name="tray_w1" type="box" size="{wt:.4f} {ih/2+wt:.4f} {wh/2:.4f}" pos="{-(iw/2+wt/2):.4f} 0 {wh/2:.4f}" rgba="{tray_rgba}"/>'
        f'<geom name="tray_w2" type="box" size="{iw/2+wt:.4f} {wt:.4f} {wh/2:.4f}" pos="0 {ih/2+wt/2:.4f} {wh/2:.4f}" rgba="{tray_rgba}"/>'
        f'<geom name="tray_w3" type="box" size="{iw/2+wt:.4f} {wt:.4f} {wh/2:.4f}" pos="0 {-(ih/2+wt/2):.4f} {wh/2:.4f}" rgba="{tray_rgba}"/>'
    )
    objs = []
    for o in scfg["objects"]:
        ov = (object_overrides or {}).get(o["id"], {})
        pos = ov.get("position", o["position"])
        yaw = np.deg2rad(ov.get("yaw_deg", o.get("yaw_deg", 0.0)))
        rgba = _v(ov.get("color", o["color"]))
        mass = ov.get("mass", o["mass"])
        fric = ov.get("friction", o.get("friction", 1.0))
        q = _q([0, 0, yaw])
        if o["type"] == "box":
            g = f'<geom name="{o["id"]}_geom" type="box" size="{_v(o["size"])}" rgba="{rgba}" friction="{fric} 0.01 0.0005" mass="{mass}"/>'
        elif o["type"] == "cylinder":
            g = f'<geom name="{o["id"]}_geom" type="cylinder" size="{_v(o["size"][:2])}" rgba="{rgba}" friction="{fric} 0.01 0.0005" mass="{mass}"/>'
        elif o["type"] == "sphere":
            g = f'<geom name="{o["id"]}_geom" type="sphere" size="{o["size"][0]:.4f}" rgba="{rgba}" friction="{fric} 0.01 0.0005" mass="{mass}"/>'
        elif o["type"] == "capsule":
            g = f'<geom name="{o["id"]}_geom" type="capsule" size="{_v(o["size"][:2])}" rgba="{rgba}" friction="{fric} 0.01 0.0005" mass="{mass}"/>'
        else:
            raise ValueError(f"unknown object type {o['type']}")
        objs.append(
            f'<body name="{o["id"]}" pos="{_v(pos)}" quat="{q}"><freejoint name="{o["id"]}_free"/>{g}</body>'
        )
    cam_q = camera_quat_lookat(ccfg["position"], ccfg["lookat"])
    light_dir = ccfg.get("light_diffuse", 0.7)
    xml = f"""
<mujoco model="mycobot280_pick_place">
  <compiler angle="radian" autolimits="true"/>
  <option timestep="{ts}" integrator="implicitfast" cone="elliptic" impratio="10" noslip_iterations="3"/>
  <visual>
    <global offwidth="{ccfg['width']}" offheight="{ccfg['height']}"/>
    <quality shadowsize="2048"/>
    <headlight ambient="0.35 0.35 0.35" diffuse="0.5 0.5 0.5" specular="0.1 0.1 0.1"/>
  </visual>
  <default>
    <default class="arm_vis"><geom type="mesh" contype="0" conaffinity="0" group="1" rgba="0.95 0.95 0.95 1"/></default>
    <default class="arm_col"><geom type="mesh" contype="1" conaffinity="1" group="3" rgba="0.5 0.5 1 0.3" margin="0.0005"/></default>
    <default class="pad_col"><geom contype="1" conaffinity="1" group="3" condim="6" solimp="0.95 0.99 0.001" solref="0.004 1" margin="0.0005" priority="2"/></default>
  </default>
  <asset>
    {mesh_assets()}
    <texture name="grid" type="2d" builtin="checker" rgb1="0.2 0.2 0.2" rgb2="0.3 0.3 0.3" width="256" height="256"/>
    <material name="floor_mat" texture="grid" texrepeat="4 4" reflectance="0.0"/>
    <material name="table_mat" rgba="{_v(scfg['table_color'])}" reflectance="0.05"/>
  </asset>
  <worldbody>
    <light name="top" pos="0.3 0.2 1.5" dir="-0.2 -0.1 -1" diffuse="{light_dir} {light_dir} {light_dir}" specular="0.2 0.2 0.2" castshadow="true"/>
    <light name="fill" pos="-0.5 -0.5 1.0" dir="0.4 0.4 -1" diffuse="0.3 0.3 0.3" castshadow="false"/>
    <geom name="floor" type="plane" size="2 2 0.1" pos="0 0 -0.75" material="floor_mat"/>
    <geom name="table" type="box" size="{ts_x/2:.4f} {ts_y/2:.4f} {ts_z/2:.4f}" pos="{ts_x/2 - 0.12:.4f} 0 {-ts_z/2:.4f}" material="table_mat" friction="0.8 0.01 0.0005"/>
    <body name="tray" pos="{tx:.4f} {ty:.4f} {tz:.4f}">{tray_geoms}<site name="tray_center" pos="0 0 0.004" size="0.003" rgba="0 0 1 0.3"/></body>
    {"".join(objs)}
    <camera name="{ccfg['name']}" pos="{_v(ccfg['position'])}" quat="{_v(cam_q)}" fovy="{ccfg['fovy_deg']}"/>
    <camera name="top_debug" pos="0.2 0 0.8" quat="1 0 0 0" fovy="50"/>
    {bodies}
  </worldbody>
  {excludes}
  {equality}
  <actuator>
    {actuators}
  </actuator>
  <sensor>
    <framepos name="tcp_pos" objtype="site" objname="tcp"/>
    <framequat name="tcp_quat" objtype="site" objname="tcp"/>
  </sensor>
</mujoco>
"""
    return xml


def kinematic_model_xml(rcfg: dict) -> str:
    """Robot-only model (no contacts, no gravity) used for FK/IK on sim and real robot alike."""
    bodies, _, _, _ = robot_xml(rcfg, with_gripper=False, kinematic_only=True)
    return f"""
<mujoco model="mycobot280_kinematic">
  <compiler angle="radian" autolimits="true"/>
  <option gravity="0 0 0"/>
  <default>
    <default class="arm_vis"><geom type="mesh" contype="0" conaffinity="0" group="1"/></default>
  </default>
  <asset>{mesh_assets()}</asset>
  <worldbody>{bodies}</worldbody>
</mujoco>
"""
