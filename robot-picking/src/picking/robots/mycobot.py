"""myCobot 280 M5 adapter over pymycobot (serial/USB). Robot-specific code lives ONLY here.

Design decisions:
  * Cartesian targets are converted to joint targets with OUR kinematic model (URDF-based), then sent with
    send_angles(). We do not rely on the firmware's Cartesian solver nor on its Euler convention.
  * Every method is defensive: it never moves the robot unless `armed` is True and the target is within the
    configured joint limits (with margin). `dry_run=True` logs instead of sending.
  * Units at the boundary: pymycobot uses degrees and millimetres; the rest of the project uses rad / m.
"""
from __future__ import annotations

import glob
import logging
import platform
import time

import numpy as np

from picking.core.types import Pose, RobotState
from picking.robots.base import RobotAdapter
from picking.robots.kinematics import Kinematics

log = logging.getLogger(__name__)


def find_serial_port() -> str | None:
    pats = ["/dev/tty.usbserial*", "/dev/tty.SLAB_USBtoUART*", "/dev/tty.wchusbserial*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*"]
    if platform.system() == "Linux":
        pats = ["/dev/ttyUSB*", "/dev/ttyACM*"]
    for p in pats:
        hits = sorted(glob.glob(p))
        if hits:
            return hits[0]
    return None


