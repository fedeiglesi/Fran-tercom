from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import yaml


class NumpyEncoder(json.JSONEncoder):
    def default(self, o):  # noqa: D401
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, Path):
            return str(o)
        return super().default(o)


def save_json(obj: Any, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, cls=NumpyEncoder)


def load_json(path: str | Path) -> Any:
    with open(path) as f:
        return json.load(f)


def load_yaml(path: str | Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def save_yaml(obj: Any, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(obj, f, sort_keys=False)


def deep_update(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_update(out[k], v)
        else:
            out[k] = v
    return out


def git_sha(short: bool = True) -> str:
    try:
        cmd = ["git", "rev-parse", "--short" if short else "HEAD", "HEAD"] if short else ["git", "rev-parse", "HEAD"]
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, cwd=project_root()).decode().strip()
    except Exception:
        return "unknown"


def git_dirty() -> bool:
    try:
        out = subprocess.check_output(["git", "status", "--porcelain", "."], cwd=project_root(), stderr=subprocess.DEVNULL)
        return len(out.strip()) > 0
    except Exception:
        return False


def project_root() -> Path:
    env = os.environ.get("PICKING_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3]


def config_path(*parts: str) -> Path:
    return project_root() / "configs" / Path(*parts)


def next_versioned_dir(base: Path, prefix: str) -> Path:
    """Never overwrite: returns base/prefix_vNNN with the first unused NNN."""
    base.mkdir(parents=True, exist_ok=True)
    n = 1
    while (base / f"{prefix}_v{n:03d}").exists():
        n += 1
    p = base / f"{prefix}_v{n:03d}"
    p.mkdir(parents=True)
    return p
