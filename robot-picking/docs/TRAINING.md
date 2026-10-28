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
