"""Versioned wire protocol for remote policy inference (Mac client <-> GPU server).

Messages are JSON with base64-encoded JPEG/PNG images. Every message carries protocol_version and a
timestamp; the client discards action chunks that are stale or for an outdated observation.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import cv2
import numpy as np

from picking.core.types import PROTOCOL_VERSION, ActionChunk, Observation


def encode_image(img: np.ndarray, fmt: str = ".png") -> str:
    ok, buf = cv2.imencode(fmt, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    assert ok
    return base64.b64encode(buf.tobytes()).decode()


def decode_image(s: str) -> np.ndarray:
    buf = np.frombuffer(base64.b64decode(s), np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def observation_to_msg(obs: Observation, obs_id: int, image_format: str = ".png") -> str:
    return json.dumps(
        {
            "type": "observation",
            "protocol_version": PROTOCOL_VERSION,
            "obs_id": obs_id,
            "timestamp": obs.timestamp,
            "sent_at": time.time(),
            "images": {k: encode_image(v, image_format) for k, v in obs.images.items()},
            "tcp_pose": np.asarray(obs.tcp_pose, float).tolist(),
            "joints": np.asarray(obs.joints, float).tolist(),
            "gripper": float(obs.gripper),
            "language_instruction": obs.language_instruction,
        }
    )


def msg_to_observation(d: dict) -> Observation:
    return Observation(
        timestamp=float(d["timestamp"]),
        images={k: decode_image(v) for k, v in d.get("images", {}).items()},
        tcp_pose=np.asarray(d["tcp_pose"], float),
        joints=np.asarray(d["joints"], float),
        gripper=float(d["gripper"]),
        language_instruction=d.get("language_instruction", ""),
        protocol_version=d.get("protocol_version", "?"),
    )


def action_chunk_to_msg(chunk: ActionChunk, obs_id: int) -> str:
    return json.dumps(
        {
            "type": "action_chunk",
            "protocol_version": PROTOCOL_VERSION,
            "obs_id": obs_id,
            "policy_version": chunk.policy_version,
            "timestamp": chunk.timestamp,
            "inference_latency_s": chunk.inference_latency_s,
            "actions": np.asarray(chunk.actions, float).tolist(),
        }
    )


def msg_to_action_chunk(d: dict) -> ActionChunk:
    return ActionChunk(
        policy_version=d["policy_version"],
        timestamp=float(d["timestamp"]),
        actions=np.asarray(d["actions"], float).reshape(-1, 7),
        protocol_version=d.get("protocol_version", "?"),
        inference_latency_s=float(d.get("inference_latency_s", 0.0)),
    )


def heartbeat_msg() -> str:
    return json.dumps({"type": "heartbeat", "protocol_version": PROTOCOL_VERSION, "timestamp": time.time()})


def error_msg(text: str) -> str:
    return json.dumps({"type": "error", "protocol_version": PROTOCOL_VERSION, "timestamp": time.time(), "error": text})


def check_version(d: dict[str, Any]) -> bool:
    return str(d.get("protocol_version", "")).split(".")[0] == PROTOCOL_VERSION.split(".")[0]
