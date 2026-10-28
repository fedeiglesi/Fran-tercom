import numpy as np

from picking.cameras.sim_camera import SimCamera
from picking.perception.segmentation import detect_colored_object
from picking.perception.verification import verify_grasp


def test_colour_depth_detection_matches_ground_truth(env):
    cam = SimCamera(env)
    errs = []
    for seed in range(5):
        env.reset(seed=seed)
        fr = cam.read()
        det = detect_colored_object(fr.rgb, fr.depth, fr.intrinsics, cam.pose_cv_in_base(), "red", "red_cube")
        assert det is not None
        gt = env.object_pose("red_cube")[:3, 3]
        errs.append(np.linalg.norm(det.position_base - gt))
        assert abs(det.height_m - 0.025) < 0.006
    assert max(errs) < 0.006, errs


def test_grasp_verification_signals():
    assert verify_grasp(0.4, 0.024, 0.015).holding
    assert not verify_grasp(1.0, 0.0, 0.015).holding  # fully closed = empty
    before = np.zeros((60, 60, 3), np.uint8)
    before[20:30, 20:30] = (220, 20, 20)
    after = np.zeros((60, 60, 3), np.uint8)
    after[40:50, 40:50] = (220, 20, 20)
    v = verify_grasp(0.4, 0.024, 0.015, before, after, "red", np.array([25.0, 25.0]))
    assert v.visual_signal is True and v.holding
    v2 = verify_grasp(0.4, 0.024, 0.015, before, before, "red", np.array([25.0, 25.0]))
    assert v2.visual_signal is False and not v2.holding
