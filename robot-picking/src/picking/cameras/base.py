"""Camera interface: the OAK-D (real) and the MuJoCo camera (sim) expose the same Frame."""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    distortion: list[float] = field(default_factory=list)

    def K(self) -> np.ndarray:
        return np.array([[self.fx, 0, self.cx], [0, self.fy, self.cy], [0, 0, 1]], float)

    def to_dict(self) -> dict:
        return {"fx": self.fx, "fy": self.fy, "cx": self.cx, "cy": self.cy, "width": self.width, "height": self.height, "distortion": list(self.distortion)}


@dataclass
class Frame:
    rgb: np.ndarray  # HxWx3 uint8
    depth: np.ndarray | None  # HxW float32 metres (NaN/0 = invalid)
    timestamp: float
    intrinsics: Intrinsics
    seq: int = 0


class Camera(abc.ABC):
    name: str = "camera"

    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def read(self) -> Frame: ...

    @abc.abstractmethod
    def intrinsics(self) -> Intrinsics: ...

    def deproject(self, u: float, v: float, depth_m: float) -> np.ndarray:
        """Pixel + depth -> 3D point in the camera frame (OpenCV convention: x right, y down, z forward)."""
        k = self.intrinsics()
        return np.array([(u - k.cx) * depth_m / k.fx, (v - k.cy) * depth_m / k.fy, depth_m])


def now() -> float:
    return time.time()
