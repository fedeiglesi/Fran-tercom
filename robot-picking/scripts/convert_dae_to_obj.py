"""Convert the official Elephant Robotics COLLADA (.dae) meshes to .obj for MuJoCo.

The official meshes (mycobot_ros/mycobot_description/urdf/mycobot_280_m5/*.dae) are Rhino exports
in millimetres with a single <triangles> block per file.  MuJoCo loads OBJ/STL, not DAE, so we
convert and scale to metres. Only geometry is kept (no materials).

Usage:
    uv run python scripts/convert_dae_to_obj.py <dae_dir> <out_dir>
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}


def convert(dae_path: Path, obj_path: Path, decimate_to: int | None = None) -> tuple[int, int]:
    tree = ET.parse(dae_path)
    root = tree.getroot()
    unit = root.find("c:asset/c:unit", NS)
    scale = float(unit.get("meter", "1.0")) if unit is not None else 1.0

    all_v: list[np.ndarray] = []
    all_f: list[np.ndarray] = []
    offset = 0
    for geom in root.findall(".//c:library_geometries/c:geometry", NS):
        mesh = geom.find("c:mesh", NS)
        if mesh is None:
            continue
        sources = {}
        for src in mesh.findall("c:source", NS):
            fa = src.find("c:float_array", NS)
            if fa is None:
                continue
            acc = src.find("c:technique_common/c:accessor", NS)
            stride = int(acc.get("stride", "3")) if acc is not None else 3
            arr = np.fromstring(fa.text, sep=" ", dtype=np.float64).reshape(-1, stride)
            sources["#" + src.get("id")] = arr
        verts = mesh.find("c:vertices", NS)
        vid = "#" + verts.get("id")
        pos_src = None
        for inp in verts.findall("c:input", NS):
            if inp.get("semantic") == "POSITION":
                pos_src = inp.get("source")
        positions = sources[pos_src]
        for tri in mesh.findall("c:triangles", NS) + mesh.findall("c:polylist", NS):
            inputs = tri.findall("c:input", NS)
            n_in = max(int(i.get("offset")) for i in inputs) + 1
            v_off = [int(i.get("offset")) for i in inputs if i.get("semantic") == "VERTEX" and i.get("source") == vid][0]
            p = np.fromstring(tri.find("c:p", NS).text, sep=" ", dtype=np.int64)
            if tri.tag.endswith("polylist"):
                vcount = np.fromstring(tri.find("c:vcount", NS).text, sep=" ", dtype=np.int64)
                assert np.all(vcount == 3), "only triangulated polylists supported"
            idx = p.reshape(-1, n_in)[:, v_off].reshape(-1, 3)
            all_v.append(positions * scale)
            all_f.append(idx + offset)
            offset += len(positions)
    V = np.concatenate(all_v)
    F = np.concatenate(all_f)
    with open(obj_path, "w") as f:
        f.write(f"# converted from {dae_path.name}\n")
        for v in V:
            f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
        for t in F:
            f.write(f"f {t[0]+1} {t[1]+1} {t[2]+1}\n")
    return len(V), len(F)


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    names = sys.argv[3:] or [p.stem for p in src.glob("*.dae")]
    for n in names:
        nv, nf = convert(src / f"{n}.dae", dst / f"{n}.obj")
        print(f"{n}: {nv} vertices, {nf} faces")


if __name__ == "__main__":
    main()
