"""Level 7 benchmark: thousands of simulated orders (source bin -> order bin, mixed packaging, SKU identification).

    uv run python scripts/evaluate_orders.py --orders 200 --sku-error 0.05
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

import numpy as np

from picking.calibration.extrinsics import load_calibration
from picking.evaluation.order_runner import run_order
from picking.perception.sku import SkuIdentifierOracle
from picking.policies.perception_scripted import PerceptionScriptedPickPlace
from picking.robots.sim import SimRobotAdapter
from picking.safety.filter import SafetyFilter
from picking.simulation.env import PickPlaceEnv
from picking.utils.io import config_path, git_sha, load_yaml, project_root, save_json
from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sim/level7_orders.yaml")
    ap.add_argument("--safety", default="safety/sim_default.yaml")
    ap.add_argument("--calibration", default="calibration/oakd_to_mycobot_sim_v001.yaml")
    ap.add_argument("--orders", type=int, default=100)
    ap.add_argument("--seed-start", type=int, default=100000)
    ap.add_argument("--sku-error", type=float, default=0.0, help="SKU identifier error rate (placeholder for the learned detector)")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--grasp-offset", type=float, default=0.0)
    ap.add_argument("--grasp-depth", type=float, default=0.0, help="close this fraction of the object height BELOW its centre (rounded parts escape when pinched above the equator)")
    ap.add_argument("--tol-frac", type=float, default=0.3, help="vertical tolerance to start closing, fraction of the object height")
    ap.add_argument("--xy-gate", type=float, default=0.0025, help="descend: hold the height until the lateral error is below this [m] (0 disables)")
    ap.add_argument("--no-opening-limiter", action="store_true", help="disable the wall/neighbour-aware pre-shaped opening")
    ap.add_argument("--preshape", type=float, default=0.008, help="pre-shaped opening margin [m] (8 mm: fits a 32 mm part in a bin corner)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--log-every", type=int, default=25)
    args = ap.parse_args()
    setup_logging()
    cfg = load_yaml(config_path(args.config))
    env = PickPlaceEnv(cfg)
    env.T_base_camera = load_calibration(project_root() / args.calibration)
    adapter = SimRobotAdapter(env)
    safety = SafetyFilter(load_yaml(config_path(args.safety)))
    ident = SkuIdentifierOracle(error_rate=args.sku_error, seed=args.seed_start)
    policy = PerceptionScriptedPickPlace(safety.scale.max_delta_pos, safety.scale.max_delta_rot, camera=env.cam_name, detector="sku", sku_identifier=ident, lift_height=float(cfg["task"]["lift_height"]) + 0.03, roi=tuple(cfg["camera"]["policy_crop"]), gripper_max_opening=float(cfg["robot"]["gripper_max_opening"]), grasp_height_offset=args.grasp_offset, preshape_margin=args.preshape, grasp_depth_frac=args.grasp_depth, descend_tol_frac=args.tol_frac)
    policy.descend_xy_gate = args.xy_gate
    policy.use_opening_limiter = not args.no_opening_limiter
    out = project_root() / "reports" / "orders" / f"{time.strftime('%Y%m%d_%H%M%S')}_{args.tag or 'orders'}"
    out.mkdir(parents=True, exist_ok=True)
    results = []
    t0 = time.time()
    for i in range(args.orders):
        r = run_order(env, adapter, policy, safety, seed=args.seed_start + i, max_retries_per_item=args.retries)
        results.append(r)
        if (i + 1) % args.log_every == 0:
            print(f"orders {i+1}/{args.orders}: success {np.mean([x.success for x in results]):.3f} ({time.time()-t0:.0f}s)", flush=True)
    items = [it for r in results for it in r.items]
    by_pack = collections.defaultdict(list)
    for it in items:
        by_pack[it["packaging"]].append(it["success"])
    summary = {
        "n_orders": len(results),
        "order_success_rate": float(np.mean([r.success for r in results])),
        "item_success_rate": float(np.mean([it["success"] for it in items])),
        "item_failure_types": dict(collections.Counter(it["failure_type"] for it in items if not it["success"])),
        "wrong_sku_rate": float(np.mean([r.wrong_sku > 0 for r in results])),
        "prefilled_disturbed_rate": float(np.mean([r.prefilled_disturbed for r in results])),
        "collision_rate": float(np.mean([r.collision for r in results])),
        "by_packaging": {k: {"n": len(v), "success_rate": float(np.mean(v))} for k, v in by_pack.items()},
        "by_sku": {sku: float(np.mean([it["success"] for it in items if it["sku"] == sku])) for sku in sorted({it["sku"] for it in items})},
        "median_order_time_s": float(np.median([r.cycle_time_s for r in results])),
        "sku_identifier": {"name": ident.name, "error_rate": args.sku_error, "n_corrupted": ident.n_corrupted},
        "config": args.config, "seed_start": args.seed_start, "git_sha": git_sha(), "wall_time_s": time.time() - t0,
    }
    save_json([r.to_dict() for r in results], out / "orders.json")
    save_json(summary, out / "summary.json")
    print(json.dumps(summary, indent=2))
    print("written to", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
