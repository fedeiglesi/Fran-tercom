"""Validates the whole perception geometry chain in simulation: intrinsics + depth + Kabsch recover the
ground-truth camera pose; projected object positions match."""
import numpy as np

from picking.calibration.extrinsics import calibrate_from_points, calibrate_hand_eye, camera_point_to_base, check_calibration_quality
from picking.cameras.sim_camera import SimCamera
from picking.core.transforms import inv_T, make_T, rotvec_to_mat


def test_sim_camera_depth_backprojection_matches_calibration(env):
    env.reset(seed=0)
    cam = SimCamera(env)
    k = cam.intrinsics()
    T_gt = cam.pose_cv_in_base()
    P_cam, P_base = [], []
    rng = np.random.default_rng(0)
    for seed in range(8):
        env.reset(seed=seed)
        # use the cube top-face centre as the "touched" point: project it with ground truth, read depth there
        p_base = env.object_pose("red_cube")[:3, 3] + np.array([0, 0, env.object_half_extent_z("red_cube")])
        p_cam_gt = inv_T(T_gt)[:3, :3] @ p_base + inv_T(T_gt)[:3, 3]
        u = k.fx * p_cam_gt[0] / p_cam_gt[2] + k.cx
        v = k.fy * p_cam_gt[1] / p_cam_gt[2] + k.cy
        frame = cam.read()
        z = frame.depth[int(round(v)), int(round(u))]
        assert abs(z - p_cam_gt[2]) < 0.005, (z, p_cam_gt[2])  # depth rendering consistent with the pinhole model
        P_cam.append(cam.deproject(u, v, z) + rng.normal(0, 0.0005, 3))
        P_base.append(p_base + rng.normal(0, 0.0005, 3))
    res = calibrate_from_points(np.array(P_cam), np.array(P_base))
    ok, msg = check_calibration_quality(res)
    assert ok, msg
    assert np.allclose(res.T_base_camera[:3, 3], T_gt[:3, 3], atol=0.01), (res.T_base_camera[:3, 3], T_gt[:3, 3])
    assert np.allclose(res.T_base_camera[:3, :3], T_gt[:3, :3], atol=0.03)
    # re-projection of a fresh point
    env.reset(seed=99)
    p_base = env.object_pose("red_cube")[:3, 3]
    p_cam = inv_T(T_gt)[:3, :3] @ p_base + inv_T(T_gt)[:3, 3]
    assert np.linalg.norm(camera_point_to_base(res.T_base_camera, p_cam) - p_base) < 0.005


def test_hand_eye_synthetic():
    rng = np.random.default_rng(1)
    T_bc = make_T([0.5, 0.1, 0.4], R=rotvec_to_mat([2.0, 0.3, -0.2]))
    T_gm = make_T([0.0, 0.02, 0.05], R=rotvec_to_mat([0.1, 0.0, 0.3]))  # marker on gripper
    T_bg_list, T_cm_list = [], []
    for _ in range(12):
        T_bg = make_T(rng.uniform([0.1, -0.15, 0.05], [0.25, 0.15, 0.2]), R=rotvec_to_mat(rng.normal(0, 0.4, 3)) @ rotvec_to_mat([np.pi, 0, 0]))
        T_cm = inv_T(T_bc) @ T_bg @ T_gm
        T_bg_list.append(T_bg)
        T_cm_list.append(T_cm)
    res = calibrate_hand_eye(T_bg_list, T_cm_list)
    assert np.allclose(res.T_base_camera, T_bc, atol=1e-4)
    assert res.rms_residual_m < 1e-4
