# Dataset

Formato: **LeRobotDataset v3** (`lerobot` 0.6.x), creado con `EpisodeRecorder` (`src/picking/datasets/recorder.py`).
Ubicación: `data/datasets/<nombre>_vNNN/` — **nunca se sobrescribe**; cada grabación crea una versión nueva.

## Features por frame (`datasets/schema.py`)

| Clave | Tipo | Contenido |
|---|---|---|
| `observation.images.<cam>` | uint8 H×W×3 (PNG) | imagen recortada al ROI del workspace y reescalada (128×128 por defecto) |
| `observation.depth.<cam>` | float32 H×W | profundidad en metros (opcional, `--depth`) |
| `observation.state` | float32 (10,) | TCP xyz + rotación 6D + gripper |
| `observation.joints` | float32 (6,) | joints reales (rad), auxiliar |
| `action` | float32 (7,) | acción normalizada **ejecutada** (tras safety) |
| `extra.policy_action` | float32 (7,) | acción cruda de la policy/operador |
| `extra.target_tcp_pose` | float32 (7,) | pose TCP alcanzada tras el paso (xyz + quat) |
| `extra.target_joints` | float32 (6,) | joints resultantes |
| `extra.object_pose` | float32 (7,) | pose privilegiada del objeto (sim) |
| `extra.safety_rejected` | float32 (1,) | 1 si safety detuvo el robot en ese paso |
| `extra.corrected` | float32 (1,) | 1 si la acción ejecutada fue una corrección humana (DAgger, `scripts/dagger.py`) |
| `task` | str | instrucción textual |
| `timestamp, frame_index, episode_index, index, task_index` | — | añadidos por LeRobot |

## Metadatos por episodio (`picking_meta/episodes.jsonl`)

`success, grasp_success, place_success, collision, drop, timeout, failure_type (NO_GRASP/SLIP/COLLISION/DROP/
PLACE_FAIL/TIMEOUT/...), n_steps, cycle_time_s, path_length_m, n_retries, seed, task, object_id, metadata
(policy, policy_version, randomization, safety events, git sha...)`.

`picking_meta/dataset_info.json`: versión de esquema, git SHA, fps, features, configuración resuelta.

## Seeds

Entrenamiento: seeds `< 100000`. Evaluación holdout: seeds `>= 100000`. Nunca se entrena con seeds de test.

## Procedimiento

* Sim, experto scripted: `./record_demonstrations.sh 200 level1_cube_scripted`
* Sim, teleop: `uv run python scripts/teleop.py --sim --record --name teleop_sim`
* Real, teleop: `uv run python scripts/teleop.py --real --record --name teleop_real`
* Real, policy (con fallos y correcciones): `scripts/run_policy.py` graba todo.

## Lectura

```python
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset("local/level1_cube_scripted_v001", root="data/datasets/level1_cube_scripted_v001",
                    delta_timestamps={"action": [i/10 for i in range(20)]})
item = ds[0]   # imágenes (3,H,W) float en [0,1], action (20,7), action_is_pad (20,)
```
