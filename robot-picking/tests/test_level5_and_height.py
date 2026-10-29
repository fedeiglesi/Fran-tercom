import numpy as np

from picking.cameras.sim_camera import SimCamera
from picking.perception.segmentation import detect_by_height
from picking.simulation.env import PickPlaceEnv
from picking.utils.io import load_yaml, project_root


def test_level5_places_distinct_colour_objects_apart():
    cfg = load_yaml(project_root() / "configs" / "sim" / "level5_multi_object.yaml")
    env = PickPlaceEnv(cfg, render=False)
    for seed in range(5):
        env.reset(seed=seed)
        assert len(env.present_objects) == 4 and env.current_target in env.present_objects
        colours = [env.object_specs[o]["color_name"] for o in env.present_objects]
        assert len(set(colours)) == 4
        pos = np.array([env.object_pose(o)[:3, 3][:2] for o in env.present_objects])
        for i in range(4):
            for j in range(i + 1, 4):
                assert np.linalg.norm(pos[i] - pos[j]) >= 0.05
        assert "Pick the" in env.instruction()
    env.close()


def test_height_detector_matches_ground_truth(env):
    cam = SimCamera(env)
    for seed in range(4):
        env.reset(seed=seed)
        fr = cam.read()
        det = detect_by_height(fr.rgb, fr.depth, fr.intrinsics, cam.pose_cv_in_base(), "obj", exclude_xy=env.tray_footprint(), workspace_xy=((0.08, 0.30), (-0.22, 0.22)))
        assert det is not None
        gt = env.object_pose("red_cube")[:3, 3]
        assert np.linalg.norm(det.position_base[:2] - gt[:2]) < 0.006
        assert abs(det.height_m - 0.025) < 0.006
