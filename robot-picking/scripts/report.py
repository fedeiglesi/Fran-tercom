"""Build the observability report: reports/RESULTS.md + plots from every evaluation under reports/eval.

    uv run python scripts/report.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from picking.utils.io import project_root


def main() -> int:
    root = project_root()
    evals = sorted((root / "reports" / "eval").glob("*/summary.json"))
    rows = []
    for p in evals:
        s = json.load(open(p))
        if s.get("n_episodes", 0) == 0:
            continue
        rows.append({"run": p.parent.name, "policy": s.get("policy"), "ckpt": s.get("checkpoint"), "n": s["n_episodes"], "succ": s["success_rate"], "grasp": s["grasp_success_rate"], "coll": s["collision_rate"], "drop": s["drop_rate"], "timeout": s["timeout_rate"], "cycle": s.get("median_cycle_time_s"), "p95": s.get("p95_cycle_time_s"), "lat": s.get("mean_policy_latency_ms"), "fail": s.get("failure_types"), "gate": (s.get("gate") or {}).get("all_pass"), "sha": s.get("git_sha"), "seed0": s.get("seed_start")})
    out_dir = root / "reports"
    out_dir.mkdir(exist_ok=True)
    ckpts = sorted((root / "outputs").glob("*/picking_policy.json"))
    lines = ["# RESULTS (generado por scripts/report.py)", ""]
    best = max(rows, key=lambda r: (r["succ"], -r["coll"], r["n"])) if rows else None
    if best:
        lines += [f"**Mejor evaluación:** `{best['run']}` — policy `{best['policy']}` success {best['succ']:.3f} (n={best['n']}), colisiones {best['coll']:.3f}, drops {best['drop']:.3f}", ""]
    if ckpts:
        last = json.load(open(ckpts[-1]))
        lines += [f"**Último checkpoint:** `{ckpts[-1].parent.name}` — dataset `{last.get('dataset')}` ({last.get('dataset_episodes')} ep, {last.get('dataset_frames')} frames), step {last.get('step')}, loss final {(last.get('history') or [{}])[-1].get('loss')}", ""]
    lines += ["| run | policy | n | success | grasp | collision | drop | timeout | ciclo med [s] | p95 [s] | latencia [ms] | gate | fallos | git |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['run']} | {r['policy']}{' ('+Path(r['ckpt']).name+')' if r['ckpt'] else ''} | {r['n']} | {r['succ']:.3f} | {r['grasp']:.3f} | {r['coll']:.3f} | {r['drop']:.3f} | {r['timeout']:.3f} | {r['cycle']:.2f} | {r['p95']:.2f} | {r['lat'] if r['lat'] is None else round(r['lat'],1)} | {r['gate']} | {r['fail']} | {r['sha']} |")
    (out_dir / "RESULTS.md").write_text("\n".join(lines) + "\n")
    if rows:
        fig, ax = plt.subplots(figsize=(10, 4))
        xs = range(len(rows))
        ax.bar(xs, [r["succ"] for r in rows], color="#4c72b0", label="success")
        ax.bar(xs, [r["coll"] for r in rows], color="#c44e52", label="collision", alpha=0.8)
        ax.set_xticks(list(xs))
        ax.set_xticklabels([r["run"][:22] for r in rows], rotation=45, ha="right", fontsize=7)
        ax.set_ylim(0, 1.05)
        ax.legend()
        ax.set_title("Evaluaciones en simulación (holdout)")
        fig.tight_layout()
        fig.savefig(out_dir / "success_by_run.png", dpi=120)
    for c in ckpts:
        d = json.load(open(c))
        h = d.get("history") or []
        if h:
            fig, ax = plt.subplots(figsize=(6, 3))
            ax.plot([x["step"] for x in h], [x["loss"] for x in h])
            ax.set_yscale("log")
            ax.set_xlabel("step")
            ax.set_ylabel("loss")
            ax.set_title(c.parent.name)
            fig.tight_layout()
            fig.savefig(c.parent / "loss.png", dpi=110)
            plt.close(fig)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
