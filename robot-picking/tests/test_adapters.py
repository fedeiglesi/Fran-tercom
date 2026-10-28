import numpy as np

from picking.core.transforms import make_T, rotvec_to_mat
from picking.core.types import Action
from picking.robots.base import ActionScale
from picking.robots.mycobot import MyCobotAdapter
from picking.robots.ur10e import UR10eAdapter
from picking.utils.io import load_yaml, project_root


class FakeMyCobot:
    """Minimal pymycobot stand-in: stores angles (deg) and gripper value."""

    def __init__(self):
        self.angles = [21.0, 0.2, -96.8, 6.7, 0.0, -69.0]
        self.gripper = 100
        self.calls = []

    def get_angles(self):
        return list(self.angles)

    def send_angles(self, angles, speed):
        self.calls.append(("send_angles", list(angles), speed))
        self.angles = list(angles)

    def set_gripper_value(self, v, speed):
        self.calls.append(("gripper", v))
        self.gripper = v

    def get_gripper_value(self):
        return self.gripper

    def get_coords(self):
        return [0, 0, 0, 0, 0, 0]

    def power_on(self):
        self.calls.append(("power_on",))


def test_mycobot_adapter_unit_conversion_and_arming():
    cfg = load_yaml(project_root() / "configs" / "real" / "mycobot_280.yaml")["robot"]
    fake = FakeMyCobot()
    ad = MyCobotAdapter(cfg, device=fake)
    ad.connect()
    s = ad.get_state()
    assert np.allclose(np.rad2deg(s.joints), fake.angles, atol=1e-6)
    assert s.tcp.position[2] > 0.05  # FK from the model
    # not armed -> no motion
    T = s.tcp.as_matrix().copy()
    T[2, 3] -= 0.01
    assert ad.set_tcp_target(T, 0.0) is False
    assert not any(c[0] == "send_angles" for c in fake.calls)
    ad.arm()
    assert ad.set_tcp_target(T, 1.0) is True
    sent = [c for c in fake.calls if c[0] == "send_angles"][-1]
    assert len(sent[1]) == 6 and all(np.isfinite(sent[1]))
    assert ("gripper", 0) in fake.calls  # closed -> value 0
    s2 = ad.get_state()
    assert abs(s2.tcp.position[2] - T[2, 3]) < 2e-3


def test_mycobot_refuses_out_of_limit_joints():
    cfg = load_yaml(project_root() / "configs" / "real" / "mycobot_280.yaml")["robot"]
    fake = FakeMyCobot()
    ad = MyCobotAdapter(cfg, device=fake)
    ad.connect()
    ad.arm()
    assert ad._send_angles(np.array([0, 0, 0, 0, 0, 5.0])) is False
    assert ad._send_angles(np.array([0, 0, 0, 0, 0, np.nan])) is False


def test_ur10e_mock_follows_same_policy_api():
    ad = UR10eAdapter(mock=True)
    ad.connect()
    ad.reset_targets()
    s0 = ad.get_state()
    scale = ActionScale(0.01, 0.1)
    ok = ad.apply_action(Action(dpos=np.array([1.0, 0, 0]), drot=np.zeros(3), gripper=1.0), scale)
    assert ok
    s1 = ad.get_state()
    assert abs((s1.tcp.position - s0.tcp.position)[0] - 0.01) < 1e-3
    assert s1.gripper == 1.0
