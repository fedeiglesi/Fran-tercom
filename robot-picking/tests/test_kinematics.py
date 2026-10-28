import numpy as np
import pytest

from picking.core.transforms import make_T, rotvec_to_mat
from picking.robots.kinematics import Kinematics
from picking.robots.dh import DHKinematics, UR10E_DH, UR10E_LIMITS


@pytest.fixture(scope="module")
def kin(sim_cfg):
    return Kinematics(sim_cfg["robot"])


def test_fk_home_points_down(kin):
    T = kin.fk(kin.home)
    assert T[:3, 2][2] < -0.99
    assert 0.15 < T[0, 3] < 0.21 and abs(T[1, 3]) < 0.02


@pytest.mark.parametrize("seed", range(8))
def test_ik_roundtrip(kin, seed):
    rng = np.random.default_rng(seed)
    R_down = rotvec_to_mat([np.pi, 0, 0])
    p = np.array([rng.uniform(0.15, 0.24), rng.uniform(-0.12, 0.12), rng.uniform(0.02, 0.15)])
    yaw = rng.uniform(-0.5, 0.5)
    T = make_T(p, R=rotvec_to_mat([0, 0, yaw]) @ R_down)
    q, ok, pe, re = kin.ik(T, kin.home)
    assert ok, (p, pe, re)
    assert np.all(q >= kin.lower) and np.all(q <= kin.upper)
    assert np.allclose(kin.fk(q)[:3, 3], p, atol=1e-3)


def test_ik_unreachable_reports_failure(kin):
    T = make_T([0.6, 0.0, 0.1], R=rotvec_to_mat([np.pi, 0, 0]))
    q, ok, pe, re = kin.ik(T, kin.home)
    assert not ok and pe > 0.1


def test_ur10e_dh_fk_ik():
    k = DHKinematics(UR10E_DH, UR10E_LIMITS[0], UR10E_LIMITS[1], tcp_offset=np.array([0, 0, 0.15]), home=np.deg2rad([0, -90, 90, -90, -90, 0]))
    T0 = k.fk(k.home)
    assert np.isfinite(T0).all()
    T1 = T0.copy()
    T1[:3, 3] += [0.05, -0.03, 0.02]
    q, ok, pe, re = k.ik(T1, k.home)
    assert ok and np.allclose(k.fk(q)[:3, 3], T1[:3, 3], atol=1e-3)
