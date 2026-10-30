"""SKU identification for order fulfilment.

`SkuIdentifierOracle` labels every detected blob with the SKU of the nearest simulated object and corrupts the
label with probability `error_rate` (swaps it for another present SKU). It is an explicit PLACEHOLDER for the
learned detector that will be trained on photos of the real parts; it lets the whole order-fulfilment logic
(selection among distractors, wrong-SKU failures, retries, placement) be benchmarked at scale now.
"""
from __future__ import annotations

import numpy as np

from picking.perception.segmentation import ObjectDetection


class SkuIdentifierOracle:
    """Stand-in for a learned INSTANCE detector: returns one detection per object in the source bin with a
    position error (gaussian, `pos_noise_m`, default 2 mm = measured accuracy of our geometric detector) and a
    SKU label corrupted with probability `error_rate`. Occluded or merged objects are not modelled."""

    name = "sku_oracle"

    def __init__(self, error_rate: float = 0.0, seed: int = 0, max_match_m: float = 0.03, pos_noise_m: float = 0.002):
        self.error_rate = float(error_rate)
        self.rng = np.random.default_rng(seed)
        self.max_match_m = max_match_m
        self.pos_noise_m = pos_noise_m
        self.n_calls = 0
        self.n_corrupted = 0

    def detect(self, true_objects: dict[str, dict], skus: dict[str, str]) -> list[tuple[ObjectDetection, str]]:
        """true_objects: object_id -> {pos, yaw, yaw_period, width, height} (privileged scene state)."""
        out = []
        present = list(true_objects)
        for oid, o in true_objects.items():
            self.n_calls += 1
            p = np.asarray(o["pos"], float) + self.rng.normal(0, self.pos_noise_m, 3) * np.array([1, 1, 0.5])
            top = p.copy()
            top[2] = p[2] + o.get("height", 0.02) / 2
            det = ObjectDetection(oid, np.zeros(2), p, top, float(o.get("yaw", 0.0)), float(o.get("height", 0.02)), 100, 1.0, float(o.get("yaw_period", np.pi / 2)), o.get("width"))
            label = skus[oid]
            if self.error_rate > 0 and self.rng.random() < self.error_rate and len(present) > 1:
                others = [skus[x] for x in present if skus[x] != label]
                if others:
                    label = str(self.rng.choice(others))
                    self.n_corrupted += 1
            out.append((det, label))
        return out

    def label(self, detections: list[ObjectDetection], true_positions: dict[str, np.ndarray], skus: dict[str, str]) -> list[tuple[ObjectDetection, str | None]]:
        """true_positions: object_id -> xyz (privileged, used only to emulate recognition); skus: object_id -> sku."""
        out = []
        present = list(true_positions)
        for d in detections:
            self.n_calls += 1
            best, best_d = None, self.max_match_m
            for oid, p in true_positions.items():
                dist = float(np.linalg.norm(np.asarray(p)[:2] - d.position_base[:2]))
                if dist < best_d:
                    best, best_d = oid, dist
            label = None if best is None else skus[best]
            if label is not None and self.error_rate > 0 and self.rng.random() < self.error_rate and len(present) > 1:
                others = [skus[o] for o in present if skus[o] != label]
                if others:
                    label = str(self.rng.choice(others))
                    self.n_corrupted += 1
            out.append((d, label))
        return out
