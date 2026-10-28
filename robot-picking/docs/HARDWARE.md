# Hardware y conexiones

## Equipos

| Equipo | Rol | Conexión |
|---|---|---|
| Mac Studio (M3 Ultra, 90 GB) | PC principal: cámara, robot, dataset, entrenamiento ligero, cliente de inferencia | — |
| myCobot 280 M5 | Brazo 6 DOF + gripper adaptativo | USB-C del M5 → Mac (USB serie, 115200 baud) + fuente 12 V del robot |
| OAK-D | Cámara RGB + estéreo (depth) | USB-C 3.0 → Mac. Fija en trípode mirando la mesa |
| Servidor NVIDIA (opcional) | Entrenar/servir VLA (SmolVLA, π₀.₅) | LAN; `scripts/policy_server.py` |

## Montaje

* El robot va atornillado a la mesa. **Frame base** = centro de la base del robot, z hacia arriba, z=0 en
  la superficie de la mesa (la base del modelo `joint1` apoya en z=0).
* La cámara se coloca enfrente del robot, a ~0.5 m del centro del área de picking y ~0.4 m de altura,
  apuntando al centro de la mesa (equivalente a `configs/sim/level1_cube.yaml: camera.position / lookat`).
  Debe ver toda el área de trabajo y la bandeja, y no debe moverse después de calibrar.
* Bandeja destino: dentro del área alcanzable con el gripper vertical (ver mapa de alcance abajo).

## Área alcanzable con gripper vertical (modelo cinemático URDF oficial)

Verificado numéricamente con IK (`scripts/` test de alcance): x ∈ [0.14, 0.26] m, |y| ≤ 0.12 m a z=0.02 m,
y una zona más amplia para |y| mayor en x ∈ [0.16, 0.20]. Fuera de eso la IK falla (el adapter no se mueve).

## Cosas que hay que VERIFICAR físicamente antes de control autónomo

| # | Qué | Cómo | Dónde se configura |
|---|---|---|---|
| 1 | Modelo exacto / firmware | `./run_robot.sh readonly` imprime versión y firmware | — |
| 2 | Puerto serie | `ls /dev/tty.*` en macOS (p.ej. `/dev/tty.usbserial-XXXX`) | `configs/real/mycobot_280.yaml: port` |
| 3 | Versión pymycobot | `uv run python -c "import pymycobot;print(pymycobot.__version__)"` | `pyproject.toml` (extra `real`) |
| 4 | Sentido y offset de cada joint vs. modelo | Test 2 por joint: mover +5° y comparar con `get_coords` y FK | `joint_sign`, `joint_offset_deg` |
| 5 | Offset flange→centro de agarre (`tcp_offset`) | Medir con regla desde la brida al centro de las almohadillas | `tcp_offset` |
| 6 | Apertura real del gripper y mapeo 0–100 | Test 4 | `gripper_value_open/closed`, `gripper_max_opening` |
| 7 | Unidades de `get_coords` | Test 2/3 (mm y grados) — se registran pero no se usan para control | — |
| 8 | Límites articulares reales | Comparar con `JOINT_LIMITS` en `simulation/mjcf.py` (del URDF oficial) | `mjcf.py` |

## Gripper (aproximación en simulación)

En simulación el gripper es paralelo simplificado (dos almohadillas, apertura 45 mm, TCP a 65 mm de la brida).
El gripper adaptativo real tiene cinemática de palanca: medir y ajustar `tcp_offset` y `gripper_max_opening`.
