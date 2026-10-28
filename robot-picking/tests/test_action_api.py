import numpy as np

from picking.core.transforms import make_T, rotvec_to_mat
from picking.core.types import ACTION_DIM, STATE_DIM, Action, Observation
from picking.robots.base import ActionScale, target_from_delta


def test_action_roundtrip():
    a = Action(dpos=np.array([0.1, -0.2, 0.3]), drot=np.array([0, 0.5, 0]), gripper=0.7)
    arr = a.to_array()
    assert arr.shape == (ACTION_DIM,)
    b = Action.from_array(arr)
    assert np.allclose(a.to_array(), b.to_array())
    assert a.is_finite()
    assert not Action(dpos=np.array([np.nan, 0, 0]), drot=np.zeros(3), gripper=0).is_finite()


def test_denormalize_and_target():
    scale = ActionScale(max_delta_pos=0.01, max_delta_rot=0.1)
    a = Action(dpos=np.array([1.0, -0.5, 2.0]), drot=np.zeros(3), gripper=1.5)
    dpos, drot, g = scale.denormalize(a)
    assert np.allclose(dpos, [0.01, -0.005, 0.01])  # clipped to [-1,1] first
    assert g == 1.0
    T0 = make_T([0.1, 0.0, 0.1], R=rotvec_to_mat([np.pi, 0, 0]))
    T1 = target_from_delta(T0, dpos, np.array([0, 0, 0.1]))
    assert np.allclose(T1[:3, 3], [0.11, -0.005, 0.11])
    # rotation applied in base frame: z axis still down
    assert np.allclose(T1[:3, 2], [0, 0, -1], atol=1e-9)


def test_state_vector_is_continuous_near_180deg():
    # quaternions q and -q represent the same rotation; the 6D state must be identical
    obs1 = Observation(timestamp=0, images={}, tcp_pose=np.array([0.1, 0, 0.1, 1e-6, 1, 0, 0]), joints=np.zeros(6), gripper=0.0)
    obs2 = Observation(timestamp=0, images={}, tcp_pose=np.array([0.1, 0, 0.1, -1e-6, -1, 0, 0]), joints=np.zeros(6), gripper=0.0)
    s1, s2 = obs1.state_vector(), obs2.state_vector()
    assert s1.shape == (STATE_DIM,)
    assert np.allclose(s1, s2, atol=1e-5)
