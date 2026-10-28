"""ACT (Action Chunking Transformer) via LeRobot 0.6.x, wrapped behind the robot-agnostic Policy API."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
import torch

from picking.core.types import Action, Observation
from picking.policies.base import Policy

log = logging.getLogger(__name__)


def build_act_policy(cfg: dict, dataset_stats: dict, image_hw: tuple[int, int], state_dim: int, action_dim: int, device: str):
    from lerobot.configs.types import FeatureType, NormalizationMode, PolicyFeature
    from lerobot.policies.act.configuration_act import ACTConfig
    from lerobot.policies.act.modeling_act import ACTPolicy
    from lerobot.policies.act.processor_act import make_act_pre_post_processors

    a = cfg["act"]
    input_features = {
        cfg["state_key"]: PolicyFeature(type=FeatureType.STATE, shape=(state_dim,)),
        cfg["image_key"]: PolicyFeature(type=FeatureType.VISUAL, shape=(3, image_hw[0], image_hw[1])),
    }
    output_features = {cfg["action_key"]: PolicyFeature(type=FeatureType.ACTION, shape=(action_dim,))}
    act_cfg = ACTConfig(
        input_features=input_features,
        output_features=output_features,
        chunk_size=int(a["chunk_size"]),
        n_action_steps=int(a["n_action_steps"]),
        vision_backbone=a.get("vision_backbone", "resnet18"),
        pretrained_backbone_weights=a.get("pretrained_backbone_weights"),
        dim_model=int(a.get("dim_model", 512)),
        n_heads=int(a.get("n_heads", 8)),
        dim_feedforward=int(a.get("dim_feedforward", 3200)),
        n_encoder_layers=int(a.get("n_encoder_layers", 4)),
        n_decoder_layers=int(a.get("n_decoder_layers", 1)),
        use_vae=bool(a.get("use_vae", True)),
        latent_dim=int(a.get("latent_dim", 32)),
        kl_weight=float(a.get("kl_weight", 10.0)),
        dropout=float(a.get("dropout", 0.1)),
        temporal_ensemble_coeff=a.get("temporal_ensemble_coeff"),
        normalization_mapping={"VISUAL": NormalizationMode.MEAN_STD, "STATE": NormalizationMode.MEAN_STD, "ACTION": NormalizationMode.MEAN_STD},
        device=device,
    )
    policy = ACTPolicy(act_cfg)
    pre, post = make_act_pre_post_processors(act_cfg, dataset_stats)
    return policy, pre, post, act_cfg


def _stats_to_tensors(stats: dict) -> dict:
    out = {}
    for k, v in stats.items():
        out[k] = {kk: torch.as_tensor(np.asarray(vv), dtype=torch.float32) for kk, vv in v.items() if kk in ("mean", "std", "min", "max")}
    return out


class ACTPolicyWrapper(Policy):
    name = "act"
    needs_images = True

    def __init__(self, checkpoint_dir: str | Path, device: str = "cpu", act_override: dict | None = None):
        """act_override: inference-time ACT config changes (e.g. {"n_action_steps": 5} or
        {"temporal_ensemble_coeff": 0.01, "n_action_steps": 1}); weights are unchanged."""
        self.ckpt = Path(checkpoint_dir)
        meta = json.load(open(self.ckpt / "picking_policy.json"))
        self.version = meta.get("version", self.ckpt.name)
        self.cfg = json.loads(json.dumps(meta["config"]))
        if act_override:
            self.cfg["act"].update(act_override)
            self.version += "+" + ",".join(f"{k}={v}" for k, v in act_override.items())
        self.image_key = self.cfg["image_key"]
        self.state_key = self.cfg["state_key"]
        self.camera_name = self.image_key.split(".")[-1]
        self.image_hw = tuple(meta["image_hw"])
        stats = _stats_to_tensors(json.load(open(self.ckpt / "dataset_stats.json")))
        self.policy, self.pre, self.post, self.act_cfg = build_act_policy(self.cfg, stats, self.image_hw, meta["state_dim"], meta["action_dim"], device)
        sd = torch.load(self.ckpt / "model.pt", map_location=device)
        self.policy.load_state_dict(sd)
        self.policy.to(device).eval()
        self.device = device
        self.latencies: list[float] = []
        torch.set_num_threads(max(1, torch.get_num_threads()))

    def reset(self, instruction: str = "", task_info: dict | None = None) -> None:
        self.policy.reset()
        self.instruction = instruction

    def act(self, obs: Observation) -> Action:
        t0 = time.perf_counter()
        img = obs.images[self.camera_name]
        if img.shape[:2] != self.image_hw:
            import cv2

            img = cv2.resize(img, (self.image_hw[1], self.image_hw[0]), interpolation=cv2.INTER_AREA)
        img_t = torch.from_numpy(np.ascontiguousarray(img)).permute(2, 0, 1).float() / 255.0
        batch = {
            self.state_key: torch.from_numpy(obs.state_vector()).float(),
            self.image_key: img_t,
            "task": self.instruction,
        }
        batch = self.pre(batch)
        with torch.no_grad():
            a = self.policy.select_action(batch)
        a = self.post(a)
        a = a.squeeze(0).cpu().numpy().astype(np.float64)
        self.latencies.append((time.perf_counter() - t0) * 1000)
        a[:6] = np.clip(a[:6], -1, 1)
        a[6] = float(np.clip(a[6], 0, 1))
        return Action.from_array(a)

    def info(self) -> dict:
        return {"mean_latency_ms": float(np.mean(self.latencies)) if self.latencies else 0.0}
