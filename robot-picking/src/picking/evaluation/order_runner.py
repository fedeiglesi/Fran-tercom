"""Level 7: fulfil an ORDER = sequence of SKUs picked from the source bin into the order bin (which already
holds other products). One env reset per order; one pick-place cycle per item with the same policy."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from picking.core.types import Action
from picking.evaluation.runner import run_episode


@dataclass
class OrderResult:
    order_id: str
    seed: int
    skus: list[str]
    items: list[dict]
    success: bool  # every item placed, nothing else disturbed, no collision
    n_ok: int
    wrong_sku: int
    prefilled_disturbed: bool
    collision: bool
    cycle_time_s: float
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__


def _recover_up(env, adapter, safety, z_safe: float = 0.10, max_steps: int = 40) -> None:
    """RECOVER after a collision: raise the TCP (through the safety filter) until it is clear of the bin walls,
    so the next item does not start in contact. Pure vertical retreat = the safest motion from any contact."""
    dt = 1.0 / env.control_hz
    for _ in range(max_steps):
        st = adapter.get_state()
        if st.tcp.position[2] >= z_safe:
            break
        a = Action(dpos=np.array([0.0, 0.0, 0.5]), drot=np.zeros(3), gripper=0.0)
        safe = safety.filter(a, st, dt)
        if safe is None:
            adapter.stop()
        else:
            adapter.apply_action(safe, safety.scale)


def run_order(env, adapter, policy, safety, seed: int, image_size=None, full_frame: bool = True, max_retries_per_item: int = 1, step_callback=None, recover_after_collision: bool = True) -> OrderResult:
    env.reset(seed=seed)
    adapter.reset_targets()
    ident = getattr(policy, "sku_identifier", None)
    if ident is not None and hasattr(ident, "rng"):
        ident.rng = np.random.default_rng(seed)  # per-order seeding: any order is reproducible on its own
    src, dst = env.order_task["source_bin"], env.order_task["order_bin"]
    prefilled = list(env.objects_in_bin(dst))
    pre_pos = {o: env.object_pose(o)[:3, 3].copy() for o in prefilled}
    items = []
    t0 = env.sim_time()
    collision = False
    wrong = 0
    def clearance(oid):
        c, hs = env.bin_center(src), env.bin_inner_halfsize(src)
        p = env.object_pose(oid)[:3, 3][:2]
        wall = float(min(hs[0] - abs(p[0] - c[0]), hs[1] - abs(p[1] - c[1])))
        neigh = min([float(np.linalg.norm(env.object_pose(o)[:3, 3][:2] - p)) for o in env.objects_in_bin(src) if o != oid], default=1.0)
        return min(wall, neigh)

    sequence = sorted(env.order, key=lambda o: -clearance(o))  # most accessible first
    for k, oid in enumerate(sequence):
        env.place_slot = env.free_slot(dst)[:2]
        spec = env.object_specs[oid]
        attempt = 0
        while True:
            cb = (lambda d, _k=k, _a=attempt: step_callback(_k, _a, d)) if step_callback is not None else None
            r = run_episode(env, adapter, policy, safety, seed=seed * 100 + k, image_size=image_size, full_frame=full_frame, env_reset=False, target_override=oid, episode_id=f"order{seed}_item{k}_{oid}", step_callback=cb)
            labels = r.metadata.get("policy_info", {}).get("sku_labels", [])
            moved_other = any(np.linalg.norm(env.object_pose(o)[:3, 3] - pre_pos[o]) > 0.02 for o in prefilled)
            wrong_pick = (not r.success) and r.failure_type == "WRONG_OBJECT"
            items.append({"sku": spec.get("sku", oid), "object": oid, "packaging": spec.get("packaging", "loose"), "attempt": attempt, "success": r.success, "failure_type": r.failure_type, "steps": r.n_steps, "collision": r.collision, "collision_detail": r.metadata.get("collision_detail", ""), "sku_labels": labels[:6]})
            collision |= r.collision
            wrong += int(wrong_pick)
            if r.collision and recover_after_collision:
                _recover_up(env, adapter, safety)
            if r.success or r.collision or attempt >= max_retries_per_item:
                break
            attempt += 1
    disturbed = any(np.linalg.norm(env.object_pose(o)[:3, 3] - pre_pos[o]) > 0.02 for o in prefilled)
    placed_ok = [it["success"] for it in items]
    n_ok = sum(1 for oid in env.order if any(it["object"] == oid and it["success"] for it in items))
    success = n_ok == len(env.order) and not disturbed and not collision
    return OrderResult(f"order_{seed}", seed, [env.object_specs[o].get("sku", o) for o in env.order], items, success, n_ok, wrong, disturbed, collision, env.sim_time() - t0, {"source_items": env.order_task["n_source_items"], "prefilled": prefilled, "sequence": sequence})
