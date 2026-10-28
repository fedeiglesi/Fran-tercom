"""Versioned episode recorder on top of LeRobotDataset v3 (+ our own machine-readable metadata).

Never overwrites: every recording session creates data/datasets/<name>_vNNN.
Failures are recorded too (they are data), tagged in picking_meta/episodes.jsonl.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from picking.core.types import EpisodeResult
from picking.datasets.schema import SCHEMA_VERSION, build_features
from picking.evaluation.runner import StepCallbackData
from picking.utils.io import git_dirty, git_sha, project_root, save_json

log = logging.getLogger(__name__)


def next_dataset_root(name: str, base: Path | None = None) -> Path:
    base = base or (project_root() / "data" / "datasets")
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while (base / f"{name}_v{n:03d}").exists():
        n += 1
    return base / f"{name}_v{n:03d}"


class EpisodeRecorder:
    def __init__(
        self,
        name: str,
        fps: int,
        camera_names: list[str],
        image_hw: tuple[int, int],
        with_depth: bool = False,
        root: Path | None = None,
        metadata: dict | None = None,
        robot_type: str = "mycobot_280_m5",
        use_videos: bool = False,
    ):
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        self.root = root or next_dataset_root(name)
        self.repo_id = f"local/{self.root.name}"
        self.features = build_features(camera_names, image_hw, with_depth, use_videos)
        self.ds = LeRobotDataset.create(
            repo_id=self.repo_id,
            fps=fps,
            features=self.features,
            root=self.root,
            robot_type=robot_type,
            use_videos=use_videos,
            image_writer_threads=4,
        )
        self.camera_names = camera_names
        self.with_depth = with_depth
        self.meta_dir = self.root / "picking_meta"
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.episodes_file = self.meta_dir / "episodes.jsonl"
        self.n_frames_in_episode = 0
        self.n_episodes = 0
        info = {
            "schema_version": SCHEMA_VERSION,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "git_sha": git_sha(),
            "git_dirty": git_dirty(),
            "fps": fps,
            "features": {k: {kk: (list(vv) if isinstance(vv, tuple) else vv) for kk, vv in v.items()} for k, v in self.features.items()},
            **(metadata or {}),
        }
        save_json(info, self.meta_dir / "dataset_info.json")
        log.info("recording dataset at %s", self.root)

    # called every control step by the runner
    def on_step(self, d: StepCallbackData) -> None:
        obs = d.obs
        executed = d.safe_action.to_array() if d.safe_action is not None else np.zeros(7, np.float32)
        raw = d.policy_action.to_array() if d.policy_action is not None else np.zeros(7, np.float32)
        after = d.state_after
        frame = {
            "task": obs.language_instruction,
            "observation.state": obs.state_vector().astype(np.float32),
            "observation.joints": np.asarray(obs.joints, np.float32),
            "action": executed.astype(np.float32),
            "extra.policy_action": raw.astype(np.float32),
            "extra.target_tcp_pose": np.concatenate([after.tcp.position, after.tcp.quaternion]).astype(np.float32),
            "extra.target_joints": np.asarray(after.joints, np.float32),
            "extra.object_pose": np.concatenate([d.privileged["object_pos"], [np.cos(d.privileged["object_yaw"] / 2), 0, 0, np.sin(d.privileged["object_yaw"] / 2)]]).astype(np.float32),
            "extra.safety_rejected": np.array([0.0 if d.safe_action is not None else 1.0], np.float32),
            "extra.corrected": np.array([1.0 if d.info.get("corrected") else 0.0], np.float32),
        }
        for cam in self.camera_names:
            frame[f"observation.images.{cam}"] = obs.images[cam]
            if self.with_depth:
                frame[f"observation.depth.{cam}"] = obs.depth[cam].astype(np.float32)
        self.ds.add_frame(frame)
        self.n_frames_in_episode += 1

    def end_episode(self, result: EpisodeResult, extra: dict | None = None) -> None:
        if self.n_frames_in_episode == 0:
            return
        self.ds.save_episode()
        rec = result.to_dict()
        rec["episode_index"] = self.n_episodes
        rec["n_frames"] = self.n_frames_in_episode
        rec.update(extra or {})
        with open(self.episodes_file, "a") as f:
            f.write(json.dumps(rec, default=_json_default) + "\n")
        self.n_episodes += 1
        self.n_frames_in_episode = 0

    def discard_episode(self) -> None:
        self.ds.clear_episode_buffer()
        self.n_frames_in_episode = 0

    def finalize(self) -> dict:
        self.ds.finalize()
        summary = {"n_episodes": self.n_episodes, "root": str(self.root)}
        save_json(summary, self.meta_dir / "summary.json")
        return summary


def _json_default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def load_episode_records(root: Path) -> list[dict]:
    p = Path(root) / "picking_meta" / "episodes.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in open(p) if line.strip()]
