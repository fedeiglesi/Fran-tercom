# Seguridad

## Principios

1. **Ninguna acción llega al robot sin pasar por `SafetyFilter`** (`src/picking/safety/filter.py`). Es
   determinista y no depende de la policy. La policy no puede modificar sus límites.
2. **El adapter real nunca se mueve sin `arm()` explícito** y rechaza objetivos fuera de los límites
   articulares (margen configurable). `dry_run=True` sólo registra lo que enviaría.
3. **Pérdida de comunicación ⇒ HOLD**: `RemotePolicy` devuelve `None` ante timeout / respuesta vieja /
   desconexión; `SafetyFilter` devuelve `None` ⇒ `adapter.stop()`.
4. **Nunca loops físicos infinitos**: reintentos limitados (`max_retries`), timeout por episodio.
5. **Parada física accesible**: el operador mantiene la mano en el interruptor del myCobot. `x`/ESC en
   teleop ⇒ `stop()`.

## Límites (configs/safety)

| Parámetro | sim_default.yaml | real_mycobot.yaml (primeras pruebas) |
|---|---|---|
| workspace x [m] | 0.08 – 0.30 | 0.12 – 0.26 |
| workspace y [m] | −0.22 – 0.22 | −0.15 – 0.15 |
| workspace z (TCP) [m] | 0.008 – 0.22 | 0.015 – 0.20 |
| cono de orientación (desde vertical) | 35° | 25° |
| Δpos máx por paso | 12 mm | 5 mm |
| Δrot máx por paso | 0.10 rad | 0.05 rad |
| velocidad TCP máx | 0.15 m/s | 0.05 m/s |
| timeout sin acción válida | 0.5 s | 0.5 s |
| edad máx de una acción | 0.3 s | 0.3 s |

Qué hace el filtro, en orden: valores inválidos (NaN/Inf/forma) ⇒ STOP; recorta magnitudes a [−1,1];
limita velocidad; recorta el objetivo al workspace; si el TCP ya está muy fuera del workspace ⇒ STOP;
anula la rotación que sacaría al gripper del cono; si ya está fuera del cono ⇒ STOP.

Eventos y conteos (`n_clamped`, `n_rejected`, `events`) se guardan en cada `EpisodeResult`.

## Procedimiento ante incidente

1. Pulsar parada / cortar alimentación del robot.
2. No reiniciar hasta entender la causa: revisar `reports/.../episodes.json` (campo `safety`).
3. Si fue un objetivo IK raro: reducir `max_delta_pos` y el workspace; revisar `joint_sign` del adapter.

## Tests

`tests/test_safety.py` cubre NaN, recorte, workspace, cono, acciones viejas y timeout.
