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
    ap.add_argument("--dart-noise", type=float, default=0.0, help="DART: execute noisy actions (std, normalized units) but label with the expert's clean action")
    ap.add_argument("--dart-fraction", type=float, default=0.5, help="fraction of episodes recorded with DART noise")
    ap.add_argument("--expert-gain", type=float, default=1.0, help="proportional gain of the scripted expert (lower = smoother, less saturated actions)")
    ap.add_argument("--expert-min-speed", type=float, default=0.0, help="speed floor (normalized) of the scripted expert near goals")
    ap.add_argument("--lowdim", action="store_true", help="no images; record observation.environment_state = object pose (privileged + noise)")
    ap.add_argument("--env-state-noise", type=float, default=0.002, help="[m] gaussian noise on the recorded object position (matches measured perception error)")
    args = ap.parse_args()
    setup_logging()
    log = logging.getLogger("record_demos")
    cfg = load_yaml(config_path(args.config))
    if args.override:
        cfg = deep_update(cfg, json.loads(args.override))
    safety = SafetyFilter(load_yaml(config_path(args.safety)))
    env = PickPlaceEnv(cfg, render=not args.lowdim)
    adapter = SimRobotAdapter(env)
    policy = ScriptedPickPlace(safety.scale.max_delta_pos, safety.scale.max_delta_rot, lift_height=float(cfg["task"]["lift_height"]) + 0.03, noise_std=args.action_noise, rng=np.random.default_rng(args.seed_start + 11), gain=args.expert_gain, min_speed=args.expert_min_speed)
    image_size = tuple(cfg["camera"]["policy_image_size"])  # (w, h)
    noise_rng = np.random.default_rng(args.seed_start + 99)

    from picking.perception.env_state import REDETECT_MIN_TCP_Z, env_state_vector

    held = {"v": None}

    def env_state(d):
        # emulate the perception provider: (re)observe only when the gripper is open and the TCP is high,
        # otherwise hold the last observation (object occluded / in hand)
        obs = d.obs
        may_observe = held["v"] is None or (obs.gripper < 0.3 and obs.tcp_pose[2] > REDETECT_MIN_TCP_Z)
        if may_observe or d.step == 0:
            p = np.asarray(d.privileged["object_pos"], float) + noise_rng.normal(0, args.env_state_noise, 3)
            held["v"] = env_state_vector(p, float(d.privileged["object_yaw"]), float(d.privileged.get("object_yaw_period", np.pi / 2)))
        return held["v"]

    rec = EpisodeRecorder(
        name=args.name,
        fps=int(cfg["sim"]["control_hz"]),
        camera_names=[] if args.lowdim else [env.cam_name],
        image_hw=(image_size[1], image_size[0]),
        with_depth=args.depth and not args.lowdim,
        env_state_fn=env_state if args.lowdim else None,
        dart_labels=args.dart_noise > 0,
        metadata={"source": "simulation", "config": args.config, "config_resolved": cfg, "safety": args.safety, "policy": policy.name, "policy_version": policy.version, "action_noise": args.action_noise, "seed_start": args.seed_start, "n_requested": args.episodes},
    )
    results = []
    t0 = time.time()
    for i in range(args.episodes):
        seed = args.seed_start + i
        held["v"] = None
        policy.noise_std = args.dart_noise if (args.dart_noise > 0 and (i % 100) < args.dart_fraction * 100) else args.action_noise
        r = run_episode(env, adapter, policy, safety, seed=seed, image_size=image_size, with_depth=args.depth and not args.lowdim, step_callback=rec.on_step, force_images=not args.lowdim)
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