class MyCobotAdapter(RobotAdapter):
    name = "mycobot_280_m5"

    def __init__(self, cfg: dict, dry_run: bool = False, device=None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.kin = Kinematics(cfg)
        self.sign = np.asarray(cfg.get("joint_sign", [1] * 6), float)
        self.offset_deg = np.asarray(cfg.get("joint_offset_deg", [0] * 6), float)
        self.speed = int(cfg.get("move_speed", 20))
        self.gripper_speed = int(cfg.get("gripper_speed", 50))
        self.g_open = int(cfg.get("gripper_value_open", 100))
        self.g_closed = int(cfg.get("gripper_value_closed", 0))
        self.settle = float(cfg.get("settle_time_s", 0.15))
        self.margin = np.deg2rad(3.0)
        self.mc = device  # injected fake for tests
        self.armed = False
        self.connected = False
        self._gripper_cmd = 0.0
        self._last_q = None
        self._last_sent_q = None
        self.ik_failures = 0
        self.last_contacts = None
        self.log_rows: list[dict] = []

    # ---------------------------------------------------------------- connection
    def connect(self) -> None:
        if self.mc is None:
            port = self.cfg.get("port", "auto")
            if port == "auto":
                port = find_serial_port()
                if port is None:
                    raise RuntimeError("No serial port found. Connect the myCobot USB cable (see docs/HARDWARE.md).")
            import pymycobot  # lazy import: only needed on the lab machine

            cls_name = self.cfg.get("pymycobot_class", "MyCobot280")
            cls = getattr(pymycobot, cls_name, None) or getattr(pymycobot, "MyCobot")
            self.mc = cls(port, int(self.cfg.get("baudrate", 115200)))
            log.info("connected to %s on %s with pymycobot %s (%s)", cls.__name__, port, getattr(pymycobot, "__version__", "?"), cls_name)
        self.connected = True
        info = self.read_info()
        log.info("robot info: %s", info)

    def read_info(self) -> dict:
        info = {}
        for name in ("get_system_version", "get_basic_version", "get_robot_id", "is_power_on", "get_fresh_mode", "get_error_information"):
            fn = getattr(self.mc, name, None)
            if fn is None:
                continue
            try:
                info[name] = fn()
            except Exception as e:  # noqa: BLE001
                info[name] = f"error: {e}"
        return info

    def disconnect(self) -> None:
        self.armed = False
        self.connected = False

    def arm(self) -> None:
        """Explicitly enable motion. Never armed by default."""
        if hasattr(self.mc, "power_on") and not self.dry_run:
            try:
                self.mc.power_on()
            except Exception as e:  # noqa: BLE001
                log.warning("power_on failed: %s", e)
        self.armed = True

    def release(self) -> None:
        """Free all servos (robot becomes compliant; hold it!)."""
        self.armed = False
        if not self.dry_run and hasattr(self.mc, "release_all_servos"):
            self.mc.release_all_servos()

    # ---------------------------------------------------------------- unit conversion
    def deg_to_model(self, deg) -> np.ndarray:
        return self.sign * np.deg2rad(np.asarray(deg, float) - self.offset_deg)

    def model_to_deg(self, q) -> np.ndarray:
        return np.rad2deg(np.asarray(q, float) * self.sign) + self.offset_deg

    # ---------------------------------------------------------------- state
    def read_angles_deg(self) -> np.ndarray:
        for _ in range(5):
            a = self.mc.get_angles()
            if isinstance(a, (list, tuple)) and len(a) == 6 and all(isinstance(v, (int, float)) for v in a):
                return np.asarray(a, float)
            time.sleep(0.05)
        raise RuntimeError(f"get_angles returned invalid data: {a!r}")

    def get_state(self) -> RobotState:
        deg = self.read_angles_deg()
        q = self.deg_to_model(deg)
        self._last_q = q
        T = self.kin.fk(q)
        coords = None
        if hasattr(self.mc, "get_coords"):
            try:
                coords = self.mc.get_coords()
            except Exception:  # noqa: BLE001
                coords = None
        g = self._gripper_cmd
        if hasattr(self.mc, "get_gripper_value"):
            try:
                v = self.mc.get_gripper_value()
                if isinstance(v, (int, float)) and v >= 0:
                    g = float(np.clip((self.g_open - v) / (self.g_open - self.g_closed), 0, 1))
            except Exception:  # noqa: BLE001
                pass
        return RobotState(joints=q, tcp=Pose.from_matrix(T), gripper=g, timestamp=time.time(), extra={"angles_deg": deg.tolist(), "firmware_coords": coords, "gripper_cmd": self._gripper_cmd})

    @property
    def joint_limits(self):
        return self.kin.lower, self.kin.upper

    def fk(self, q):
        return self.kin.fk(q)

    # ---------------------------------------------------------------- motion
    def _check_joints(self, q: np.ndarray) -> bool:
        q = np.asarray(q, float)
        if q.shape != (6,) or not np.all(np.isfinite(q)):
            log.error("invalid joint target %s", q)
            return False
        if np.any(q < self.kin.lower + self.margin) or np.any(q > self.kin.upper - self.margin):
            log.error("joint target outside limits: %s", np.rad2deg(q).round(1))
            return False
        return True

    def _send_angles(self, q: np.ndarray, speed: int | None = None) -> bool:
        if not self._check_joints(q):
            return False
        if not self.armed:
            log.error("robot not armed: refusing to move (call adapter.arm())")
            return False
        deg = [float(round(v, 2)) for v in self.model_to_deg(q)]
        self._last_sent_q = np.asarray(q, float).copy()
        self.log_rows.append({"t": time.time(), "cmd": "send_angles", "deg": deg})
        if self.dry_run:
            log.info("[dry-run] send_angles(%s, %d)", deg, speed or self.speed)
            return True
        self.mc.send_angles(deg, int(speed or self.speed))
        return True

    def move_to_joints(self, q, gripper=None, blocking=True) -> None:
        ok = self._send_angles(q)
        if gripper is not None:
            self.set_gripper(gripper)
        if ok and blocking and not self.dry_run:
            self._wait_until_reached(q)

    def _wait_until_reached(self, q, tol_deg: float = 2.0, timeout: float = 8.0) -> bool:
        t0 = time.time()
        target = self.model_to_deg(q)
        while time.time() - t0 < timeout:
            try:
                cur = self.read_angles_deg()
            except RuntimeError:
                time.sleep(0.1)
                continue
            if np.max(np.abs(cur - target)) < tol_deg:
                return True
            time.sleep(0.05)
        log.warning("move did not reach target within %.1fs", timeout)
        return False

    def commanded_tcp(self):
        return None if self._last_sent_q is None else self.kin.fk(self._last_sent_q)

    def set_tcp_target(self, T_target, gripper, blocking=True) -> bool:
        q0 = self._last_sent_q if self._last_sent_q is not None else (self._last_q if self._last_q is not None else self.kin.home)
        q, ok, pe, re = self.kin.ik(T_target, q0)
        if not ok:
            self.ik_failures += 1
            log.warning("IK failed (pos err %.4f m, rot err %.4f rad): holding", pe, re)
            return False
        sent = self._send_angles(q)
        if sent:
            self.set_gripper(gripper)
            time.sleep(self.settle)
        return sent

    def set_gripper(self, g: float) -> None:
        g = float(np.clip(g, 0, 1))
        if abs(g - self._gripper_cmd) < 0.02:
            return
        self._gripper_cmd = g
        value = int(round(self.g_open + (self.g_closed - self.g_open) * g))
        self.log_rows.append({"t": time.time(), "cmd": "set_gripper_value", "value": value})
        if self.dry_run or not self.armed:
            log.info("[dry-run/not-armed] set_gripper_value(%d)", value)
            return
        self.mc.set_gripper_value(value, self.gripper_speed)

    def stop(self) -> None:
        self.log_rows.append({"t": time.time(), "cmd": "stop"})
        if self.dry_run:
            return
        if hasattr(self.mc, "stop"):
            try:
                self.mc.stop()
            except Exception as e:  # noqa: BLE001
                log.error("stop() failed: %s", e)
        # hold current position explicitly
        try:
            deg = self.read_angles_deg()
            if self.armed:
                self.mc.send_angles([float(v) for v in deg], 10)
        except Exception as e:  # noqa: BLE001
            log.error("hold failed: %s", e)

    def home(self) -> None:
        self.move_to_joints(self.kin.home, gripper=0.0, blocking=True)

    def reset_targets(self) -> None:
        self.ik_failures = 0
        self._last_q = None
        self._last_sent_q = None
