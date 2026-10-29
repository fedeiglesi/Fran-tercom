# robot-picking — plataforma de picking robótico con aprendizaje para **Beto** (myCobot 280 M5 + OAK-D) → motopartes → UR10e

Este directorio contiene un sistema completo para enseñar a un brazo robótico a tomar objetos y dejarlos en
una bandeja a partir de una instrucción en texto, empezando en simulación y pasando luego al robot físico.
Está escrito para que una persona **sin conocimientos de programación** pueda ejecutarlo con 6 comandos.

## Qué hay dentro (resumen)

| Qué | Dónde | Estado |
|---|---|---|
| Simulador MuJoCo del myCobot 280 (geometría oficial), mesa, bandeja, objetos, cámara tipo OAK-D | `src/picking/simulation` | funcionando, con tests |
| Abstracción de robot (`RobotAdapter`): simulación, myCobot real, UR10e (mock) | `src/picking/robots` | funcionando (myCobot/UR10e pendientes de prueba física) |
| Capa de seguridad determinista | `src/picking/safety` | funcionando, con tests |
| Evaluador automático (pick, place, colisión, drop, timeout) + métricas | `src/picking/evaluation` | funcionando |
| Dataset LeRobot v3 versionado (éxitos y fallos) | `src/picking/datasets` | funcionando |
| Policies: heurística con percepción (Baseline 0: 1000/1000 niveles 1 y 2), ACT visual (~55 %), ACT de baja dimensión (87.2 % en 1000 ep), remota (cliente/servidor) | `src/picking/policies`, `networking` | ver RESULTS.md y EXPERIMENTS.md |
| Calibración cámara→robot (ArUco + puntos, hand-eye) | `src/picking/calibration` | validada en sim |
| Cámara OAK-D (DepthAI 3) y teleoperación por teclado | `src/picking/cameras`, `teleop` | pendiente de prueba física |
| Scripts de primer contacto con el robot (tests 1–7) | `scripts/`, `run_robot.sh` | pendiente de prueba física |

Documentación: `docs/` (ARCHITECTURE, HARDWARE, CALIBRATION, DATASET, TRAINING, SIMULATION, REAL_ROBOT,
SAFETY, MIGRATION_UR10E), `EXPERIMENTS.md` (historial), `RESULTS.md` (números).

## Instalación (Mac Studio, macOS)

1. Abrir la aplicación **Terminal** (Launchpad → Otros → Terminal).
2. Copiar y pegar, uno por uno, estos comandos (ENTER después de cada uno):

```bash
cd ~/Fran-tercom/robot-picking        # (la carpeta donde está este README; ajustar si la clonó en otro lugar)
./setup.sh
```
Resultado esperado: termina con `SETUP OK`. Si aparece `command not found: uv`, cerrar y reabrir Terminal
y repetir `./setup.sh`. Si falla la descarga, comprobar internet y repetir.

Para el robot real (pymycobot, depthai): `PICKING_REAL=1 ./setup.sh`.

## Los 6 comandos

| Comando | Qué hace | Resultado esperado |
|---|---|---|
| `./setup.sh` | instala todo y corre los tests | `SETUP OK` |
| `./run_simulation.sh 100` | 100 episodios de pick & place en simulación con el experto scripted | `"success_rate": 1.0` y carpeta `reports/eval/...` |
| `./record_demonstrations.sh 200` | graba 200 demostraciones en `data/datasets/level1_cube_scripted_vNNN` | `"n_episodes": 200` |
| `./train_policy.sh level1_cube_scripted_v001 6000` | entrena la policy ACT (horas en CPU) | carpeta `outputs/act_...` |
| `./evaluate_policy.sh outputs/act_... 100` | evalúa la policy entrenada en seeds no vistas | `success_rate`, `collision_rate`, ... |
| `./run_robot.sh readonly` | **robot real**: lee estado sin mover (Test 1) | ángulos de los 6 joints |

Si algo no coincide con lo esperado: copiar el texto completo del error y revisar `docs/` (cada doc tiene
una sección "si aparece otro resultado").

## Reglas del proyecto

* Nada se da por terminado sin métricas en episodios no vistos (`seed >= 100000`).
* No se controla el robot real con una policy hasta pasar el gate de simulación (1000 episodios, ≥95 %).
* Ninguna acción llega al robot sin la capa de seguridad.
* Los datasets y calibraciones nunca se sobrescriben: se versionan.
