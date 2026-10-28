import numpy as np

from picking.core.types import Action, Observation
from picking.teleop.dagger import HumanCorrector


def test_human_corrector_overrides_then_returns_control():
    keys = iter(["w", None, None, None, None, "S"])
    hc = HumanCorrector(key_source=lambda: next(keys, None), hold_steps=2)
    obs = Observation(timestamp=0, images={}, tcp_pose=np.array([0.1, 0, 0.1, 0, 1, 0, 0.0]), joints=np.zeros(6), gripper=0.0)
    pa = Action.zero()
    a1 = hc(obs, pa)
    assert a1 is not None and a1.dpos[0] > 0  # human override
    a2 = hc(obs, pa)
    assert a2 is not None  # hold
    a3 = hc(obs, pa)
    assert a3 is None  # back to policy
    hc(obs, pa)
    hc(obs, pa)
    assert hc(obs, pa) is None and hc.last_command == "success"
    assert 0 < hc.correction_rate() < 1
