"""MuJoCo GL backend selection. Must run before `import mujoco` anywhere (done in picking/__init__.py)."""
from __future__ import annotations

import os
import platform


def select_gl_backend(preferred: str = "auto") -> str:
    if preferred != "auto":
        os.environ["MUJOCO_GL"] = preferred
        return preferred
    if "MUJOCO_GL" in os.environ:
        return os.environ["MUJOCO_GL"]
    if platform.system() == "Linux" and not os.environ.get("DISPLAY"):
        os.environ["MUJOCO_GL"] = "osmesa"
        os.environ.setdefault("PYOPENGL_PLATFORM", "osmesa")
        return "osmesa"
    return "default"
