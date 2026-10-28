# Calibración OAK-D ↔ base del robot

Se resuelve `T_base_camera` (pose del frame óptico de la cámara — convención OpenCV — en el frame base del robot).
Módulo: `src/picking/calibration/extrinsics.py`. Script: `scripts/calibrate_camera.py`.

## Método principal: correspondencias de puntos (Kabsch/SVD)

Robusto, sin necesidad de marcador en el gripper:

1. Imprimir un marcador ArUco `DICT_4X4_50` id 0, ~30 mm (p.ej. https://chev.me/arucogen/).
2. Dejarlo plano sobre la mesa donde la cámara lo vea.
3. `./run_robot.sh calibrate` → con el teclado (o a mano, con los servos liberados) llevar la punta del gripper
   cerrado a tocar el centro del marcador; ENTER. El script guarda: posición TCP (FK del robot, base) y
   posición 3D del centro del marcador (píxel + profundidad OAK-D, frame cámara).
4. Mover el marcador y repetir 6–10 veces cubriendo toda el área de trabajo y 2 alturas (poner el marcador
   sobre un bloque de 3–5 cm en algunos puntos).
5. `q` para resolver. Se acepta si RMS ≤ 4 mm y error máximo por punto ≤ 8 mm (umbrales en
   `check_calibration_quality`). Si no pasa, NO se guarda (usar `--force` sólo para depurar).

Resultado versionado: `calibration/oakd_to_mycobot_vNNN.yaml` con matriz, cuaternión, residuales, fecha,
procedimiento, git SHA, cámara, robot, setup.

## Método secundario: hand-eye (OpenCV, eye-to-hand)

Marcador fijado al gripper; N poses; `calibrate_hand_eye()` (PARK). Residual = dispersión de la posición del
marcador relativa al gripper. Implementado y testeado sintéticamente (`tests/test_calibration.py`).

## Verificación automática

`tests/test_calibration.py::test_sim_camera_depth_backprojection_matches_calibration` ejecuta el procedimiento
completo en simulación (intrínsecos → depth → deproyección → Kabsch) y comprueba que recupera la pose real
de la cámara (error de traslación < 1 cm; en la corrida de referencia 3.1 mm con ruido de 0.5 mm añadido).

`scripts/calibrate_camera.py --sim` reproduce lo mismo y guarda `calibration/oakd_to_mycobot_sim_vNNN.yaml`.

## Uso

```python
from picking.calibration.extrinsics import load_calibration, camera_point_to_base
T = load_calibration("calibration/oakd_to_mycobot_v001.yaml")
p_base = camera_point_to_base(T, camera.deproject(u, v, depth))
```
Nunca hardcodear coordenadas que deban salir de aquí.
