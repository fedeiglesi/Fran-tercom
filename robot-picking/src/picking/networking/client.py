"""RemotePolicy: a `Policy` whose inference happens on a policy server.

Safety behaviour: any timeout, stale response, protocol mismatch or disconnect makes act() return None,
which the runner/safety layer turns into STOP/HOLD. Reconnection is attempted on the next call.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from picking.core.types import Action, Observation
from picking.networking.protocol import check_version, msg_to_action_chunk, observation_to_msg
from picking.policies.base import Policy

log = logging.getLogger(__name__)


@dataclass
class RemoteStats:
    n_sent: int = 0
    n_ok: int = 0
    n_timeout: int = 0
    n_stale: int = 0
    n_errors: int = 0
    n_reconnects: int = 0
    latencies_ms: list[float] = field(default_factory=list)


class RemotePolicy(Policy):
    name = "remote"
    needs_images = True

    def __init__(self, url: str = "ws://127.0.0.1:8765", timeout_s: float = 0.5, max_action_age_s: float = 0.3, image_format: str = ".jpg"):
        self.url = url
        self.timeout_s = timeout_s
        self.max_action_age_s = max_action_age_s
        self.image_format = image_format
        self.stats = RemoteStats()
        self._ws = None
        self._obs_id = 0
        self._lock = threading.Lock()
        self._last_server_msg = 0.0
        self.version = "remote-unknown"
        self.instruction = ""

    # ----------------------------------------------------------------- connection (sync websockets client)
    def _connect(self) -> bool:
        from websockets.sync.client import connect

        try:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.stats.n_reconnects += 1
            self._ws = connect(self.url, open_timeout=self.timeout_s, max_size=64 * 1024 * 1024)
            self._last_server_msg = time.time()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("connect to %s failed: %s", self.url, e)
            self._ws = None
            return False

    def _recv_until(self, want_type: str, obs_id: int | None, deadline: float):
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError
            raw = self._ws.recv(timeout=remaining)
            self._last_server_msg = time.time()
            d = json.loads(raw)
            if not check_version(d):
                raise RuntimeError(f"protocol mismatch {d.get('protocol_version')}")
            if d.get("type") == "heartbeat":
                continue
            if d.get("type") == "error":
                raise RuntimeError(d.get("error"))
            if d.get("type") == want_type and (obs_id is None or d.get("obs_id") == obs_id):
                return d
            # stale / out-of-order response: drop it
            self.stats.n_stale += 1

    def connected(self) -> bool:
        return self._ws is not None

    # ----------------------------------------------------------------- Policy API
    def reset(self, instruction: str = "", task_info: dict | None = None) -> None:
        self.instruction = instruction
        with self._lock:
            if self._ws is None and not self._connect():
                return
            try:
                from picking.core.types import PROTOCOL_VERSION

                self._ws.send(json.dumps({"type": "reset", "protocol_version": PROTOCOL_VERSION, "timestamp": time.time(), "language_instruction": instruction, "task_info": task_info}))
                d = self._recv_until("reset_ok", None, time.time() + self.timeout_s * 4)
                self.version = f"{d.get('policy')}@{d.get('policy_version')}"
            except Exception as e:  # noqa: BLE001
                log.warning("reset failed: %s", e)
                self._ws = None

    def act(self, obs: Observation) -> Action | None:
        with self._lock:
            if self._ws is None and not self._connect():
                self.stats.n_errors += 1
                return None
            self._obs_id += 1
            oid = self._obs_id
            t0 = time.perf_counter()
            try:
                self._ws.send(observation_to_msg(obs, oid, self.image_format))
                self.stats.n_sent += 1
                d = self._recv_until("action_chunk", oid, time.time() + self.timeout_s)
            except TimeoutError:
                self.stats.n_timeout += 1
                log.warning("remote policy timeout (> %.2fs) -> HOLD", self.timeout_s)
                return None
            except Exception as e:  # noqa: BLE001
                self.stats.n_errors += 1
                log.warning("remote policy error: %s -> HOLD, will reconnect", e)
                self._ws = None
                return None
            chunk = msg_to_action_chunk(d)
            age = time.time() - chunk.timestamp
            if age > self.max_action_age_s or chunk.actions.shape[0] == 0:
                self.stats.n_stale += 1
                return None
            self.stats.n_ok += 1
            self.stats.latencies_ms.append((time.perf_counter() - t0) * 1000)
            return Action.from_array(chunk.actions[0])

    def info(self) -> dict:
        s = self.stats
        return {"n_sent": s.n_sent, "n_ok": s.n_ok, "n_timeout": s.n_timeout, "n_stale": s.n_stale, "n_errors": s.n_errors, "n_reconnects": s.n_reconnects, "mean_latency_ms": (sum(s.latencies_ms) / len(s.latencies_ms)) if s.latencies_ms else None}

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
