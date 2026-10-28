import numpy as np
import pytest

from picking.datasets.recorder import EpisodeRecorder, load_episode_records
from picking.evaluation.runner import run_episode
from picking.policies.heuristic import ScriptedPickPlace
from picking.robots.sim import SimRobotAdapter
from picking.safety.filter import SafetyFilter


@pytest.mark.timeout(600)
def test_record_and_load_dataset(env, safety_cfg, tmp_path):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ad = SimRobotAdapter(env)
    sf = SafetyFilter(safety_cfg)
    pol = ScriptedPickPlace(sf.scale.max_delta_pos, sf.scale.max_delta_rot, lift_height=0.08)
    root = tmp_path / "ds_v001"
    rec = EpisodeRecorder("ds", fps=10, camera_names=[env.cam_name], image_hw=(64, 64), with_depth=True, root=root)
    for seed in (1, 2):
        r = run_episode(env, ad, pol, sf, seed=seed, image_size=(64, 64), with_depth=True, step_callback=rec.on_step, force_images=True)
        rec.end_episode(r)
    rec.finalize()
    recs = load_episode_records(root)
    assert len(recs) == 2 and all("success" in r and "failure_type" in r for r in recs)
    ds = LeRobotDataset(f"local/{root.name}", root=root, delta_timestamps={"action": [i / 10 for i in range(5)]})
    assert ds.meta.total_episodes == 2 and len(ds) == sum(r["n_frames"] for r in recs)
    it = ds[3]
    assert it["observation.state"].shape == (10,)
    assert it["observation.images." + env.cam_name].shape == (3, 64, 64)
    assert it["observation.depth." + env.cam_name].shape == (64, 64)
    assert it["action"].shape == (5, 7) and it["action_is_pad"].shape == (5,)
    assert np.isfinite(it["action"].numpy()).all()
