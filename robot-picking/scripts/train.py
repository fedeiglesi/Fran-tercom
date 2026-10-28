"""Train an imitation-learning policy (ACT) on a recorded LeRobot dataset. CPU-friendly.

    uv run python scripts/train.py --config policies/act_level1.yaml --out outputs/act_level1_v001
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch

from picking.core.types import ACTION_DIM, STATE_DIM
from picking.policies.act_policy import _stats_to_tensors, build_act_policy
from picking.utils.io import config_path, deep_update, git_dirty, git_sha, load_yaml, project_root, save_json
from picking.utils.logging import setup_logging


class RandomShift:
    """Small random crop-shift (DrQ-style) + brightness jitter on a (B,3,H,W) float batch in [0,1]."""

    def __init__(self, pad: int = 4, brightness: float = 0.1):
        self.pad = pad
        self.brightness = brightness

    def __call__(self, x: torch.Tensor) -> torch.Tensor:
        b, c, h, w = x.shape
        if self.pad > 0:
            xp = torch.nn.functional.pad(x, (self.pad,) * 4, mode="replicate")
            out = torch.empty_like(x)
            for i in range(b):
                dx, dy = random.randint(0, 2 * self.pad), random.randint(0, 2 * self.pad)
                out[i] = xp[i, :, dy : dy + h, dx : dx + w]
        else:
            out = x.clone()
        if self.brightness > 0:
            f = 1 + (torch.rand(b, 1, 1, 1) * 2 - 1) * self.brightness
            out = (out * f).clamp(0, 1)
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="policies/act_level1.yaml")
    ap.add_argument("--override", default=None)
    ap.add_argument("--dataset", default=None, help="dataset dir name under data/datasets (overrides config)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--resume", default=None)
    args = ap.parse_args()
    setup_logging()
    log = logging.getLogger("train")
    cfg = load_yaml(config_path(args.config))
    if args.override:
        cfg = deep_update(cfg, json.loads(args.override))
    if args.dataset:
        cfg["dataset"] = args.dataset
    if args.steps:
        cfg["training"]["steps"] = args.steps
    tr = cfg["training"]
    seed = int(tr.get("seed", 1))
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    device = tr.get("device", "cpu")

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    root = project_root() / "data" / "datasets" / cfg["dataset"]
    chunk = int(cfg["act"]["chunk_size"])
    probe = LeRobotDataset(f"local/{root.name}", root=root)
    fps = probe.fps
    ds = LeRobotDataset(f"local/{root.name}", root=root, delta_timestamps={cfg["action_key"]: [i / fps for i in range(chunk)]})
    feat = ds.meta.features
    if cfg.get("image_key") and cfg["image_key"] not in feat:
        cfg["image_key"] = None  # low-dim dataset (no images)
    if "observation.environment_state" in feat:
        cfg["env_state_key"] = "observation.environment_state"
        cfg["env_state_dim"] = int(feat["observation.environment_state"]["shape"][0])
    image_hw = tuple(feat[cfg["image_key"]]["shape"][:2]) if cfg.get("image_key") else (0, 0)
    train_hw = tuple(tr["image_size"]) if tr.get("image_size") and cfg.get("image_key") else image_hw
    image_hw = train_hw
    state_dim = int(feat[cfg["state_key"]]["shape"][0])
    action_dim = int(feat[cfg["action_key"]]["shape"][0])
    assert state_dim == STATE_DIM and action_dim == ACTION_DIM, (state_dim, action_dim)
    stats_json = {k: {kk: np.asarray(vv).tolist() for kk, vv in v.items()} for k, v in ds.meta.stats.items() if k in (cfg.get("image_key"), cfg["state_key"], cfg["action_key"], cfg.get("env_state_key"))}
    stats = _stats_to_tensors(stats_json)
    policy, pre, post, act_cfg = build_act_policy(cfg, stats, image_hw, state_dim, action_dim, device)
    policy.to(device).train()
    n_params = sum(p.numel() for p in policy.parameters())
    log.info("dataset %s: %d frames, %d episodes; image %s; params %.2fM", root.name, len(ds), ds.meta.total_episodes, image_hw, n_params / 1e6)

    out = Path(args.out) if args.out else project_root() / "outputs" / f"act_{root.name}_{time.strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    backbone_params = [p for n, p in policy.named_parameters() if n.startswith("model.backbone") and p.requires_grad]
    other_params = [p for n, p in policy.named_parameters() if not n.startswith("model.backbone") and p.requires_grad]
    optim = torch.optim.AdamW(
        [{"params": other_params, "lr": float(tr["lr"])}, {"params": backbone_params, "lr": float(tr["lr_backbone"])}],
        weight_decay=float(tr["weight_decay"]),
    )
    steps = int(tr["steps"])
    sched = torch.optim.lr_scheduler.LambdaLR(optim, lambda s: 0.5 * (1 + math.cos(math.pi * min(s, steps) / steps)) * 0.9 + 0.1)
    start_step = 0
    if args.resume:
        ck = torch.load(Path(args.resume) / "model.pt", map_location=device)
        policy.load_state_dict(ck)
        st = json.load(open(Path(args.resume) / "picking_policy.json"))
        start_step = st.get("step", 0)
    loader = torch.utils.data.DataLoader(ds, batch_size=int(tr["batch_size"]), shuffle=True, num_workers=int(tr.get("num_workers", 0)), drop_last=True, persistent_workers=int(tr.get("num_workers", 0)) > 0)
    # NOTE: with a FIXED camera, a random pixel shift destroys the mapping pixel -> world position (a 4 px
    # shift at 128 px ~ 15 mm, the whole object-position range at level 1). Keep shift at 0 for fixed cameras.
    aug = RandomShift(pad=int(tr.get("image_shift_pad", 0)), brightness=float(tr.get("image_brightness", 0.1))) if tr.get("image_augmentation", True) else None
    meta = {
        "version": out.name,
        "config": cfg,
        "image_hw": list(image_hw),
        "state_dim": state_dim,
        "action_dim": action_dim,
        "dataset": root.name,
        "dataset_episodes": ds.meta.total_episodes,
        "dataset_frames": len(ds),
        "git_sha": git_sha(),
        "git_dirty": git_dirty(),
        "seed": seed,
        "n_params": n_params,
        "torch": torch.__version__,
    }
    save_json(stats_json, out / "dataset_stats.json")
    history = []
    t0 = time.time()
    step = start_step
    it = iter(loader)
    run_loss = []
    while step < steps:
        try:
            batch = next(it)
        except StopIteration:
            it = iter(loader)
            batch = next(it)
        if cfg.get("image_key"):
            if tuple(batch[cfg["image_key"]].shape[-2:]) != tuple(train_hw):
                batch[cfg["image_key"]] = torch.nn.functional.interpolate(batch[cfg["image_key"]], size=train_hw, mode="area")
            if aug is not None:
                batch[cfg["image_key"]] = aug(batch[cfg["image_key"]])
        batch = pre(batch)
        loss, info = policy.forward(batch)
        optim.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(policy.parameters(), float(tr["grad_clip_norm"]))
        optim.step()
        sched.step()
        step += 1
        run_loss.append(loss.item())
        if step % int(tr["log_every"]) == 0 or step == steps:
            el = time.time() - t0
            rec = {"step": step, "loss": float(np.mean(run_loss)), "l1": info.get("l1_loss"), "kld": info.get("kld_loss"), "lr": sched.get_last_lr()[0], "elapsed_s": el, "steps_per_s": (step - start_step) / el}
            history.append(rec)
            log.info("step %d/%d loss %.4f l1 %.4f kld %.3f (%.2f it/s)", step, steps, rec["loss"], rec["l1"] or 0, rec["kld"] or 0, rec["steps_per_s"])
            run_loss = []
        if step % int(tr["save_every"]) == 0 or step == steps:
            torch.save(policy.state_dict(), out / "model.pt")
            save_json({**meta, "step": step, "history": history, "train_time_s": time.time() - t0}, out / "picking_policy.json")
    log.info("done: %s", out)
    print(json.dumps({"out": str(out), "final_loss": history[-1]["loss"] if history else None, "train_time_s": time.time() - t0}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
