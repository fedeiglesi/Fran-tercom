"""Keyboard teleoperation + demonstration recording (sim or real). See picking/teleop/keyboard.py for keys.

    uv run python scripts/teleop.py --sim --record --name teleop_sim
    uv run python scripts/teleop.py --real --record --name teleop_real
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from picking.core.types import Action
from picking.policies.base import Policy
from picking.safety.filter import SafetyFilter
from picking.teleop.keyboard import KeyMapper, RawKeyboard
from picking.utils.io import config_path, load_yaml
from picking.utils.logging import setup_logging


class TeleopPolicy(Policy):
    name, version, needs_images = "human_teleop", "1", True

    def __init__(self, kb: RawKeyboard, mapper: KeyMapper):
        self.kb, self.mapper = kb, mapper
        self.last_command = None

    def act(self, obs):
        ev = self.mapper.map(self.kb.get_key(0.0))
        if ev.command:
            self.last_command = ev.command
            return Action.zero(self.mapper.gripper)
        return ev.action


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--name", default="teleop")
    ap.add_argument("--config", default="sim/level1_cube.yaml")
    ap.add_argument("--safety", default=None)
    args = ap.parse_args()
    setup_logging()
    print(__doc__)
    mapper = KeyMapper()
    rec = None
    if args.real:
        from picking.cameras.oakd import OakDCamera
        from picking.evaluation.real_runner import RealEpisodeConfig, run_real_episode
        from picking.robots.mycobot import MyCobotAdapter

        rcfg = load_yaml(config_path("real/mycobot_280.yaml"))["robot"]
        sf = SafetyFilter(load_yaml(config_path(args.safety or "safety/real_mycobot.yaml")))
        ad = MyCobotAdapter(rcfg)
        ad.connect()
        ad.arm()
        cam = OakDCamera()
        cam.start()
        ecfg = RealEpisodeConfig(instruction="Pick the red cube and place it in the blue tray.", control_hz=float(rcfg.get("control_hz", 5)), max_steps=2000, camera_key="oakd")
        if args.record:
            from picking.datasets.recorder import EpisodeRecorder

            rec = EpisodeRecorder(args.name, fps=int(ecfg.control_hz), camera_names=["oakd"], image_hw=(128, 128), with_depth=True, metadata={"source": "real_teleop"})
        with RawKeyboard() as kb:
            pol = TeleopPolicy(kb, mapper)
            while True:
                ad.home()
                r = run_real_episode(cam, ad, pol, sf, ecfg, key_poll=lambda: (pol.last_command or [None])[0].upper() if pol.last_command in ("success", "failure") else ("x" if pol.last_command == "abort" else None), step_callback=rec.on_step if rec else None)
                if rec:
                    rec.end_episode(r, {"operator": True})
                print("episode:", r.success, r.failure_type)
                if pol.last_command == "abort":
                    break
                pol.last_command = None
        if rec:
            rec.finalize()
        ad.disconnect()
        cam.stop()
        return 0
    # simulation teleop (headless-friendly: prints state; on macOS a viewer can be added)
    from picking.evaluation.runner import run_episode
    from picking.robots.sim import SimRobotAdapter
    from picking.simulation.env import PickPlaceEnv

    cfg = load_yaml(config_path(args.config))
    sf = SafetyFilter(load_yaml(config_path(args.safety or "safety/sim_default.yaml")))
    env = PickPlaceEnv(cfg, render=True)
    ad = SimRobotAdapter(env)
    if args.record:
        from picking.datasets.recorder import EpisodeRecorder

        rec = EpisodeRecorder(args.name, fps=int(cfg["sim"]["control_hz"]), camera_names=[env.cam_name], image_hw=tuple(cfg["camera"]["policy_image_size"])[::-1], metadata={"source": "sim_teleop"})
    seed = 0
    with RawKeyboard() as kb:
        pol = TeleopPolicy(kb, mapper)

        class SlowTeleop(TeleopPolicy):
            def act(self, obs):
                time.sleep(1.0 / cfg["sim"]["control_hz"])
                p = obs.tcp_pose[:3]
                print(f"\rtcp x={p[0]:.3f} y={p[1]:.3f} z={p[2]:.3f} grip={obs.gripper:.2f} cmd={self.last_command}   ", end="")
                return super().act(obs)

            def done(self):
                return self.last_command in ("success", "failure", "abort", "reset")

        pol = SlowTeleop(kb, mapper)
        while True:
            r = run_episode(env, ad, pol, sf, seed=seed, image_size=tuple(cfg["camera"]["policy_image_size"]), step_callback=rec.on_step if rec else None, force_images=True)
            if rec and pol.last_command in ("success", "failure"):
                r.success = pol.last_command == "success"
                rec.end_episode(r, {"operator": True})
            elif rec:
                rec.discard_episode()
            print("\nepisode result:", r.success, r.failure_type, "(operator:", pol.last_command, ")")
            if pol.last_command == "abort":
                break
            pol.last_command = None
            seed += 1
    if rec:
        rec.finalize()
    env.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
