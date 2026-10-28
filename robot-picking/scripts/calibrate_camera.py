"""OAK-D -> myCobot base extrinsic calibration by point correspondences (docs/CALIBRATION.md).

Procedure (interactive):
  1. Put a small ArUco marker (DICT_4X4_50, id 0, ~30 mm) flat on the table at a spot the camera sees.
  2. Jog the robot (keyboard) until the closed gripper tip touches the marker centre; press ENTER.
     The script records: TCP position from the robot (base frame) + marker centre from the camera (pixel+depth).
  3. Move the marker to a new spot (spread over the whole workspace, 6-10 points) and repeat.
  4. Press q to solve. The result is saved only if the quality check passes (or with --force).

    uv run python scripts/calibrate_camera.py
    uv run python scripts/calibrate_camera.py --sim          # same procedure on the simulator (ground truth known)
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

from picking.calibration.extrinsics import calibrate_from_points, check_calibration_quality, next_calibration_path, save_calibration
from picking.utils.io import config_path, load_yaml, project_root
from picking.utils.logging import setup_logging


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--config", default="real/mycobot_280.yaml")
    ap.add_argument("--marker-id", type=int, default=0)
    ap.add_argument("--max-rms-mm", type=float, default=4.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--points-file", default=None, help="CSV with cam_x,cam_y,cam_z,base_x,base_y,base_z (skip interaction)")
    args = ap.parse_args()
    setup_logging()
    P_cam, P_base = [], []
    if args.points_file:
        arr = np.loadtxt(args.points_file, delimiter=",")
        P_cam, P_base = arr[:, :3], arr[:, 3:6]
        setup = {"source": args.points_file}
    elif args.sim:
        from picking.cameras.sim_camera import SimCamera
        from picking.core.transforms import inv_T
        from picking.simulation.env import PickPlaceEnv

        cfg = load_yaml(config_path("sim/level1_cube.yaml"))
        env = PickPlaceEnv(cfg)
        env.reset(seed=0)
        cam = SimCamera(env)
        T_gt = cam.pose_cv_in_base()
        k = cam.intrinsics()
        for seed in range(8):
            env.reset(seed=seed)
            p_base = env.object_pose("red_cube")[:3, 3] + [0, 0, env.object_half_extent_z("red_cube")]
            pc = inv_T(T_gt)[:3, :3] @ p_base + inv_T(T_gt)[:3, 3]
            u, v = k.fx * pc[0] / pc[2] + k.cx, k.fy * pc[1] / pc[2] + k.cy
            z = cam.read().depth[int(round(v)), int(round(u))]
            P_cam.append(cam.deproject(u, v, z))
            P_base.append(p_base)
        setup = {"source": "simulation", "ground_truth_T_base_camera": T_gt.tolist()}
    else:
        from picking.calibration.extrinsics import detect_aruco_centers
        from picking.cameras.oakd import OakDCamera
        from picking.robots.mycobot import MyCobotAdapter

        rcfg = load_yaml(config_path(args.config))["robot"]
        ad = MyCobotAdapter(rcfg)
        ad.connect()
        cam = OakDCamera()
        cam.start()
        print(__doc__)
        while True:
            cmd = input(f"[{len(P_cam)} points] ENTER = record point (robot tip touching marker centre), q = solve: ").strip()
            if cmd == "q":
                break
            fr = cam.read()
            centers = detect_aruco_centers(fr.rgb)
            if args.marker_id not in centers:
                print("marker not visible, move it / check lighting")
                continue
            u, v = centers[args.marker_id]
            win = fr.depth[int(v) - 2 : int(v) + 3, int(u) - 2 : int(u) + 3]
            z = float(np.nanmedian(win))
            if not np.isfinite(z):
                print("no valid depth at marker, move it closer / away from reflections")
                continue
            p_cam = cam.deproject(u, v, z)
            p_base = ad.get_state().tcp.position
            P_cam.append(p_cam)
            P_base.append(p_base)
            print(f"  recorded cam={np.round(p_cam,4).tolist()} base={np.round(p_base,4).tolist()}")
        cam.stop()
        ad.disconnect()
        setup = {"source": "real", "camera": "OAK-D", "robot": "myCobot 280 M5", "marker": f"aruco DICT_4X4_50 id {args.marker_id}"}
    res = calibrate_from_points(np.array(P_cam), np.array(P_base))
    ok, msg = check_calibration_quality(res, max_rms_m=args.max_rms_mm / 1000)
    print("calibration:", msg)
    print("T_base_camera =\n", np.round(res.T_base_camera, 4))
    if args.sim:
        err_t = np.linalg.norm(res.T_base_camera[:3, 3] - np.array(setup["ground_truth_T_base_camera"])[:3, 3])
        print(f"sim ground-truth translation error: {err_t*1000:.2f} mm")
    if ok or args.force:
        path = next_calibration_path(project_root() / "calibration", "oakd_to_mycobot" + ("_sim" if args.sim else ""))
        save_calibration(res, path, {"procedure": "point_correspondences (gripper tip touches ArUco centre)", "setup": setup, "accepted": bool(ok)})
        print("saved", path)
        return 0
    print("NOT saved: quality check failed. Re-do the points more carefully (spread them out, touch precisely).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
