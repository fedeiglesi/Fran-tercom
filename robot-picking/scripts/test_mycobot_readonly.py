"""TEST 1 - connect to the myCobot 280 and READ its state. Does not move anything.

    uv run python scripts/test_mycobot_readonly.py            # auto-detect port
    uv run python scripts/test_mycobot_readonly.py --port /dev/tty.usbserial-XXXX
Expected output: pymycobot version, firmware info, 6 joint angles (deg), firmware coords, FK TCP from our model.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from picking.robots.mycobot import MyCobotAdapter, find_serial_port
from picking.utils.io import config_path, load_yaml
from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="real/mycobot_280.yaml")
    ap.add_argument("--port", default=None)
    args = ap.parse_args()
    setup_logging()
    cfg = load_yaml(config_path(args.config))["robot"]
    if args.port:
        cfg["port"] = args.port
    print("serial ports found:", find_serial_port())
    try:
        import pymycobot

        print("pymycobot version:", getattr(pymycobot, "__version__", "unknown"))
    except ImportError:
        print("ERROR: pymycobot not installed. Run: uv sync --extra real")
        return 2
    ad = MyCobotAdapter(cfg)
    ad.connect()
    print("robot info:", json.dumps(ad.read_info(), default=str))
    for i in range(3):
        s = ad.get_state()
        print(f"[{i}] angles(deg) = {np.round(s.extra['angles_deg'], 2).tolist()}")
        print(f"    firmware coords (mm/deg, raw) = {s.extra['firmware_coords']}")
        print(f"    model FK TCP (m) = {np.round(s.tcp.position, 4).tolist()}  gripper = {s.gripper:.2f}")
    print("OK: read-only test passed. The robot did NOT move.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
