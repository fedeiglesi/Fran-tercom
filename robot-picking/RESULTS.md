# RESULTS — resultados cuantitativos (holdout seeds ≥ 100000)

Todos los números provienen de `reports/eval/<run>/summary.json` (git SHA, config resuelta, seeds y versiones
dentro de cada archivo). `uv run python scripts/report.py` regenera la tabla completa en `reports/RESULTS.md`.
Entorno de medición: contenedor Linux x86_64, 4 vCPU, sin GPU, MuJoCo 3.17, LeRobot 0.6.1, torch 2.11 (CPU).

## 1. Simulación — gate sim-to-real

| Nivel | Policy | Episodios | Success | Grasp | Collision | Drop | Timeout | Ciclo mediana / p95 [s] | Gate ≥97/≤0.5/≤2/≤2 |
|---|---|---|---|---|---|---|---|---|---|
| 1 (cubo) | Baseline 0 privilegiado (`heuristic`) | 1000 | **100.0 %** | 100 % | 0.0 % | 0.0 % | 0.0 % | 6.8 / 7.0 | PASA |
| 1 (cubo) | Baseline 0 percepción (`perception`, calibración por archivo) | 1000 | **100.0 %** | 100 % | 0.0 % | 0.0 % | 0.0 % | 8.1 / 8.4 | PASA |
| 2 (10 objetos) | Baseline 0 percepción | 1000 | **100.0 %** | 100 % | 0.0 % | 0.0 % | 0.0 % | 7.9 / 8.5 | PASA |
| 3 (24 geometrías + DR cámara/luz/color) | Baseline 0 percepción | 120 | 90.8 % | 92 % | 1.7 % | 0.0 % | 0.0 % | — | no (elipsoides que ruedan) |
| 3 | Baseline 0 privilegiado | 120 | 92.5 % | — | 0.0 % | 0.0 % | 1.7 % | — | no |
| 1 (cubo) | ACT visual (LeRobot 0.6.1, ResNet18 desde cero, 128×128), 200 demos, ckpt 500/1000/2000 | 30 c/u | 53 % / 50 % / 53 % | 60 % | 0 % | 10 % | ~45 % | — | no |
| 1 (cubo) | ACT visual, recorte ajustado 160×90 (cubo ≈15 px), con/sin desplazamiento aleatorio | 30 c/u | 50–57 % | 57 % | 7–17 % | 0 % | 27–37 % | — | no |
| 1 (cubo) | **ACT de baja dimensión** (estado + pose del objeto desde la percepción), experto suave + velocidad mínima, 200 demos, n_action_steps=5 | 100 (seeds 100100+) | **84 %** | 100 % | 0 % | 3 % | 13 % | 13.4 / 14.8 | no (todavía) |

Notas:
* "Baseline 0 percepción" NO usa estado privilegiado: segmentación de color + profundidad + `T_base_camera`
  (archivo de calibración con 3.1 mm de error respecto a la pose real) + verificación de agarre (apertura del
  gripper + señal visual) + reintentos con re-observación. Es la policy candidata para el primer contacto real.
* Nivel 3: 24 objetos; 19 al 100 %. Fallos concentrados en `red_egg` (elipsoide inestable que rueda al ser tocado
  → BAD_TARGET fuera de alcance o no queda en reposo en la bandeja), `blue_bean`, `green_lens`. Desglose por objeto
  en `reports/eval/*level3_perception_v2/episodes.json`.
* Latencia de policy: heurística < 0.1 ms; ACT visual en CPU (ResNet18, 128×128) ≈ 125 ms/paso; ACT de baja dimensión ≈ 2 ms/paso.
* Historia completa de las 30 iteraciones del loop experimental (hipótesis, resultado, keep/revert) en EXPERIMENTS.md.
  Hallazgos clave para policies aprendidas: (1) el ACT visual entrenado desde cero en CPU no aprende a leer la
  posición del objeto (ejecuta la trayectoria media); (2) con features explícitas de percepción, lo decisivo fue un
  experto *suave* (acciones proporcionales, no saturadas, con velocidad mínima) y ejecutar chunks de 5 acciones.

## 2. Robot real (myCobot 280 + OAK-D)

**No ejecutado**: este repositorio se construyó en un contenedor sin acceso al hardware. Todo lo necesario está
implementado (`scripts/test_mycobot_readonly.py`, `scripts/real_tests.py` tests 2–7, `scripts/calibrate_camera.py`,
`scripts/run_policy.py`, `scripts/evaluate_real.py`) pero **ninguna métrica real existe todavía**. Ver
docs/REAL_ROBOT.md para la secuencia y los criterios (100 intentos ≥ 90 %, luego 500 ≥ 95 %).

## 3. Motopartes

No iniciado (requiere objetos reales o digitalizados). El pipeline de nivel 3 (geometrías variadas + randomización)
es el punto de partida; la percepción por color deberá sustituirse por un detector de vocabulario abierto
detrás de la misma interfaz `ObjectDetection`.

## 4. Arquitectura (evidencia por tests)

| Afirmación | Evidencia |
|---|---|
| Cambiar de robot no cambia la API de la policy | `tests/test_adapters.py::test_ur10e_mock_follows_same_policy_api` (misma `Action` sobre el mock UR10e) |
| El adapter real no se mueve sin armar ni fuera de límites | `tests/test_adapters.py` (myCobot con dispositivo falso) |
| Safety independiente de la policy | `tests/test_safety.py` (NaN, workspace, cono, velocidad, timeout, acciones viejas) |
| Calibración versionada y verificada | `tests/test_calibration.py`, `calibration/oakd_to_mycobot_sim_v001.yaml` |
| VLA/policy local o remota | `tests/test_networking.py` (roundtrip, timeout ⇒ HOLD, servidor caído ⇒ HOLD) |
| Dataset interoperable (LeRobot v3) | `tests/test_dataset.py` |

## 5. Qué NO se pudo medir en este entorno

* SmolVLA / π₀.₅: pesos base en huggingface.co (bloqueado aquí) y, para π₀.₅, GPU NVIDIA. Código de entrenamiento
  de LeRobot instalado y documentado en docs/TRAINING.md.
* ACT con backbone preentrenado (ImageNet, download.pytorch.org bloqueado): se entrenó desde cero.
* Entrenamiento ACT completo (6000 pasos ≈ 0.2 it/s con 4 vCPU compartidas): resultados parciales arriba; en el
  Mac Studio (M3 Ultra, MPS) el mismo comando debería tardar una fracción.
