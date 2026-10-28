import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("PICKING_ROOT", str(ROOT))


@pytest.fixture(scope="session")
def sim_cfg():
    from picking.utils.io import load_yaml

    return load_yaml(ROOT / "configs" / "sim" / "level1_cube.yaml")


@pytest.fixture(scope="session")
def safety_cfg():
    from picking.utils.io import load_yaml

    return load_yaml(ROOT / "configs" / "safety" / "sim_default.yaml")


@pytest.fixture(scope="session")
def env(sim_cfg):
    from picking.simulation.env import PickPlaceEnv

    e = PickPlaceEnv(sim_cfg, render=True)
    yield e
    e.close()
