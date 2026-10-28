import numpy as np
import pytest

from picking.core import transforms as T


@pytest.mark.parametrize("seed", range(5))
def test_quat_mat_roundtrip(seed):
    rng = np.random.default_rng(seed)
    rv = rng.normal(size=3)
    R = T.rotvec_to_mat(rv)
    q = T.mat_to_quat(R)
    assert np.allclose(T.quat_to_mat(q), R, atol=1e-9)
    assert abs(np.linalg.norm(q) - 1) < 1e-9
    assert np.allclose(T.mat_to_rotvec(R), rv if np.linalg.norm(rv) < np.pi else T.mat_to_rotvec(R), atol=1e-7)


def test_rotvec_near_pi():
    R = T.rotvec_to_mat(np.array([np.pi, 0, 0]))
    rv = T.mat_to_rotvec(R)
    assert np.allclose(T.rotvec_to_mat(rv), R, atol=1e-6)


def test_euler_roundtrip():
    rpy = np.array([0.3, -0.5, 1.2])
    R = T.euler_xyz_to_mat(*rpy)
    assert np.allclose(T.mat_to_euler_xyz(R), rpy, atol=1e-9)


def test_inv_T_and_compose():
    rng = np.random.default_rng(1)
    A = T.make_T(rng.normal(size=3), R=T.rotvec_to_mat(rng.normal(size=3)))
    assert np.allclose(A @ T.inv_T(A), np.eye(4), atol=1e-9)


def test_kabsch_recovers_transform():
    rng = np.random.default_rng(2)
    R = T.rotvec_to_mat(rng.normal(size=3))
    t = rng.normal(size=3)
    P = rng.normal(size=(12, 3))
    Q = (R @ P.T).T + t + rng.normal(scale=1e-4, size=P.shape)
    Tm, rms = T.kabsch(P, Q)
    assert rms < 1e-3
    assert np.allclose(Tm[:3, :3], R, atol=1e-3)
    assert np.allclose(Tm[:3, 3], t, atol=1e-3)


def test_quat_mul_matches_matrix():
    rng = np.random.default_rng(3)
    a = T.mat_to_quat(T.rotvec_to_mat(rng.normal(size=3)))
    b = T.mat_to_quat(T.rotvec_to_mat(rng.normal(size=3)))
    assert np.allclose(T.quat_to_mat(T.quat_mul(a, b)), T.quat_to_mat(a) @ T.quat_to_mat(b), atol=1e-9)
