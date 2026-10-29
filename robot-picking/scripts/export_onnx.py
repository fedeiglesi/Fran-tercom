"""Export a trained low-dim ACT policy to ONNX (lightweight on-robot inference, as the Open Duck does).

    uv run python scripts/export_onnx.py --checkpoint outputs/act_lowdim_smooth_floor_v001
Writes <checkpoint>/policy.onnx + <checkpoint>/onnx_io.json (input/output names, normalization stats, chunk size)
and verifies the ONNX output against PyTorch on random inputs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

from picking.policies.act_policy import ACTPolicyWrapper
from picking.utils.io import save_json


class ChunkModel(torch.nn.Module):
    """Normalized (state, env_state) -> normalized action chunk (1, chunk, 7)."""

    def __init__(self, act, state_key, env_key):
        super().__init__()
        self.act = act
        self.state_key, self.env_key = state_key, env_key

    def forward(self, state, env_state):
        batch = {self.state_key: state, "observation.environment_state": env_state}
        return self.act(batch)[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    args = ap.parse_args()
    ck = Path(args.checkpoint)
    w = ACTPolicyWrapper(ck)
    assert w.env_state_key and not w.image_key, "export supports the low-dim (state + environment_state) ACT"
    model = ChunkModel(w.policy.model, w.state_key, w.env_state_key).eval()
    stats = json.load(open(ck / "dataset_stats.json"))
    state = torch.zeros(1, w.policy.config.input_features[w.state_key].shape[0])
    env = torch.zeros(1, w.policy.config.input_features[w.env_state_key].shape[0])
    out = ck / "policy.onnx"
    torch.onnx.export(model, (state, env), str(out), input_names=["state_norm", "env_state_norm"], output_names=["action_chunk_norm"], opset_version=17, dynamo=False)
    io = {
        "inputs": {"state_norm": "(observation.state - mean) / std", "env_state_norm": "(observation.environment_state - mean) / std"},
        "output": "action_chunk_norm (1, chunk_size, 7): action = out * std + mean; execute n_action_steps then re-plan",
        "chunk_size": w.policy.config.chunk_size,
        "n_action_steps": w.cfg["act"]["n_action_steps"],
        "stats": {k: {kk: vv for kk, vv in v.items() if kk in ("mean", "std")} for k, v in stats.items()},
    }
    save_json(io, ck / "onnx_io.json")
    # verify
    import onnxruntime as ort

    sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(0)
    max_err = 0.0
    for _ in range(5):
        s = rng.normal(size=(1, state.shape[1])).astype(np.float32)
        e = rng.normal(size=(1, env.shape[1])).astype(np.float32)
        with torch.no_grad():
            ref = model(torch.from_numpy(s), torch.from_numpy(e)).numpy()
        got = sess.run(None, {"state_norm": s, "env_state_norm": e})[0]
        max_err = max(max_err, float(np.abs(ref - got).max()))
    print(f"exported {out} ({out.stat().st_size/1e6:.1f} MB); max |onnx - torch| = {max_err:.2e}")
    return 0 if max_err < 1e-4 else 1


if __name__ == "__main__":
    sys.exit(main())
