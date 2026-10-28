"""Generic SEE -> POLICY -> SAFETY -> ROBOT loop over the abstract Camera / RobotAdapter interfaces.

Used for the real robot (OAK-D + myCobot/UR10e) and for teleoperation. Success is decided by the
operator (keys) or by an optional automatic verifier; everything is recorded, failures included.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from picking.cameras.base import Camera
from picking.core.types import Action, EpisodeResult, Observation
from picking.policies.base import Policy
from picking.robots.base import RobotAdapter
from picking.safety.filter import SafetyFilter

log = logging.getLogger(__name__)


@dataclass
class RealEpisodeConfig:
    instruction: str
    control_hz: float = 5.0
    max_steps: int = 300
    image_size: tuple[int, int] | None = (128, 128)
    crop: tuple[int, int, int, int] | None = None
    camera_key: str = "oakd"
    target_object: str = "unknown"
    extra: dict = field(default_factory=dict)


def make_observation(cam: Camera, adapter: RobotAdapter, cfg: RealEpisodeConfig) -> Observation:
    fr = cam.read()
    st = adapter.get_state()
    rgb = fr.rgb
    depth = fr.depth
    if cfg.crop:
        x0, y0, x1, y1 = cfg.crop
        rgb = rgb[y0:y1, x0:x1]
        depth = None if depth is None else depth[y0:y1, x0:x1]
    if cfg.image_size:
        rgb = cv2.resize(rgb, tuple(cfg.image_size), interpolation=cv2.INTER_AREA)
        if depth is not None:
            depth = cv2.resize(depth, tuple(cfg.image_size), interpolation=cv2.INTER_NEAREST)
    return Observation(
        timestamp=fr.timestamp,
        images={cfg.camera_key: np.ascontiguousarray(rgb)},
        depth={} if depth is None else {cfg.camera_key: np.ascontiguousarray(depth)},
        tcp_pose=np.concatenate([st.tcp.position, st.tcp.quaternion]),
        joints=st.joints,
        gripper=st.gripper,
        language_instruction=cfg.instruction,
        extra={"camera_latency_ms": getattr(cam, "last_latency_ms", 0.0), "robot_extra": st.extra, "privileged": {"object_pos": np.zeros(3), "object_yaw": 0.0, "tray_pos": np.zeros(3)}},
    )


def run_real_episode(
    cam: Camera,
    adapter: RobotAdapter,
    policy: Policy,
    safety: SafetyFilter,
    cfg: RealEpisodeConfig,
    key_poll: Callable[[], str | None] | None = None,
    step_callback: Callable | None = None,
    corrections: Callable[[Observation, Action | None], Action | None] | None = None,
) -> EpisodeResult:
    """`key_poll` returns 'S' (success), 'F' (failure), 'x' (abort) or None. `corrections` implements
    DAgger-style human override: returns a corrected action or None to keep the policy's."""
    from picking.evaluation.runner import StepCallbackData

    episode_id = f"real_{uuid.uuid4().hex[:8]}"
    dt = 1.0 / cfg.control_hz
    policy.reset(cfg.instruction, {"target_object": cfg.target_object})
    safety.events.clear()
    safety.n_rejected = safety.n_clamped = 0
    t_start = time.time()
    outcome = None
    n_steps = 0
    path = 0.0
    last_p = None
    latencies = []
    n_corrections = 0
    for step in range(cfg.max_steps):
        t_step = time.time()
        obs = make_observation(cam, adapter, cfg)
        state_before = adapter.get_state()
        t0 = time.perf_counter()
        a_policy = policy.act(obs)
        latencies.append((time.perf_counter() - t0) * 1000)
        a = a_policy
        if corrections is not None:
            a_corr = corrections(obs, a_policy)
            if a_corr is not None:
                a = a_corr
                n_corrections += 1
        safe = safety.filter(a, state_before, dt, action_timestamp=obs.timestamp if a is not None else None)
        if safe is None:
            adapter.stop()
        else:
            if not adapter.apply_action(safe, safety.scale):
                log.warning("IK failed: holding")
        state_after = adapter.get_state()
        p = state_after.tcp.position
        if last_p is not None:
            path += float(np.linalg.norm(p - last_p))
        last_p = p
        n_steps = step + 1
        if step_callback is not None:
            step_callback(StepCallbackData(step, obs, a_policy, safe, state_before, state_after, obs.extra["privileged"], {"corrected": a is not a_policy}))
        if key_poll is not None:
            k = key_poll()
            if k in ("S", "F", "x"):
                outcome = {"S": "success", "F": "failure", "x": "abort"}[k]
                break
        if policy.done():
            break
        rem = dt - (time.time() - t_step)
        if rem > 0:
            time.sleep(rem)
    if outcome == "abort":
        adapter.stop()
    timeout = outcome is None and n_steps >= cfg.max_steps
    success = outcome == "success"
    ft = "NONE" if success else ("TIMEOUT" if timeout else "SAFETY_STOP" if outcome == "abort" else "PLACE_FAIL")
    return EpisodeResult(
        episode_id=episode_id, success=success, grasp_success=success, place_success=success, collision=False, drop=False, timeout=timeout,
        failure_type=ft, n_steps=n_steps, cycle_time_s=time.time() - t_start, path_length_m=path, n_retries=int(policy.info().get("retries", 0)),
        seed=-1, task=cfg.instruction, object_id=cfg.target_object,
        metadata={"policy": policy.name, "policy_version": policy.version, "mean_policy_latency_ms": float(np.mean(latencies)) if latencies else 0.0, "safety": safety.summary(), "operator_outcome": outcome, "n_corrections": n_corrections, "real": True, **cfg.extra},
    )
