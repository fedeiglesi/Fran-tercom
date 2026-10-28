# Robot real — primer contacto y operación

**Gate obligatorio**: no ejecutar una policy en el robot real hasta que en simulación 1000 episodios holdout
den success ≥ 95 %, colisiones ≤ 0.5 %, drops ≤ 2 %, timeouts ≤ 2 % (ver RESULTS.md).

## Preparación (una vez)

1. Instalar extras de hardware: `PICKING_REAL=1 ./setup.sh` (o `uv sync --extra real`).
2. Conectar: fuente del robot, cable USB-C del M5 al Mac, OAK-D al Mac (USB 3).
3. Encender el robot (interruptor en la base). Mantener la mano cerca del interruptor en todas las pruebas.
4. Despejar la mesa.

## Secuencia de pruebas (`./run_robot.sh`)

| Test | Comando | Qué hace | Resultado esperado |
|---|---|---|---|
| 1 | `./run_robot.sh readonly` | lee versión, joints, coords; **no mueve** | 6 ángulos y FK impresos |
| 2 | `./run_robot.sh test 2` (`--joint N`) | +5° un joint y vuelve, lento | el joint gira; comparar dirección con el modelo → `joint_sign` |
| 3 | `./run_robot.sh test 3` | TCP +1 cm arriba y vuelve (IK propio) | delta medido ≈ 0.01 m en z |
| 4 | `./run_robot.sh test 4` | abre/cierra gripper | estado 1.0 cerrado / 0.0 abierto |
| 5 | `./run_robot.sh test 5` | va a HOME (gripper vertical sobre la mesa) | TCP ≈ (0.18, 0.00, 0.12) |
| 6 | `./run_robot.sh test 6` | acciones por `RobotAdapter` + `SafetyFilter` | baja 2 cm, sube, gripper |
| 7 | `./run_robot.sh test 7 outputs/<ckpt>` | policy con velocidad ≤ 3 cm/s, operador marca S/F | episodio registrado |

Cada test pide escribir `YES` antes de mover. Si un resultado no coincide con lo esperado: **no seguir**,
revisar `configs/real/mycobot_280.yaml` (`joint_sign`, `joint_offset_deg`, `tcp_offset`, `port`).

## Calibración cámara-robot

`./run_robot.sh camera` (chequeo OAK-D), luego `./run_robot.sh calibrate` (docs/CALIBRATION.md).

## Evaluación real

`uv run python scripts/run_policy.py --policy act --checkpoint outputs/<ckpt> --episodes 100`
Objetivo inicial: 100 intentos, ≥ 90 % éxito, 0 colisiones peligrosas, 0 violaciones de workspace.
Después 500 intentos ≥ 95 %. Todo se graba en `data/datasets/real_eval_vNNN` y `reports/real/`.

## Inferencia remota (GPU)

Servidor: `uv run python scripts/policy_server.py --policy act --checkpoint ... --port 8765` (en la máquina GPU).
Cliente: `scripts/run_policy.py --policy remote --server-url ws://IP:8765`. Timeout ⇒ HOLD.
