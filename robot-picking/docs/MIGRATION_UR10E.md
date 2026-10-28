# Migración a UR10e

## Qué NO cambia

* Policy API (`Observation` → `Action`), datasets (misma `schema.py`), safety (`SafetyFilter`, sólo otro YAML
  de límites), evaluador, networking, calibración (mismo procedimiento con la cámara fija).

## Qué cambia

1. `RobotAdapter`: `MyCobotAdapter` → `UR10eAdapter` (`src/picking/robots/ur10e.py`). Hoy existe como mock
   cinemático con DH oficial del UR10e (`robots/dh.py: UR10E_DH`), testeado con la misma `Action`
   (`tests/test_adapters.py::test_ur10e_mock_follows_same_policy_api`). Con hardware: `mock=False` usa
   `ur_rtde` (`moveJ`/`servoJ`, `stopJ`) — pendiente de validación física.
2. `configs/safety/real_ur10e.yaml` (crear): workspace y velocidades del UR (mucho mayores; empezar con
   `max_tcp_speed` 0.05 m/s igual que el myCobot).
3. Gripper industrial (p.ej. Robotiq 2F-85): implementar `set_gripper` en el adapter (URCap / Modbus).
4. `tcp_offset` y `home` del UR10e.
5. Simulación: añadir modelo MuJoCo del UR10e (MuJoCo Menagerie `universal_robots_ur10e`) como segunda
   opción en `simulation/mjcf.py` para regenerar datasets sim con el nuevo robot si hace falta.

## Datasets

Las acciones son deltas de TCP normalizados: un dataset grabado con el myCobot sigue siendo válido como
pretraining para el UR10e (misma semántica, distinta escala física vía `configs/safety`). Los joints se
guardan sólo como auxiliares. Se recomienda fine-tuning con demos reales del UR10e (docs/TRAINING.md).

## ROS 2

No es necesario para el prototipo. Si la celda industrial lo exige, envolver `UR10eAdapter` en un nodo
ROS 2 que exponga el mismo contrato; la policy y el safety no cambian.
