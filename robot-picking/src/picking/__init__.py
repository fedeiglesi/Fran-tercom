"""picking: robot-agnostic learning platform for robotic pick-and-place.

Layers (strictly separated, see docs/ARCHITECTURE.md):
  perception  -> picking.cameras, picking.calibration
  policy      -> picking.policies (heuristic, ACT, VLA, remote)
  safety      -> picking.safety (deterministic, independent from the policy)
  robot       -> picking.robots (RobotAdapter: sim, myCobot 280, UR10e mock)
  evaluation  -> picking.evaluation
"""
__version__ = "0.1.0"
