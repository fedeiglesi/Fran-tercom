# Simulación

Motor: **MuJoCo 3.x** (`src/picking/simulation`). Render headless con OSMesa en Linux sin pantalla
(`picking/utils/gl.py` lo configura solo); en macOS se usa el backend por defecto.

## Modelo del myCobot 280 M5

* Cadena cinemática, orígenes de joints y límites transcritos del URDF oficial
  (`elephantrobotics/mycobot_ros`, `mycobot_280_m5.urdf`) en `simulation/mjcf.py: URDF_CHAIN, JOINT_LIMITS`.
* Mallas visuales oficiales (DAE → OBJ con `scripts/convert_dae_to_obj.py`), colisiones por hull convexo.
* Gripper paralelo simplificado (2 almohadillas, 45 mm, TCP a 65 mm de la brida).
* Actuadores de posición + compensación de gravedad (emula servos sin caída estática).
* Cámara externa `oakd_sim`: fovy 55° (≈ OAK-D RGB 69° HFOV a 4:3), 640×480, pose en config; RGB + depth.

## Escena nivel 1 (`configs/sim/level1_cube.yaml`)

Cubo rojo de 25 mm en (0.18, 0.09) ± 15 mm, yaw ± 10°; bandeja azul 80×80 mm en (0.14, −0.14).
Control a 10 Hz, física a 500 Hz, timeout 150 pasos (15 s).

## Niveles del currículo (configs/sim)

| Nivel | Config | Contenido |
|---|---|---|
| 1 | `level1_cube.yaml` | 1 cubo rojo 25 mm, ±15 mm, ±10° |
| 2 | `level2_objects.yaml` | 10 objetos simples (cajas, cilindros, esfera), 4 colores, ±3 cm, ±45°, masa/fricción aleatorias, uno por episodio |
| 3 | `level3_geometries.yaml` | 24 geometrías (cajas, cilindros, esferas, cápsulas, elipsoides) + ruido de cámara 3 mm/0.5°, luz ±30 %, color ±0.06, color de mesa |
| 4–7 | pendientes | motopartes digitalizadas, varios objetos, clutter, bin |

Resultados por nivel en RESULTS.md. Con `one_object_per_episode: true` el objetivo se elige por seed y los demás
objetos se aparcan fuera de la vista; la instrucción se genera con `instruction_template`.

## Comandos

```
./run_simulation.sh 100                      # baseline heurístico, 100 episodios holdout
uv run python scripts/evaluate_sim.py --policy heuristic --episodes 1000 --gate target
uv run python scripts/evaluate_sim.py --policy act --checkpoint outputs/<ckpt> --episodes 1000
```
Opciones: `--override '{"randomization":{"object_xy_range":[0.03,0.03]}}'` para variar la config sin editarla.

## Randomización (`simulation/randomization.py`)

Objeto: posición, yaw, masa, fricción, color. Cámara: posición y orientación. Mundo: luz, color de mesa.
Rangos por nivel en el YAML (`randomization:`). Los valores muestreados se guardan en cada `EpisodeResult`.

## Checks previos a entrenar (todos automatizados en `tests/`)

reset determinista por seed · cámara RGB/depth · API de acciones · gripper · detección de éxito · detección de
colisión · logging · grabación y relectura del dataset.
