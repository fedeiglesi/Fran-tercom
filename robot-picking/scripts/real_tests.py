"""Physical first-contact sequence (docs/REAL_ROBOT.md). Each test asks for confirmation before moving.

    uv run python scripts/real_tests.py --test 2        # move ONE joint slowly (+5 deg and back)
    uv run python scripts/real_tests.py --test 3        # small cartesian move (1 cm up and back)
    uv run python scripts/real_tests.py --test 4        # open / close gripper
    uv run python scripts/real_tests.py --test 5        # predefined poses (home, above-table)
    uv run python scripts/real_tests.py --test 6        # actions through RobotAdapter + SafetyFilter
    uv run python scripts/real_tests.py --test 7 --checkpoint outputs/<ckpt>   # policy, very slow
Keep a hand on the power switch / emergency stop at all times.
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from picking.core.transforms import make_T
from picking.core.types import Action
from picking.robots.mycobot import MyCobotAdapter
from picking.safety.filter import SafetyFilter
from picking.utils.io import config_path, load_yaml
from picking.utils.logging import setup_logging


def confirm(msg: str) -> bool:
    print("\n" + msg)
    return input("Type YES to continue (anything else aborts): ").strip() == "YES"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", type=int, required=True, choices=[2, 3, 4, 5, 6, 7])
    ap.add_argument("--config", default="real/mycobot_280.yaml")
    ap.add_argument("--safety", default="safety/real_mycobot.yaml")
    ap.add_argument("--port", default=None)
    ap.add_argument("--joint", type=int, default=1, help="test 2: joint index 1..6")
    ap.add_argument("--delta-deg", type=float, default=5.0)
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    setup_logging()
    cfg = load_yaml(config_path(args.config))["robot"]
    if args.port:
        cfg["port"] = args.port
    ad = MyCobotAdapter(cfg, dry_run=args.dry_run)
    ad.connect()
    s = ad.get_state()
    print("current angles (deg):", np.round(s.extra["angles_deg"], 1).tolist(), " TCP:", np.round(s.tcp.position, 3).tolist())
    if not confirm(f"TEST {args.test}: the robot WILL MOVE slowly. Clear the workspace. Emergency stop ready?"):
        print("aborted")
        return 1
    ad.arm()
    try:
        if args.test == 2:
            q = s.joints.copy()
            j = args.joint - 1
            q2 = q.copy()
            q2[j] += np.deg2rad(args.delta_deg)
            print(f"moving joint {args.joint} by +{args.delta_deg} deg ...")
            ad.move_to_joints(q2)
            time.sleep(1.0)
            s2 = ad.get_state()
            print("angles after:", np.round(s2.extra["angles_deg"], 1).tolist(), "TCP moved by (m):", np.round(s2.tcp.position - s.tcp.position, 4).tolist())
            print("firmware coords after:", s2.extra["firmware_coords"])
            print("CHECK: did the physical joint rotate in the direction our model predicts? If not, flip joint_sign in configs/real/mycobot_280.yaml")
            ad.move_to_joints(q)
        elif args.test == 3:
            T = s.tcp.as_matrix().copy()
            T[2, 3] += 0.01
            print("moving TCP +1 cm up via IK ...")
            ok = ad.set_tcp_target(T, ad._gripper_cmd)
            time.sleep(1.0)
            s2 = ad.get_state()
            print("IK ok:", ok, " measured TCP delta (m):", np.round(s2.tcp.position - s.tcp.position, 4).tolist())
            ad.set_tcp_target(s.tcp.as_matrix(), ad._gripper_cmd)
        elif args.test == 4:
            print("closing gripper ...")
            ad.set_gripper(1.0)
            time.sleep(2.0)
            print("gripper state:", ad.get_state().gripper)
            print("opening gripper ...")
            ad.set_gripper(0.0)
            time.sleep(2.0)
            print("gripper state:", ad.get_state().gripper)
        elif args.test == 5:
            print("moving to HOME posture ...")
            ad.home()
            time.sleep(1.0)
            print("TCP at home:", np.round(ad.get_state().tcp.position, 3).tolist(), "(expected ~[0.18, 0.00, 0.12] pointing down)")
        elif args.test == 6:
            sf = SafetyFilter(load_yaml(config_path(args.safety)))
            ad.home()
            seq = [Action(np.array([0, 0, -1.0]), np.zeros(3), 0.0)] * 4 + [Action(np.array([0, 0, 1.0]), np.zeros(3), 1.0)] * 4 + [Action(np.zeros(3), np.zeros(3), 0.0)] * 2
            for a in seq:
                st = ad.get_state()
                safe = sf.filter(a, st, 1.0 / cfg.get("control_hz", 5))
                if safe is None:
                    ad.stop()
                    print("safety STOP")
                    break
                ad.apply_action(safe, sf.scale)
                print("tcp:", np.round(ad.get_state().tcp.position, 3).tolist(), "gripper:", round(ad.get_state().gripper, 2))
            print("safety summary:", sf.summary())
        elif args.test == 7:
            from picking.cameras.oakd import OakDCamera
            from picking.evaluation.real_runner import RealEpisodeConfig, run_real_episode
            from picking.policies.act_policy import ACTPolicyWrapper
            from picking.teleop.keyboard import RawKeyboard

            sf = SafetyFilter(load_yaml(config_path(args.safety)))
            sf.max_tcp_speed = min(sf.max_tcp_speed, 0.03)
            cam = OakDCamera()
            cam.start()
            policy = ACTPolicyWrapper(args.checkpoint)
            rcfg = RealEpisodeConfig(instruction="Pick the red cube and place it in the blue tray.", control_hz=float(cfg.get("control_hz", 5)), max_steps=200)
            ad.home()
            print("Policy running at very low speed. Press S (success) / F (failure) / x (abort).")
            with RawKeyboard() as kb:
                r = run_real_episode(cam, ad, policy, sf, rcfg, key_poll=lambda: kb.get_key(0.0))
            print(r.to_dict())
            cam.stop()
    finally:
        ad.stop()
        ad.disconnect()
    print("test finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
