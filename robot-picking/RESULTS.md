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
| 4 (10 motopartes aproximadas, oscuras/metálicas) | Baseline 0 privilegiado | 100 | 93 % | — | 0.0 % | 2 % | 0 % | — | (n=100) |
| 4 | Baseline 0 percepción por ALTURA (sin color) | 100 | 82 % | — | 0.0 % | 2 % | 0 % | — | (n=100) |
| 5 (4 objetos simultáneos, objetivo por instrucción) | Baseline 0 percepción | 100 | 98 % | 99 % | 0.0 % | 0.0 % | 1 % | — | (n=100) |
| 6 (clutter: 4 objetos casi en contacto) | Baseline 0 percepción + pre-conformado del gripper | 100 | 70 % | — | 3 % | 0.0 % | 0 % | — | no (17 % objeto equivocado desplazado) |
| 1 (cubo) | ACT visual (LeRobot 0.6.1, ResNet18 desde cero, 128×128), 200 demos, ckpt 500/1000/2000 | 30 c/u | 53 % / 50 % / 53 % | 60 % | 0 % | 10 % | ~45 % | — | no |
| 1 (cubo) | ACT visual, recorte ajustado 160×90 (cubo ≈15 px), con/sin desplazamiento aleatorio | 30 c/u | 50–57 % | 57 % | 7–17 % | 0 % | 27–37 % | — | no |
| 1 (cubo) | ACT visual sin VAE | 30 | 3 % | 60 % | 27 % | 7 % | 67 % | — | no |
| 1 (cubo) | ACT de baja dimensión (estado + pose del objeto desde la percepción), experto suave + velocidad mínima, 200 demos nominales, n_action_steps=5 | 1000 (seeds 101000+) | 87.2 % | 99.7 % | 0.0 % | 3.8 % | 16.2 % | 13.6 / 14.8 | no |
| 1 (cubo) | **ACT de baja dimensión entrenado con 300 demos bajo servos randomizados** (`act_lowdim_servo_v001`), nas=5 | **1000** (seeds 101000+) | **99.5 %** | 99.7 % | 0.0 % | 0.2 % | 0.5 % | 7.3 / 7.7 | **PASA** |
| 1 (cubo) con servos imperfectos | idem | 1000 | 96.5 % | 97.4 % | 0.0 % | 0.1 % | 4.3 % | 10.0 / 14.7 | no (97 %) |

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
  experto *suave* (acciones proporcionales, no saturadas, con velocidad mínima) y ejecutar chunks de 5 acciones;
  (3) la policy aprendida es frágil: otro entrenamiento con 400 demos del mismo experto dio 17 % (E032), así que
  hace falta selección de checkpoint por evaluación y una acción de gripper discreta antes de confiar en ella.

## 1b. Robustez a servos imperfectos (predicción de Beto real)

Configs `level1_cube_servo.yaml` / `level2_objects_servo.yaml`: juego de servos hasta 0.8°, latencia de comando
hasta 0.2 s, ruido de lectura 0.2°, rigidez ×0.8–1.2 (inspirado en el sim2real del Open Duck). Baseline por percepción,
200 episodios holdout cada uno:

| Iteración (EXPERIMENTS E044–E055) | Nivel 1 | Nivel 2 | Nominal nivel 1 |
|---|---|---|---|
| Inicial | 18 % | 18 % | 100 % |
| Deltas sobre el setpoint comandado (no sobre la medición retrasada) | 37 % | — | 100 % |
| + desaceleración del descenso, presupuesto por fase, proyección del setpoint | 55–86 % | 54–73 % | 100 % |
| + tolerancia de descenso 6 mm, cierre por presupuesto, piso de velocidad 0.1 | 91.5 % | 86 % | 100 % |
| + agarre 2 mm sobre el centro | **92.5 %** (col 4.5 %, timeout 3 %) | **83 %** | **100 %** |

Reglas que salieron de aquí y aplican al robot real: aplicar los deltas al último setpoint comandado y acotar su
adelanto por proyección (1.5 cm) sin re-anclar; desacelerar en los últimos 2.5 cm; tolerancias y presupuestos de
pasos por fase; el ACT de baja dimensión (87 % nominal) cae a 0 % con servos imperfectos.

## 1c. Nivel 7 — preparación de pedidos (objetivo final, en construcción)

