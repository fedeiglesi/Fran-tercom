"""Policy server: hosts any `Policy` behind a WebSocket (run on the GPU box or locally)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import numpy as np

from picking.core.types import ActionChunk
from picking.networking.protocol import action_chunk_to_msg, check_version, error_msg, heartbeat_msg, msg_to_observation
from picking.policies.base import Policy

log = logging.getLogger(__name__)


class PolicyServer:
    def __init__(self, policy: Policy, host: str = "0.0.0.0", port: int = 8765, heartbeat_s: float = 1.0):
        self.policy = policy
        self.host, self.port = host, port
        self.heartbeat_s = heartbeat_s
        self.n_requests = 0
        self._server = None

    async def _handle(self, ws):
        log.info("client connected: %s", getattr(ws, "remote_address", "?"))
        hb = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(error_msg("invalid json"))
                    continue
                if not check_version(d):
                    await ws.send(error_msg(f"protocol version mismatch: {d.get('protocol_version')}"))
                    continue
                t = d.get("type")
                if t == "reset":
                    self.policy.reset(d.get("language_instruction", ""), d.get("task_info"))
                    await ws.send(json.dumps({"type": "reset_ok", "protocol_version": d["protocol_version"], "timestamp": time.time(), "policy": self.policy.name, "policy_version": self.policy.version}))
                elif t == "observation":
                    obs = msg_to_observation(d)
                    t0 = time.perf_counter()
                    try:
                        a = self.policy.act(obs)
                    except Exception as e:  # noqa: BLE001
                        log.exception("policy failed")
                        await ws.send(error_msg(f"policy error: {e}"))
                        continue
                    lat = time.perf_counter() - t0
                    actions = np.zeros((0, 7)) if a is None else a.to_array()[None]
                    chunk = ActionChunk(policy_version=self.policy.version, timestamp=time.time(), actions=actions, inference_latency_s=lat)
                    self.n_requests += 1
                    await ws.send(action_chunk_to_msg(chunk, d["obs_id"]))
                elif t == "heartbeat":
                    pass
                else:
                    await ws.send(error_msg(f"unknown message type {t}"))
        finally:
            hb.cancel()
            log.info("client disconnected")

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await ws.send(heartbeat_msg())
            except Exception:  # noqa: BLE001
                return

    async def serve(self, stop_event: asyncio.Event | None = None):
        import websockets

        async with websockets.serve(self._handle, self.host, self.port, max_size=64 * 1024 * 1024) as server:
            self._server = server
            log.info("policy server (%s v%s) listening on ws://%s:%d", self.policy.name, self.policy.version, self.host, self.port)
            if stop_event is None:
                await asyncio.Future()
            else:
                await stop_event.wait()

    def run(self):
        asyncio.run(self.serve())
