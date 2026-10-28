import asyncio
import threading
import time

import numpy as np
import pytest

from picking.core.types import Action, Observation
from picking.networking.client import RemotePolicy
from picking.networking.server import PolicyServer
from picking.policies.base import Policy


class EchoPolicy(Policy):
    name, version, needs_images = "echo", "1", True

    def __init__(self, delay=0.0):
        self.delay = delay
        self.instruction = None

    def reset(self, instruction="", task_info=None):
        self.instruction = instruction

    def act(self, obs):
        time.sleep(self.delay)
        return Action(dpos=obs.tcp_pose[:3] * 0 + 0.5, drot=np.zeros(3), gripper=float(obs.images["cam"].shape[0] == 32))


def _start(policy, port):
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    srv = PolicyServer(policy, "127.0.0.1", port)

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.serve(stop))

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.5)
    return loop, stop


def _obs():
    return Observation(timestamp=time.time(), images={"cam": np.zeros((32, 32, 3), np.uint8)}, tcp_pose=np.array([0.1, 0, 0.1, 0, 1, 0, 0.0]), joints=np.zeros(6), gripper=0.0, language_instruction="pick")


@pytest.mark.timeout(60)
def test_remote_roundtrip_and_timeout():
    port = 8791
    pol = EchoPolicy()
    loop, stop = _start(pol, port)
    client = RemotePolicy(f"ws://127.0.0.1:{port}", timeout_s=1.0)
    client.reset("pick the thing")
    assert pol.instruction == "pick the thing"
    a = client.act(_obs())
    assert a is not None and np.allclose(a.dpos, 0.5) and a.gripper == 1.0
    assert client.info()["n_ok"] == 1
    # slow server -> timeout -> None (HOLD)
    pol.delay = 2.0
    client.timeout_s = 0.3
    assert client.act(_obs()) is None
    assert client.info()["n_timeout"] == 1
    client.close()
    loop.call_soon_threadsafe(stop.set)


@pytest.mark.timeout(60)
def test_remote_server_down_returns_none():
    client = RemotePolicy("ws://127.0.0.1:8799", timeout_s=0.3)
    assert client.act(_obs()) is None
    assert client.info()["n_errors"] >= 1
