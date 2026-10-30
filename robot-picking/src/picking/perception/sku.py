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
    name = "sku_oracle"

    def __init__(self, error_rate: float = 0.0, seed: int = 0, max_match_m: float = 0.03):
        self.error_rate = float(error_rate)
        self.rng = np.random.default_rng(seed)
        self.max_match_m = max_match_m
        self.n_calls = 0
        self.n_corrupted = 0

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
