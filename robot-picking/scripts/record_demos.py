"""Generate demonstrations in simulation with the scripted expert and record a versioned LeRobot dataset.

    uv run python scripts/record_demos.py --episodes 200 --seed-start 0 --name level1_cube_scripted
Seeds < 100000 are TRAINING seeds; evaluation uses >= 100000 (holdout).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import numpy as np

from picking.datasets.recorder import EpisodeRecorder
from picking.evaluation.metrics import aggregate
from picking.evaluation.runner import run_episode
from picking.policies.heuristic import ScriptedPickPlace
from picking.robots.sim import SimRobotAdapter
from picking.safety.filter import SafetyFilter
from picking.simulation.env import PickPlaceEnv
from picking.utils.io import config_path, deep_update, load_yaml
from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sim/level1_cube.yaml")
    ap.add_argument("--safety", default="safety/sim_default.yaml")
    ap.add_argument("--override", default=None)
    ap.add_argument("--episodes", type=int, default=100)
    ap.add_argument("--seed-start", type=int, default=0)
    ap.add_argument("--name", default="level1_cube_scripted")
    ap.add_argument("--action-noise", type=float, default=0.0, help="gaussian noise on normalized actions (diversity)")
    ap.add_argument("--keep-failures", action="store_true", default=True)
    ap.add_argument("--depth", action="store_true")
    ap.add_argument("--max-episodes-fail-ratio", type=float, default=0.5)
    args = ap.parse_args()
    setup_logging()
    log = logging.getLogger("record_demos")
    cfg = load_yaml(config_path(args.config))
    if args.override:
        cfg = deep_update(cfg, json.loads(args.override))
    safety = SafetyFilter(load_yaml(config_path(args.safety)))
    env = PickPlaceEnv(cfg, render=True)
    adapter = SimRobotAdapter(env)
    policy = ScriptedPickPlace(safety.scale.max_delta_pos, safety.scale.max_delta_rot, lift_height=float(cfg["task"]["lift_height"]) + 0.03, noise_std=args.action_noise, rng=np.random.default_rng(args.seed_start + 11))
    image_size = tuple(cfg["camera"]["policy_image_size"])  # (w, h)
    rec = EpisodeRecorder(
        name=args.name,
        fps=int(cfg["sim"]["control_hz"]),
        camera_names=[env.cam_name],
        image_hw=(image_size[1], image_size[0]),
        with_depth=args.depth,
        metadata={"source": "simulation", "config": args.config, "config_resolved": cfg, "safety": args.safety, "policy": policy.name, "policy_version": policy.version, "action_noise": args.action_noise, "seed_start": args.seed_start, "n_requested": args.episodes},
    )
    results = []
    t0 = time.time()
    for i in range(args.episodes):
        seed = args.seed_start + i
        r = run_episode(env, adapter, policy, safety, seed=seed, image_size=image_size, with_depth=args.depth, step_callback=rec.on_step, force_images=True)
        rec.end_episode(r)
        results.append(r)
        if (i + 1) % 10 == 0:
            log.info("recorded %d/%d episodes, success so far %.3f (%.0fs)", i + 1, args.episodes, np.mean([x.success for x in results]), time.time() - t0)
    summary = rec.finalize()
    summary["results"] = aggregate(results)
    print(json.dumps(summary, indent=2, default=str))
    env.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
