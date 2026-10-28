import numpy as np

from picking.teleop.keyboard import KeyMapper


def test_key_mapping():
    km = KeyMapper()
    e = km.map("w")
    assert np.allclose(e.action.dpos, [0.5, 0, 0]) and e.command is None
    km.map("3")
    assert np.allclose(km.map("e").action.dpos, [0, 0, -1.0])
    assert km.map("c").action.gripper == 1.0
    assert km.map("o").action.gripper == 0.0
    assert km.map("S").command == "success" and km.map("F").command == "failure"
    assert km.map("x").command == "abort" and km.map("r").command == "reset"
    assert np.allclose(km.map("l").action.drot, [0, 0, 1.0])
