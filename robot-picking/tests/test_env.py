import numpy as np

from picking.core.transforms import make_T, rotvec_to_mat
from picking.evaluation.runner import run_episode
from picking.policies.heuristic import ScriptedPickPlace
from picking.robots.sim import SimRobotAdapter
from picking.safety.filter import SafetyFilter


def test_reset_is_deterministic_and_randomized(env):
    env.reset(seed=5)
    p1 = env.object_pose("red_cube")[:3, 3].copy()
    env.reset(seed=5)
    p2 = env.object_pose("red_cube")[:3, 3].copy()
    env.reset(seed=6)
    p3 = env.object_pose("red_cube")[:3, 3].copy()
    assert np.allclose(p1, p2, atol=1e-6)
    assert np.linalg.norm(p1[:2] - p3[:2]) > 1e-3


def test_camera_rgb_depth(env):
    env.reset(seed=0)
    rgb = env.render_rgb()
    d = env.render_depth()
    assert rgb.shape == (env.height, env.width, 3) and rgb.dtype == np.uint8
    assert d.shape == (env.height, env.width) and d.min() > 0.1 and np.isfinite(d).all()
    intr = env.camera_intrinsics()
    assert intr["fx"] > 0 and intr["width"] == env.width


def test_robot_state_and_gripper(env):
    env.reset(seed=0)
    ad = SimRobotAdapter(env)
    ad.reset_targets()
    s = ad.get_state()
    assert s.joints.shape == (6,) and s.gripper < 0.05
    ad.move_to_joints(env.home_q, gripper=1.0, blocking=True)
    assert ad.get_state().gripper > 0.95


def test_collision_detected_when_pushing_into_table(env):
    env.reset(seed=0)
    ad = SimRobotAdapter(env)
    ad.reset_targets()
    R_down = rotvec_to_mat([np.pi, 0, 0])
    hit = False
    for z in np.linspace(0.08, -0.03, 25):
        ad.set_tcp_target(make_T([0.22, 0.0, z], R=R_down), 0.0)
        c = ad.last_contacts
        if c.finger_table or c.robot_table:
            hit = True
            break
    assert hit


def test_scripted_episode_succeeds(env, safety_cfg):
    ad = SimRobotAdapter(env)
    sf = SafetyFilter(safety_cfg)
    pol = ScriptedPickPlace(sf.scale.max_delta_pos, sf.scale.max_delta_rot, lift_height=0.08)
    r = run_episode(env, ad, pol, sf, seed=123)
    assert r.success and r.grasp_success and r.place_success and not r.collision
    assert r.cycle_time_s < 12 and r.n_steps < env.max_steps
    assert np.isfinite(r.path_length_m)


def test_evaluator_detects_no_grasp(env, safety_cfg):
    ad = SimRobotAdapter(env)
    sf = SafetyFilter(safety_cfg)
    # expert that grasps 4 cm beside the cube -> NO_GRASP / retries
    pol = ScriptedPickPlace(sf.scale.max_delta_pos, sf.scale.max_delta_rot, lift_height=0.08, max_retries=0)

    class Offset(ScriptedPickPlace):
        def act(self, obs):
            obs.extra["privileged"]["object_pos"] = obs.extra["privileged"]["object_pos"] + np.array([0.04, 0, 0])
            return super().act(obs)

    pol = Offset(sf.scale.max_delta_pos, sf.scale.max_delta_rot, lift_height=0.08, max_retries=0)
    r = run_episode(env, ad, pol, sf, seed=7)
    assert not r.success and not r.grasp_success and r.failure_type in ("NO_GRASP", "SLIP", "TIMEOUT")
