# CLAUDE.md — contexto para cualquier sesión de Claude Code en esta carpeta

Este archivo lo lee automáticamente Claude Code al abrir `robot-picking/`. Resume el estado del proyecto
construido en una sesión en la nube (octubre 2026) para que una sesión local en el Mac Studio continúe sin
perder contexto. Fuentes de verdad: `EXPERIMENTS.md` (34 experimentos), `RESULTS.md`, `docs/`.

## Qué es
Plataforma de picking robótico con aprendizaje para **Beto**, el myCobot 280 M5 del laboratorio (sim → Beto + OAK-D → motopartes → UR10e).
Arquitectura: percepción → policy → safety determinista → `RobotAdapter` → hardware. Ver `docs/ARCHITECTURE.md`.
Acciones robot-agnósticas: `[dx, dy, dz, drx, dry, drz, gripper]` normalizadas (TCP, frame base).

## Comandos
```
./setup.sh                      # instala uv + deps + corre tests (49 tests)
./run_simulation.sh 100         # Baseline 0 scripted en sim
./record_demonstrations.sh 200  # dataset LeRobot v3 versionado
./train_policy.sh <dataset> 6000
./evaluate_policy.sh outputs/<ckpt> 100
./run_robot.sh readonly         # Test 1 real: lee estado, NO mueve
uv run python scripts/evaluate_sim.py --policy perception --episodes 1000 --gate target   # policy de percepción
uv run python -m pytest -q tests
```

## Estado (medido en sim, seeds holdout ≥ 100000)
- Baseline 0 por percepción (color+depth+calibración, `policies/perception_scripted.py`): 1000/1000 nivel 1,
  1000/1000 nivel 2 (10 objetos), 90.8 % nivel 3 (24 geometrías + randomización). PASA el gate sim-to-real.
- ACT visual (LeRobot 0.6.1, ResNet18 desde cero, CPU): 50–57 %. No aprende a leer la posición del objeto.
- ACT de baja dimensión (`configs/policies/act_lowdim_level1.yaml`, features de percepción): entrenado con demos
  grabadas bajo servos randomizados (`outputs/act_lowdim_servo_v001`, nas=5) → **99.5 % nominal / 96.5 % con
  servos imperfectos en 1000 episodios: pasa el gate**. Entrenado con demos nominales: 87 % y frágil. Regla:
  grabar las demos con `*_servo.yaml`.
- Nivel 7 (pedidos: caja origen con SKUs mezclados en caja/bolsa/suelto → caja de pedido con productos previos,
  `configs/sim/level7a_orders_sparse.yaml`, `scripts/evaluate_orders.py`): objetivo final; **89 % de pedidos
  completos / 92 % de ítems en 400 pedidos holdout (E067)**, 0 % SKU equivocado, 0 % previos perturbados, 8 %
  colisiones. Herramienta de traza reproducible por pedido: `run_order(step_callback=...)` (ver EXPERIMENTS
  E064–E067). Siguiente: colisiones pad-pared al soltar en la caja de pedido, SLIP de la pastilla de freno,
  encaje de piezas cuadradas al soltar, bolsas deformables, detector de SKU aprendido en lugar del oráculo.
- Nivel 4 (motopartes aproximadas): 93 % privilegiado, 82 % con detector por altura sin color (`detect_by_height`).
  Nivel 5 (4 objetos simultáneos): 98 %. Nivel 6 (clutter): 70 % (vecinos desplazados por el gripper al bajar;
  próximo paso: elegir el yaw de agarre con más holgura respecto a los vecinos). Nivel 7 (bin) pendiente.
- Robot real / OAK-D: código completo pero NUNCA probado físicamente (no había hardware). Empezar por
  `docs/REAL_ROBOT.md` Test 1. SmolVLA / π₀.₅: no se pudieron descargar (red bloqueada) → `docs/TRAINING.md`.

## Decisiones que no hay que rehacer
- Estado proprioceptivo con rotación 6D (no cuaternión: cambia de signo a 180°).
- `mj_setConst` antes de fijar qpos en reset (si no, la randomización se pierde).
- Gripper sim: apertura interna 45 mm, kp 400, condim 6; contacto objeto-palma no es colisión.
- Percepción: cara superior por banda de 3 mm en z (6 mm sesga 2 mm hacia la cámara); yaw por minAreaRect
  del lado corto; excluir blobs dentro de la bandeja; re-detectar solo con gripper abierto y TCP ≥ 0.10 m
  (regla compartida entre grabación y despliegue, `perception/env_state.py`).
- Experto scripted para demos de policies: ganancia 0.3 + velocidad mínima 0.25 (acciones no saturadas);
  DART empeoró dos veces; replanificar cada paso impide que ACT cierre el gripper; nas > 5 suelta en el aire.
- Robustez a servos (`configs/sim/*_servo.yaml`, benchmark de regresión obligatorio): los deltas se aplican al
  setpoint comandado, con adelanto acotado por PROYECCIÓN (1.5 cm) y nunca re-anclando a la medición (re-anclar
  hace oscilar/flotar); descenso con desaceleración (zona 2.5 cm, piso 0.1), tolerancia 6 mm y cierre por
  presupuesto de pasos; bajada a bandeja con tolerancia 8 mm y presupuesto; agarre 2 mm sobre el centro.
  Resultado: nivel 1 92.5 % con servos imperfectos, 100 % nominal. Órdenes en español: `picking/language`.
- Seeds < 100000 entrenamiento; ≥ 100000 evaluación. Datasets y calibraciones nunca se sobrescriben.

## Próximos pasos sugeridos
1. Mac Studio: `./setup.sh`, `./run_simulation.sh 100`, luego Test 1 del robot (`./run_robot.sh readonly`),
   Tests 2–7, calibración (`./run_robot.sh calibrate`), 100 intentos reales con la policy de percepción.
2. Policy aprendida: selección de checkpoint por evaluación, gripper como acción discreta, backbone
   preentrenado (con internet) y entrenamiento en MPS/GPU; después SmolVLA y π₀.₅ con el mismo benchmark.
3. Nivel 6 (clutter: objetos en contacto) y 7 (bin) sobre `level5_multi_object.yaml`; detector de vocabulario
   abierto para piezas reales detrás de la interfaz `ObjectDetection`.

## Reglas del proyecto
Nada se declara terminado sin métricas holdout; ninguna acción al robot sin `SafetyFilter`; registrar cada
experimento en `EXPERIMENTS.md` (ID, hipótesis, cambio, resultado, keep/revert); `main` siempre ejecutable.