Escenario (`configs/sim/level7a_orders_sparse.yaml`, `scripts/evaluate_orders.py`): caja origen (14×12 cm, la
mayor que alcanza Beto) con 3 ítems de SKUs mezclados (en caja, en bolsa, sueltos), caja de pedido (11×10 cm) con
1 producto previo, pedido de 2 SKUs; identificador de SKU = oráculo con ruido de 2 mm (placeholder del detector
aprendido). Resultados holdout (E071):

| Métrica | 400 pedidos (seeds 100100–100499) | **1000 pedidos (seeds 100500–101499)** |
|---|---|---|
| Pedidos completos (todos los ítems, previos intactos, sin colisión) | 91.0 % | **92.2 %** (16 % en la primera versión E058; 58 % en E063) |
| Ítems colocados | 94.1 % | **94.7 %** (sueltos 94 %, cajas 94 %, bolsas 97 %) |
| SKU equivocado | 0.25 % | 0.1 % de los pedidos (oráculo con ruido de posición; el error de etiqueta es configurable) |
| Productos previos perturbados | 0 % | 0 % |
| Colisiones (pad-pared/piso > 5 N) | 7.8 % | 6.8 % de los pedidos |
| Fallos por ítem (1000 pedidos) | — | 72 colisiones, 14 SLIP, 7 NO_GRASP, 6 objetivo inalcanzable, 4 PLACE_FAIL, 2 DROP, 2 TIMEOUT, 1 pieza equivocada |
| Peores SKUs | caja de cables 40×40 (85 %), rulemán y piñón (90 %) | holgura de 2.5–6 mm con la apertura máxima de 45 mm: un error de detección > 3 mm apoya una almohadilla sobre la pieza, la desplaza hacia una pared y el reintento roza la pared |
| Tiempo mediano por pedido (2 ítems) | — | 17 s simulados |

Qué hizo la diferencia (E058 → E071, ver EXPERIMENTS.md): anchos y huellas reales por SKU; base de datos de
agarre por SKU (ancho, profundidad de pinzado bajo el ecuador para piezas redondas); generador de escenas
físicamente consistente (nunca superponer piezas: la física las empujaba contra la pared y quedaban sin agarre);
ranuras de colocación alejadas de las paredes según la apertura del gripper; yaw de agarre y apertura
pre-conformada por holgura a paredes y vecinos; descenso a velocidad acotada (la deriva xy del descenso rápido
hacía aterrizar una almohadilla sobre la pieza); recuperación (subir) tras colisión; yaw alternativo cuando el IK no
alcanza; bajada consciente del apilado; giro lento de muñeca en transporte; criterio de colocación que tolera un
roce al retirar. Trazas reproducibles por pedido con pares de contacto (`run_order(step_callback=...)`).

Limitaciones del modelado que condicionan estas cifras: bolsas rígidas (bultos planos), rulemán aproximado por
cápsulas, caja origen pequeña por el alcance de Beto (28 cm; en el 13 % de las escenas sólo entran 2 ítems),
ausencia de estrategia de empuje para piezas pegadas a una pared. Regresiones obligatorias que se mantienen:
nivel 1 y 2 nominales 100 %, con servos imperfectos 95 % / 88 %.

## 2. Robot real (myCobot 280 + OAK-D)

**No ejecutado**: este repositorio se construyó en un contenedor sin acceso al hardware. Todo lo necesario está
implementado (`scripts/test_mycobot_readonly.py`, `scripts/real_tests.py` tests 2–7, `scripts/calibrate_camera.py`,
`scripts/run_policy.py`, `scripts/evaluate_real.py`) pero **ninguna métrica real existe todavía**. Ver
docs/REAL_ROBOT.md para la secuencia y los criterios (100 intentos ≥ 90 %, luego 500 ≥ 95 %).

## 3. Motopartes

Sólo en simulación, con 10 piezas aproximadas por primitivas compuestas (`configs/sim/level4_motoparts.yaml`:
pastilla de freno, rulemán, bujía, relé, palanca de freno, piñón, grommet, tuerca, espejo, tapa): 93 % con pose
privilegiada y 82 % con el detector por altura sobre la mesa (sin color, `detect_by_height`). Fallo sistemático:
palanca de freno (barra cilíndrica tumbada resbala entre almohadillas planas: límite del gripper). Benchmark con
piezas reales: pendiente de hardware; la percepción deberá pasar a un detector de vocabulario abierto detrás de
la misma interfaz `ObjectDetection`.

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
