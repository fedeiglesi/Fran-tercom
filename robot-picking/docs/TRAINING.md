# Entrenamiento

## Baseline ACT (LeRobot 0.6.x) en CPU

```
./record_demonstrations.sh 200 level1_cube_scripted      # dataset versionado
./train_policy.sh level1_cube_scripted_v001 6000          # ACT, 6000 pasos
./evaluate_policy.sh outputs/<ckpt> 100                   # holdout
```
Config: `configs/policies/act_level1.yaml` (chunk 20, 10 pasos ejecutados por chunk, ResNet18 sin pesos
ImageNet para funcionar offline, dim 256). El checkpoint contiene `model.pt`, `dataset_stats.json` y
`picking_policy.json` (config, git SHA, dataset, nº de episodios/frames, historial de loss).

El wrapper `policies/act_policy.py` reconstruye el pre/post-procesado (normalización mean/std) de LeRobot y
expone la policy con la API común `Policy.act(obs)`.

## ACT de baja dimensión (features explícitas de percepción) — mejor resultado hasta ahora

La percepción (color + depth + calibración, `picking/perception`) produce `observation.environment_state` =
`[x, y, z, cos(k·yaw), sin(k·yaw)]` del objeto (k = 2π/periodo de simetría). La policy no ve imágenes:
entrena en minutos en CPU y corre a ~2 ms/paso.

```
uv run python scripts/record_demos.py --episodes 200 --seed-start 0 --name level1_cube_lowdim_smooth_floor \
    --lowdim --expert-gain 0.3 --expert-min-speed 0.25
uv run python scripts/train.py --config policies/act_lowdim_level1.yaml --dataset level1_cube_lowdim_smooth_floor_v001
uv run python scripts/evaluate_sim.py --policy act --checkpoint outputs/<ckpt> --act-override '{"n_action_steps": 5}' --episodes 100
```
Hallazgos (EXPERIMENTS.md E022–E031): la regla de "observar sólo con gripper abierto y TCP ≥ 10 cm" debe ser la
misma al grabar y al desplegar; el yaw debe codificarse invariante a la simetría del objeto; el experto debe ser
suave (ganancia 0.3) y con velocidad mínima (0.25) para evitar que la policy "se frene" cerca de la bandeja;
ejecutar 5 acciones por chunk (más → suelta en el aire; menos → nunca cierra el gripper). DART empeoró dos veces.

## Exportar a ONNX (inferencia liviana en Beto / Mac)

```
uv sync --extra export
uv run python scripts/export_onnx.py --checkpoint outputs/act_lowdim_smooth_floor_v001
```
Genera `policy.onnx` (17 MB) y `onnx_io.json` (nombres de entradas/salidas, estadísticas de normalización,
chunk y n_action_steps) y verifica la salida contra PyTorch (error < 1e-6). La policy de baja dimensión corre
con `onnxruntime` sin PyTorch.

## SmolVLA y π₀.₅

Ambos están en LeRobot 0.6.x (`lerobot.policies.smolvla`, `lerobot.policies.pi05`) y requieren descargar
pesos base de Hugging Face (`lerobot/smolvla_base`, `lerobot/pi05_base`) — **no disponible desde el
contenedor donde se construyó este repo (huggingface.co bloqueado)**. En el Mac Studio con internet:

```
uv sync --extra vla            # transformers, accelerate, peft...
uv run lerobot-train --policy.type=smolvla --policy.pretrained_path=lerobot/smolvla_base \
   --dataset.repo_id=local/level1_cube_scripted_v001 --dataset.root=data/datasets/level1_cube_scripted_v001 ...
```
π₀.₅ (3 B parámetros) necesita GPU NVIDIA para fine-tuning: usar el servidor GPU + `policy_server.py`.
La comparación debe hacerse con el mismo benchmark (`evaluate_sim.py`, mismos seeds) — ver EXPERIMENTS.md.

## Reproducibilidad

Cada checkpoint y cada evaluación guardan: git SHA, versiones (python/mujoco/torch/lerobot), config resuelta,
seeds, nombre del dataset. `reports/eval/<fecha>_<policy>/summary.json` + `episodes.csv/json/parquet`.
