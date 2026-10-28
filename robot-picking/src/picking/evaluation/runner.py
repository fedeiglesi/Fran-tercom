"""Episode runner: SEE -> POLICY -> SAFETY -> ROBOT -> EVALUATE, identical for sim and real.

For simulation the privileged state (object pose) is attached to obs.extra["privileged"] so
scripted policies and the evaluator can use it; learned policies must ignore it.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from picking.core.transforms import mat_to_euler_xyz
from picking.core.types import Action, EpisodeResult, Observation
from picking.evaluation.evaluator import EpisodeEvaluator
from picking.policies.base import Policy
from picking.robots.sim import SimRobotAdapter
from picking.safety.filter import SafetyFilter
from picking.simulation.env import PickPlaceEnv

log = logging.getLogger(__name__)


@dataclass
class StepCallbackData:
    step: int
    obs: Observation
    policy_action: Action | None
    safe_action: Action | None
    state_before: object
    state_after: object
    privileged: dict
    info: dict = field(default_factory=dict)


def build_observation(env: PickPlaceEnv, adapter: SimRobotAdapter, instruction: str, image_size: tuple[int, int] | None, with_images: bool, with_depth: bool = False) -> tuple[Observation, dict]:
    state = adapter.get_state()
    images = {}
    depth = {}
    if with_images:
        rgb = env.render_rgb()
        crop = env.cfg["camera"].get("policy_crop")
        if crop:
            x0, y0, x1, y1 = crop
            rgb = rgb[y0:y1, x0:x1]
        if image_size is not None and (rgb.shape[1], rgb.shape[0]) != tuple(image_size):
            rgb = cv2.resize(rgb, tuple(image_size), interpolation=cv2.INTER_AREA)
        images[env.cam_name] = np.ascontiguousarray(rgb)
        if with_depth:
            d = env.render_depth()
            if crop:
                d = d[y0:y1, x0:x1]
            if image_size is not None:
                d = cv2.resize(d, tuple(image_size), interpolation=cv2.INTER_NEAREST)
            depth[env.cam_name] = np.ascontiguousarray(d)
    target = env.cfg["task"]["target_object"]
    T_obj = env.object_pose(target)
    priv = {
        "object_pos": T_obj[:3, 3].copy(),
        "object_yaw": float(mat_to_euler_xyz(T_obj[:3, :3])[2]),
        "tray_pos": env.tray_center(),
        "object_id": target,
    }
    obs = Observation(
        timestamp=time.time(),
        images=images,
        depth=depth,
        tcp_pose=np.concatenate([state.tcp.position, state.tcp.quaternion]),
        joints=state.joints,
        gripper=state.gripper,
        language_instruction=instruction,
        extra={"privileged": priv, "sim_time": env.sim_time()},
    )
    return obs, priv


def run_episode(
    env: PickPlaceEnv,
    adapter: SimRobotAdapter,
    policy: Policy,
    safety: SafetyFilter,
    seed: int,
    instruction: str | None = None,
    image_size: tuple[int, int] | None = None,
    with_depth: bool = False,
    step_callback: Callable[[StepCallbackData], None] | None = None,
    episode_id: str | None = None,
    randomize: bool | None = None,
    settle_steps_after_done: int = 5,
    force_images: bool = False,
) -> EpisodeResult:
    cfg = env.cfg
    task_cfg = cfg["task"]
    instruction = instruction or task_cfg["instruction"]
    target = task_cfg["target_object"]
    episode_id = episode_id or f"ep_{seed}_{uuid.uuid4().hex[:8]}"
    env.reset(seed=seed, randomize=randomize)
    adapter.reset_targets()
    policy.reset(instruction, {"target_object": target})
    safety.events.clear()
    safety.n_rejected = safety.n_clamped = 0
    evaluator = EpisodeEvaluator(target_object=target, lift_height=float(task_cfg["lift_height"]), max_steps=env.max_steps)
    dt = 1.0 / env.control_hz
    with_images = (policy.needs_images or force_images) and env.renderer is not None
    other_objs = [o for o in env.object_ids if o != target]
    other_init = {o: env.object_pose(o)[:3, 3].copy() for o in other_objs}
    t_start = time.time()
    latencies = []
    timeout = False
    n_steps = 0
    done_countdown = None
    for step in range(env.max_steps):
        obs, priv = build_observation(env, adapter, instruction, image_size, with_images, with_depth)
        state_before = adapter.get_state()
        t0 = time.perf_counter()
        action = policy.act(obs)
        latencies.append((time.perf_counter() - t0) * 1000)
        safe = safety.filter(action, state_before, dt)
        if safe is None:
            adapter.stop()
            evaluator.n_safety_rejects += 1
        else:
            ok = adapter.apply_action(safe, safety.scale)
            if not ok:
                evaluator.n_ik_failures += 1
        state_after = adapter.get_state()
        contacts = adapter.last_contacts
        obj_pos = env.object_pose(target)[:3, 3]
        moved_other = any(np.linalg.norm(env.object_pose(o)[:3, 3] - other_init[o]) > 0.01 for o in other_objs)
        evaluator.update(env.sim_time(), state_after.tcp.position, obj_pos, state_after.gripper, contacts, env.object_in_tray(target), moved_other)
        n_steps = step + 1
        if step_callback is not None:
            step_callback(StepCallbackData(step, obs, action, safe, state_before, state_after, priv, {"contacts": contacts, "in_tray": env.object_in_tray(target)}))
        if evaluator.collision and cfg["sim"].get("stop_on_collision", True):
            break
        if done_countdown is None and policy.done():
            done_countdown = settle_steps_after_done
        if done_countdown is not None:
            done_countdown -= 1
            if done_countdown <= 0:
                break
    else:
        timeout = True
    cycle_time = env.sim_time()
    final_state = adapter.get_state()
    result = evaluator.result(
        episode_id=episode_id,
        seed=seed,
        task=instruction,
        n_steps=n_steps,
        cycle_time=cycle_time,
        final_in_tray=env.object_in_tray(target),
        final_gripper=final_state.gripper,
        obj_vel=env.object_velocity(target),
        n_retries=int(policy.info().get("retries", 0)),
        timeout=timeout,
        metadata={
            "policy": policy.name,
            "policy_version": policy.version,
            "mean_policy_latency_ms": float(np.mean(latencies)) if latencies else 0.0,
            "wall_time_s": time.time() - t_start,
            "randomization": env.episode_rand.to_dict() if env.episode_rand else None,
            "safety": safety.summary(),
            "policy_info": policy.info(),
            "final_tcp": final_state.tcp.position.tolist(),
            "final_object_pos": env.object_pose(target)[:3, 3].tolist(),
        },
    )
    return result
