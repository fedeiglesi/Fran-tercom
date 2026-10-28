"""Run a policy on the REAL robot (after the sim gate + tests 1-7). Records every episode.

    uv run python scripts/run_policy.py --policy act --checkpoint outputs/<ckpt> --episodes 10
    uv run python scripts/run_policy.py --policy remote --server-url ws://GPU-HOST:8765 --episodes 10
Press S/F to mark success/failure of each episode, x to abort. Everything (including failures) is recorded.
"""
from __future__ import annotations

import argparse
import json
import sys

from picking.cameras.oakd import OakDCamera
from picking.datasets.recorder import EpisodeRecorder
from picking.evaluation.metrics import aggregate, save_results
from picking.evaluation.real_runner import RealEpisodeConfig, run_real_episode
from picking.robots.mycobot import MyCobotAdapter
from picking.safety.filter import SafetyFilter
from picking.teleop.keyboard import RawKeyboard
from picking.utils.io import config_path, load_yaml, project_root
from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="act", choices=["act", "remote"])
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--server-url", default="ws://127.0.0.1:8765")
    ap.add_argument("--episodes", type=int, default=10)
    ap.add_argument("--name", default="real_eval")
    ap.add_argument("--instruction", default="Pick the red cube and place it in the blue tray.")
    ap.add_argument("--safety", default="safety/real_mycobot.yaml")
    args = ap.parse_args()
    setup_logging()
    rcfg = load_yaml(config_path("real/mycobot_280.yaml"))["robot"]
    sf = SafetyFilter(load_yaml(config_path(args.safety)))
    if args.policy == "act":
        from picking.policies.act_policy import ACTPolicyWrapper

        policy = ACTPolicyWrapper(args.checkpoint)
    else:
        from picking.networking.client import RemotePolicy

        policy = RemotePolicy(args.server_url)
    ad = MyCobotAdapter(rcfg)
    ad.connect()
    cam = OakDCamera()
    cam.start()
    ecfg = RealEpisodeConfig(instruction=args.instruction, control_hz=float(rcfg.get("control_hz", 5)), max_steps=300, camera_key="oakd")
    rec = EpisodeRecorder(args.name, fps=int(ecfg.control_hz), camera_names=["oakd"], image_hw=(128, 128), with_depth=True, metadata={"source": "real_policy", "policy": policy.name, "checkpoint": args.checkpoint})
    results = []
    print("Press YES to arm the robot. Keep the emergency stop within reach.")
    if input("> ").strip() != "YES":
        return 1
    ad.arm()
    with RawKeyboard() as kb:
        for i in range(args.episodes):
            ad.home()
            print(f"\n=== episode {i+1}/{args.episodes}: place the object, then press ENTER (x aborts)")
            if (kb.get_key(1e9) or "") == "x":
                break
            r = run_real_episode(cam, ad, policy, sf, ecfg, key_poll=lambda: kb.get_key(0.0), step_callback=rec.on_step)
            rec.end_episode(r)
            results.append(r)
            print("result:", r.success, r.failure_type, "safety:", r.metadata["safety"]["n_rejected"], "rejects")
            if r.metadata.get("operator_outcome") == "abort":
                break
    ad.stop()
    ad.disconnect()
    cam.stop()
    rec.finalize()
    out = project_root() / "reports" / "real" / rec.root.name
    summary = save_results(results, out, {"policy": policy.name, "checkpoint": args.checkpoint, "dataset": str(rec.root)})
    print(json.dumps(aggregate(results), indent=2))
    print("written to", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
