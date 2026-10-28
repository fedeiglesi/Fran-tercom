"""DAgger session on the real robot: the policy acts, you correct with the keyboard; everything is recorded.

    uv run python scripts/dagger.py --checkpoint outputs/<ckpt> --episodes 20 --name dagger_real
Keys: w/s a/d q/e move, c/o gripper, S success, F failure, x abort (see picking/teleop/keyboard.py).
"""
from __future__ import annotations

import argparse
import sys

from picking.cameras.oakd import OakDCamera
from picking.datasets.recorder import EpisodeRecorder
from picking.evaluation.real_runner import RealEpisodeConfig, run_real_episode
from picking.policies.act_policy import ACTPolicyWrapper
from picking.robots.mycobot import MyCobotAdapter
from picking.safety.filter import SafetyFilter
from picking.teleop.dagger import HumanCorrector
from picking.teleop.keyboard import RawKeyboard
from picking.utils.io import config_path, load_yaml
from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--episodes", type=int, default=10)
    ap.add_argument("--name", default="dagger_real")
    ap.add_argument("--instruction", default="Pick the red cube and place it in the blue tray.")
    args = ap.parse_args()
    setup_logging()
    rcfg = load_yaml(config_path("real/mycobot_280.yaml"))["robot"]
    sf = SafetyFilter(load_yaml(config_path("safety/real_mycobot.yaml")))
    policy = ACTPolicyWrapper(args.checkpoint)
    ad = MyCobotAdapter(rcfg)
    ad.connect()
    cam = OakDCamera()
    cam.start()
    ecfg = RealEpisodeConfig(instruction=args.instruction, control_hz=float(rcfg.get("control_hz", 5)), max_steps=400, camera_key="oakd")
    rec = EpisodeRecorder(args.name, fps=int(ecfg.control_hz), camera_names=["oakd"], image_hw=(128, 128), with_depth=True, metadata={"source": "real_dagger", "checkpoint": args.checkpoint})
    if input("Type YES to arm the robot: ").strip() != "YES":
        return 1
    ad.arm()
    with RawKeyboard() as kb:
        for i in range(args.episodes):
            ad.home()
            hc = HumanCorrector(key_source=lambda: kb.get_key(0.0))
            r = run_real_episode(cam, ad, policy, sf, ecfg, key_poll=lambda: {"success": "S", "failure": "F", "abort": "x"}.get(hc.last_command), step_callback=rec.on_step, corrections=hc)
            rec.end_episode(r, {"correction_rate": hc.correction_rate()})
            print(f"episode {i+1}: success={r.success} corrections={hc.n_corrections}/{hc.n_steps}")
            if hc.last_command == "abort":
                break
    ad.stop()
    ad.disconnect()
    cam.stop()
    rec.finalize()
    return 0


if __name__ == "__main__":
    sys.exit(main())
