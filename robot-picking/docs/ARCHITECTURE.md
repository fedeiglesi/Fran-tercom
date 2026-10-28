# Arquitectura

Plataforma de aprendizaje de picking robótico, independiente del robot concreto.
Objetivo: simulación → myCobot 280 + OAK-D → motopartes → clutter → bin picking → UR10e.

```
                 ┌───────────────┐     Observation (imágenes, TCP, joints, gripper, instrucción)
  Cámara ───────►│  PERCEPCIÓN   │────────────────────────────────┐
 (OAK-D / sim)   │ cameras/      │                                ▼
                 │ calibration/  │                         ┌─────────────┐   Action normalizada
                 └───────────────┘                         │   POLICY    │   (Δx Δy Δz ΔRx ΔRy ΔRz g)
                                                           │ policies/   │───────────┐
 RobotState ◄──────────────────────────────────────────────│ heurística  │           ▼
 (joints, TCP,                                             │ ACT / VLA   │   ┌───────────────┐
  gripper)                                                 │ remote      │   │  SAFETY LAYER │  determinista,
                                                           └─────────────┘   │  safety/      │  la policy no
                                                                             └───────┬───────┘  puede apagarla
                                                                                     │ acción segura o STOP
                                                                             ┌───────▼───────┐
                                                                             │ RobotAdapter  │  robots/
                                                                             │ Sim | myCobot │  IK propio
                                                                             │ | UR10e       │
                                                                             └───────┬───────┘
                                                                                     ▼
                                                                         MuJoCo / pymycobot / RTDE
                      ┌──────────────┐          ┌──────────────┐
                      │  EVALUADOR   │          │   DATASET    │  LeRobotDataset v3 versionado
                      │ evaluation/  │          │  datasets/   │  (éxitos y fallos)
                      └──────────────┘          └──────────────┘
```

## Capas (src/picking)

| Capa | Módulos | Responsabilidad |
|---|---|---|
| Tipos comunes | `core/types.py`, `core/transforms.py` | `Observation`, `Action`, `RobotState`, `EpisodeResult`, `FailureType`; álgebra SE(3), Kabsch |
| Percepción | `cameras/base.py`, `cameras/oakd.py`, `cameras/sim_camera.py`, `calibration/extrinsics.py` | Misma interfaz `Camera` para OAK-D y MuJoCo; calibración `T_base_camera` versionada |
| Policy | `policies/base.py`, `heuristic.py`, `act_policy.py`, `networking/client.py` | `Policy.act(obs) -> Action | None`. `None` = HOLD |
| Safety | `safety/filter.py` | Límites XYZ, cono de orientación, Δ por paso, velocidad, NaN, timeout, acciones viejas |
| Robot | `robots/base.py` (`RobotAdapter`), `sim.py`, `mycobot.py`, `ur10e.py`, `kinematics.py`, `dh.py` | Convierte objetivo cartesiano → joints con IK propio; unidades en el borde |
| Simulación | `simulation/mjcf.py`, `env.py`, `randomization.py` | Escena MuJoCo generada desde la geometría oficial del URDF |
| Evaluación | `evaluation/evaluator.py`, `metrics.py`, `runner.py`, `real_runner.py` | Éxito/fallo objetivo, métricas, loop de episodio (sim y real) |
| Dataset | `datasets/schema.py`, `recorder.py` | LeRobotDataset v3 + metadatos propios (`picking_meta/`) |
| Red | `networking/protocol.py`, `server.py`, `client.py` | Inferencia remota con versionado, timeouts, heartbeat, reconexión |
| Teleop | `teleop/keyboard.py` | Demostraciones humanas y correcciones DAgger |

## Espacio de acciones (robot-agnóstico)

`Action = [dx, dy, dz, drx, dry, drz, gripper]`, los 6 primeros en `[-1, 1]` (fracción del máximo por paso
definido en `configs/safety/*.yaml`), `gripper` en `[0, 1]` (0 abierto, 1 cerrado). Los deltas se expresan
en el frame de la base del robot y se aplican al TCP (centro de agarre). Los joints reales se guardan en el
dataset sólo como información auxiliar (`observation.joints`, `extra.target_joints`).

Estado proprioceptivo para policies aprendidas: `observation.state` = TCP xyz (3) + rotación 6D (6) + gripper (1).
Se usa rotación 6D porque el cuaternión cambia de signo cerca de 180° (orientación de agarre vertical).

## Loop por paso (idéntico en sim y real)

1. `Camera.read()` + `RobotAdapter.get_state()` → `Observation`
2. `Policy.act(obs)` → `Action` (o `None`)
3. `SafetyFilter.filter(action, state, dt)` → acción recortada o `None`
4. `None` → `adapter.stop()`; si no, `adapter.apply_action(safe, scale)` (IK → joints → hardware)
5. Evaluador / grabador reciben estado antes/después, contactos, acción cruda y acción ejecutada.

## Independencia del robot (demostrado por tests)

* `tests/test_adapters.py::test_ur10e_mock_follows_same_policy_api`: la misma `Action` mueve el mock UR10e.
* `tests/test_adapters.py::test_mycobot_adapter_unit_conversion_and_arming`: el adapter myCobot convierte
  grados↔radianes y no se mueve si no está armado.
* La policy nunca importa `pymycobot`, `mujoco` ni `rtde`.

## Inferencia local o remota

`RemotePolicy` implementa la misma interfaz `Policy`; `PolicyServer` aloja cualquier policy. Protocolo en
`networking/protocol.py` (versión `PROTOCOL_VERSION`). Timeout, acción vieja o desconexión ⇒ `None` ⇒ STOP/HOLD.
