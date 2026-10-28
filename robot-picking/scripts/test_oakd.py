"""Check the OAK-D: prints device, intrinsics, frame rate, depth statistics; saves sample images.

    uv run python scripts/test_oakd.py --frames 60 --out reports/oakd_check
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--out", default="reports/oakd_check")
    args = ap.parse_args()
    setup_logging()
    try:
        import depthai as dai

        print("depthai version:", dai.__version__)
        devs = dai.Device.getAllAvailableDevices()
        print("devices:", [d.getDeviceId() if hasattr(d, "getDeviceId") else str(d) for d in devs])
    except ImportError:
        print("ERROR: depthai not installed. Run: uv sync --extra real")
        return 2
    from picking.cameras.oakd import OakDCamera

    cam = OakDCamera()
    cam.start()
    print("intrinsics:", cam.intrinsics().to_dict())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    last = None
    for i in range(args.frames):
        last = cam.read()
    fps = args.frames / (time.time() - t0)
    print(f"fps ~ {fps:.1f}, rgb {last.rgb.shape}, depth {'none' if last.depth is None else last.depth.shape}")
    if last.depth is not None:
        d = last.depth[np.isfinite(last.depth)]
        print(f"depth valid {100*len(d)/last.depth.size:.1f}%  min {d.min():.3f} m  median {np.median(d):.3f} m  max {d.max():.3f} m")
        import cv2

        dn = np.nan_to_num(last.depth, nan=0.0)
        cv2.imwrite(str(out / "depth.png"), (np.clip(dn / 1.5, 0, 1) * 255).astype(np.uint8))
    import imageio

    imageio.imwrite(out / "rgb.png", last.rgb)
    print("saved", out / "rgb.png")
    cam.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
