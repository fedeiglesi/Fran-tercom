"""Objective episode evaluation: pick / place / collision / drop / timeout + failure classification."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from picking.core.types import EpisodeResult, FailureType
from picking.simulation.env import ContactSummary


@dataclass
class StepRecord:
    t: float
    tcp_pos: np.ndarray
    obj_pos: np.ndarray
    gripper: float
    pads_on_object: int  # bitmask: 1 left, 2 right
    contacts: ContactSummary
    in_tray: bool


@dataclass
class EpisodeEvaluator:
    target_object: str
    lift_height: float  # object must rise this much above its rest height
    max_steps: int
    collision_force_threshold: float = 5.0  # [N] pad-table contact above this = collision
    rest_height: float | None = None
    steps: list[StepRecord] = field(default_factory=list)
    path_length: float = 0.0
    collision: bool = False
    collision_detail: str = ""
    grasped: bool = False  # both pads on object and object lifted
    lifted: bool = False
    drop: bool = False
    max_obj_height: float = -1.0
    n_ik_failures: int = 0
    n_safety_rejects: int = 0
    wrong_object_contact: bool = False

    def update(self, t: float, tcp_pos, obj_pos, gripper: float, contacts: ContactSummary, in_tray: bool, other_objects_moved: bool = False) -> None:
        obj_pos = np.asarray(obj_pos, float)
        tcp_pos = np.asarray(tcp_pos, float)
        if self.rest_height is None:
            self.rest_height = float(obj_pos[2])
        if self.steps:
            self.path_length += float(np.linalg.norm(tcp_pos - self.steps[-1].tcp_pos))
        pads = contacts.finger_object.get(self.target_object, 0)
        rec = StepRecord(t, tcp_pos, obj_pos, gripper, pads, contacts, in_tray)
        self.steps.append(rec)
        # collisions
        if contacts.robot_table or contacts.robot_self or contacts.robot_tray or contacts.robot_object:
            self.collision = True
            self.collision_detail = (
                "table" if contacts.robot_table else "self" if contacts.robot_self else "tray" if contacts.robot_tray else "object_with_arm"
            )
        if contacts.finger_table and contacts.max_force > self.collision_force_threshold:
            self.collision = True
            self.collision_detail = f"finger_table_force_{contacts.max_force:.1f}N"
        if other_objects_moved:
            self.wrong_object_contact = True
        self.max_obj_height = max(self.max_obj_height, float(obj_pos[2]))
        height = obj_pos[2] - self.rest_height
        if pads == 3 and height > self.lift_height:
            self.grasped = True
            self.lifted = True
        # drop: previously lifted & held, now no pad contact, object not in tray and falling/fallen
        if self.lifted and pads == 0 and not in_tray and height < self.lift_height * 0.5:
            self.drop = True

    def place_success(self, final_in_tray: bool, final_gripper: float, obj_vel: np.ndarray, pads: int) -> bool:
        stable = float(np.linalg.norm(obj_vel[:3])) < 0.02
        released = pads == 0 and final_gripper < 0.5
        return bool(final_in_tray and stable and released)

    def result(
        self,
        episode_id: str,
        seed: int,
        task: str,
        n_steps: int,
        cycle_time: float,
        final_in_tray: bool,
        final_gripper: float,
        obj_vel: np.ndarray,
        n_retries: int,
        timeout: bool,
        metadata: dict | None = None,
    ) -> EpisodeResult:
        pads = self.steps[-1].pads_on_object if self.steps else 0
        place = self.place_success(final_in_tray, final_gripper, obj_vel, pads)
        success = bool(self.grasped and place and not self.collision)
        ft = FailureType.NONE
        if not success:
            if self.collision:
                ft = FailureType.COLLISION
            elif (metadata or {}).get("policy_info", {}).get("bad_target") and not self.grasped:
                ft = FailureType.BAD_TARGET
            elif timeout and not self.grasped:
                ft = FailureType.TIMEOUT
            elif not self.grasped:
                ever_touched = any(s.pads_on_object for s in self.steps)
                ft = FailureType.NO_GRASP if not ever_touched else FailureType.SLIP
            elif self.drop:
                ft = FailureType.DROP
            elif timeout:
                ft = FailureType.TIMEOUT
            elif not final_in_tray:
                ft = FailureType.PLACE_FAIL
            elif not place:
                ft = FailureType.PLACE_FAIL
            else:
                ft = FailureType.PLACE_FAIL
        return EpisodeResult(
            episode_id=episode_id,
            success=success,
            grasp_success=bool(self.grasped),
            place_success=bool(place),
            collision=bool(self.collision),
            drop=bool(self.drop),
            timeout=bool(timeout),
            failure_type=ft.value,
            n_steps=n_steps,
            cycle_time_s=float(cycle_time),
            path_length_m=float(self.path_length),
            n_retries=int(n_retries),
            seed=int(seed),
            task=task,
            object_id=self.target_object,
            metadata={
                "collision_detail": self.collision_detail,
                "max_object_height": self.max_obj_height,
                "n_ik_failures": self.n_ik_failures,
                "n_safety_rejects": self.n_safety_rejects,
                "wrong_object_contact": self.wrong_object_contact,
                **(metadata or {}),
            },
        )
