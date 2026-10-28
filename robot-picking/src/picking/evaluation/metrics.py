from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from picking.core.types import EpisodeResult
from picking.utils.io import save_json


def aggregate(results: list[EpisodeResult]) -> dict:
    if not results:
        return {"n_episodes": 0}
    n = len(results)
    succ = [r for r in results if r.success]
    ct = np.array([r.cycle_time_s for r in succ]) if succ else np.array([np.nan])
    d = {
        "n_episodes": n,
        "success_rate": sum(r.success for r in results) / n,
        "grasp_success_rate": sum(r.grasp_success for r in results) / n,
        "place_success_rate": sum(r.place_success for r in results) / n,
        "collision_rate": sum(r.collision for r in results) / n,
        "drop_rate": sum(r.drop for r in results) / n,
        "timeout_rate": sum(r.timeout for r in results) / n,
        "mean_cycle_time_s": float(np.nanmean(ct)),
        "median_cycle_time_s": float(np.nanmedian(ct)),
        "p95_cycle_time_s": float(np.nanpercentile(ct, 95)) if succ else float("nan"),
        "mean_path_length_m": float(np.mean([r.path_length_m for r in results])),
        "mean_retries": float(np.mean([r.n_retries for r in results])),
        "total_retries": int(sum(r.n_retries for r in results)),
        "failure_types": dict(Counter(r.failure_type for r in results)),
        "ik_failures_total": int(sum(r.metadata.get("n_ik_failures", 0) for r in results)),
        "safety_rejects_total": int(sum(r.metadata.get("n_safety_rejects", 0) for r in results)),
    }
    per_obj = {}
    for oid in sorted({r.object_id for r in results}):
        rs = [r for r in results if r.object_id == oid]
        per_obj[oid] = {"n": len(rs), "success_rate": sum(r.success for r in rs) / len(rs)}
    d["per_object"] = per_obj
    lat = [r.metadata.get("mean_policy_latency_ms") for r in results if r.metadata.get("mean_policy_latency_ms") is not None]
    if lat:
        d["mean_policy_latency_ms"] = float(np.mean(lat))
    return d


def gate_check(summary: dict, gate: dict) -> dict:
    """Compare against acceptance gate thresholds, e.g. {'success_rate': ('>=', 0.95), ...}."""
    out = {}
    for k, (op, thr) in gate.items():
        v = summary.get(k)
        ok = (v >= thr) if op == ">=" else (v <= thr)
        out[k] = {"value": v, "op": op, "threshold": thr, "pass": bool(ok)}
    out["all_pass"] = all(v["pass"] for k, v in out.items() if k != "all_pass")
    return out


def save_results(results: list[EpisodeResult], out_dir: Path, summary_extra: dict | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [r.to_dict() for r in results]
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "metadata"} | {"metadata": str(r["metadata"])} for r in rows])
    df.to_csv(out_dir / "episodes.csv", index=False)
    try:
        df.to_parquet(out_dir / "episodes.parquet", index=False)
    except Exception:
        pass
    save_json(rows, out_dir / "episodes.json")
    summary = aggregate(results)
    summary.update(summary_extra or {})
    save_json(summary, out_dir / "summary.json")
    return summary
