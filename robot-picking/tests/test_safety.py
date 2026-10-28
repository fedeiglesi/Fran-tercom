import time

import numpy as np

from picking.core.transforms import rotvec_to_mat, mat_to_quat
from picking.core.types import Action, Pose, RobotState
from picking.safety.filter import SafetyFilter


def _state(pos=(0.18, 0.0, 0.10), tilt=0.0):
    R = rotvec_to_mat([0, tilt, 0]) @ rotvec_to_mat([np.pi, 0, 0])
    return RobotState(joints=np.zeros(6), tcp=Pose(np.array(pos, float), mat_to_quat(R)), gripper=0.0)


def test_rejects_nan(safety_cfg):
    sf = SafetyFilter(safety_cfg)
    a = Action(dpos=np.array([np.nan, 0, 0]), drot=np.zeros(3), gripper=0)
    assert sf.filter(a, _state(), 0.1) is None
    assert sf.n_rejected == 1


def test_clamps_magnitude_and_speed(safety_cfg):
    sf = SafetyFilter(safety_cfg)
    a = Action(dpos=np.array([5.0, 5.0, 5.0]), drot=np.array([3.0, 0, 0]), gripper=2.0)
    s = sf.filter(a, _state(), 0.1)
    assert s is not None
    assert np.all(np.abs(s.dpos) <= 1.0) and np.all(np.abs(s.drot) <= 1.0) and s.gripper == 1.0
    step_m = np.linalg.norm(s.dpos) * sf.scale.max_delta_pos
    assert step_m / 0.1 <= sf.max_tcp_speed + 1e-9


def test_workspace_clamp(safety_cfg):
    sf = SafetyFilter(safety_cfg)
    st = _state(pos=(0.18, 0.0, sf.lo[2] + 0.002))
    a = Action(dpos=np.array([0, 0, -1.0]), drot=np.zeros(3), gripper=0)
    s = sf.filter(a, st, 0.1)
    target_z = st.tcp.position[2] + s.dpos[2] * sf.scale.max_delta_pos
    assert target_z >= sf.lo[2] - 1e-9


def test_out_of_workspace_stops(safety_cfg):
    sf = SafetyFilter(safety_cfg)
    st = _state(pos=(0.6, 0.0, 0.1))
    assert sf.filter(Action.zero(), st, 0.1) is None


def test_orientation_cone(safety_cfg):
    sf = SafetyFilter(safety_cfg)
    st = _state(tilt=np.deg2rad(34))
    a = Action(dpos=np.zeros(3), drot=np.array([0, 1.0, 0]), gripper=0)  # would tilt further
    s = sf.filter(a, st, 0.1)
    assert s is not None and np.allclose(s.drot, 0)
    st2 = _state(tilt=np.deg2rad(60))
    assert sf.filter(Action.zero(), st2, 0.1) is None


def test_stale_action_rejected(safety_cfg):
    sf = SafetyFilter(safety_cfg)
    assert sf.filter(Action.zero(), _state(), 0.1, action_timestamp=time.time() - 5) is None


def test_timeout_without_actions(safety_cfg):
    sf = SafetyFilter(safety_cfg)
    sf.last_valid_time = time.time() - 10
    assert sf.filter(None, _state(), 0.1) is None
    assert any(e.reason == "ACTION_TIMEOUT" for e in sf.events)
