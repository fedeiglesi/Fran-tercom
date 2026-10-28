"""picking: robot-agnostic learning platform for robotic pick-and-place.

Layers (strictly separated, see docs/ARCHITECTURE.md):
  perception  -> picking.cameras, picking.calibration
  policy      -> picking.policies (heuristic, ACT, VLA, remote)
  safety      -> picking.safety (deterministic, independent from the policy)
  robot       -> picking.robots (RobotAdapter: sim, myCobot 280, UR10e mock)
  evaluation  -> picking.evaluation
"""
__version__ = "0.1.0"

from picking.utils.gl import select_gl_backend as _select_gl_backend  # noqa: E402

_select_gl_backend(__import__("os").environ.get("PICKING_GL", "auto"))
