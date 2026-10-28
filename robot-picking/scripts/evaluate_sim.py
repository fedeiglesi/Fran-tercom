"""Benchmark a policy in simulation over a set of seeds and write machine-readable results.

Examples:
    uv run python scripts/evaluate_sim.py --policy heuristic --episodes 100 --seed-start 100000
    uv run python scripts/evaluate_sim.py --policy act --checkpoint outputs/act_v001 --episodes 1000
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

from picking.evaluation.metrics import gate_check, save_results
from picking.evaluation.runner import run_episode
from picking.robots.sim import SimRobotAdapter
from picking.safety.filter import SafetyFilter
from picking.simulation.env import PickPlaceEnv
from picking.utils.io import config_path, deep_update, git_dirty, git_sha, load_yaml, project_root
from picking.utils.logging import setup_logging

GATE_SIM_INITIAL = {"success_rate": (">=", 0.95), "collision_rate": ("<=", 0.005), "drop_rate": ("<=", 0.02), "timeout_rate": ("<=", 0.02)}
GATE_SIM_TARGET = {"success_rate": (">=", 0.97), "collision_rate": ("<=", 0.005), "drop_rate": ("<=", 0.02), "timeout_rate": ("<=", 0.02)}


def make_policy(args, cfg, safety: SafetyFilter):
    if args.policy == "heuristic":
        from picking.policies.heuristic import ScriptedPickPlace

        return ScriptedPickPlace(safety.scale.max_delta_pos, safety.scale.max_delta_rot, lift_height=float(cfg["task"]["lift_height"]) + 0.03, noise_std=args.action_noise, rng=np.random.default_rng(args.seed_start + 7))
    if args.policy == "perception":
        from picking.calibration.extrinsics import load_calibration
        from picking.policies.perception_scripted import PerceptionScriptedPickPlace

        T = load_calibration(args.calibration) if args.calibration else None
        pol = PerceptionScriptedPickPlace(safety.scale.max_delta_pos, safety.scale.max_delta_rot, camera=cfg["camera"]["name"], color=args.color, object_id=cfg["task"]["target_object"], lift_height=float(cfg["task"]["lift_height"]) + 0.03, roi=tuple(cfg["camera"].get("policy_crop")) if cfg["camera"].get("policy_crop") else None)
        pol.T_override = T
        return pol
    if args.policy == "act":
        from picking.policies.act_policy import ACTPolicyWrapper

        return ACTPolicyWrapper(args.checkpoint, device=args.device, act_override=json.loads(args.act_override) if args.act_override else None)
    if args.policy == "remote":
        from picking.policies.remote import RemotePolicy

        return RemotePolicy(args.server_url)
    raise ValueError(args.policy)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sim/level1_cube.yaml")
    ap.add_argument("--safety", default="safety/sim_default.yaml")
    ap.add_argument("--override", default=None, help="JSON dict merged into the config")
    ap.add_argument("--policy", default="heuristic", choices=["heuristic", "perception", "act", "remote"])
    ap.add_argument("--calibration", default=None, help="perception policy: calibration YAML (default: sim ground truth)")
    ap.add_argument("--color", default="red")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--act-override", default=None, help='JSON of inference-time ACT config changes, e.g. {"n_action_steps": 5}')
    ap.add_argument("--server-url", default="ws://127.0.0.1:8765")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--episodes", type=int, default=100)
    ap.add_argument("--seed-start", type=int, default=100000, help="holdout seeds: never used for training (training uses < 100000)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--action-noise", type=float, default=0.0)
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--gate", default="initial", choices=["initial", "target", "none"])
    ap.add_argument("--log-every", type=int, default=20)
    args = ap.parse_args()
    setup_logging("INFO")
    log = logging.getLogger("evaluate_sim")
    cfg = load_yaml(config_path(args.config))
    if args.override:
        cfg = deep_update(cfg, json.loads(args.override))
    safety_cfg = load_yaml(config_path(args.safety))
    needs_render = args.policy != "heuristic"
    env = PickPlaceEnv(cfg, render=needs_render and not args.no_render)
    adapter = SimRobotAdapter(env)
    safety = SafetyFilter(safety_cfg)
    policy = make_policy(args, cfg, safety)
    if args.policy == "perception" and getattr(policy, "T_override", None) is not None:
        env.T_base_camera = policy.T_override
    image_size = tuple(cfg["camera"].get("policy_image_size", [96, 96]))
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else project_root() / "reports" / "eval" / f"{ts}_{args.policy}{('_' + args.tag) if args.tag else ''}"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    t0 = time.time()
    for i in range(args.episodes):
        seed = args.seed_start + i
        r = run_episode(env, adapter, policy, safety, seed=seed, image_size=image_size, full_frame=(args.policy == "perception"))
        results.append(r)
        if (i + 1) % args.log_every == 0 or not r.success:
            sr = sum(x.success for x in results) / len(results)
            log.info("ep %d seed %d success=%s fail=%s steps=%d retries=%d | running success %.3f (%.1fs)", i + 1, seed, r.success, r.failure_type, r.n_steps, r.n_retries, sr, time.time() - t0)
    gate = {"initial": GATE_SIM_INITIAL, "target": GATE_SIM_TARGET, "none": {}}[args.gate]
    extra = {
        "policy": policy.name,
        "policy_version": policy.version,
        "checkpoint": args.checkpoint,
        "config": args.config,
        "config_resolved": cfg,
        "safety_config": args.safety,
        "override": args.override,
        "seed_start": args.seed_start,
        "git_sha": git_sha(),
        "git_dirty": git_dirty(),
        "wall_time_s": time.time() - t0,
        "action_noise": args.action_noise,
        "versions": _versions(),
    }
    summary = save_results(results, out, extra)
    if gate:
        g = gate_check(summary, gate)
        summary["gate"] = g
        from picking.utils.io import save_json

        save_json(summary, out / "summary.json")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("config_resolved", "versions")}, indent=2, default=str))
    print(f"results written to {out}")
    env.close()
    return 0


def _versions() -> dict:
    import mujoco, numpy, torch

    v = {"python": sys.version.split()[0], "mujoco": mujoco.__version__, "numpy": numpy.__version__, "torch": torch.__version__}
    try:
        import lerobot

        v["lerobot"] = lerobot.__version__
    except Exception:
        pass
    return v


if __name__ == "__main__":
    sys.exit(main())
