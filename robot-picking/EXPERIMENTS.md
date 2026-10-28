# EXPERIMENTS

Cada fila: ID, hipótesis, modificación, commit, configuración, resultado (holdout seeds ≥ 100000), interpretación, keep/revert.

| ID | Hipótesis | Modificación | Commit | Config | Resultado | Interpretación | Decisión |
|---|---|---|---|---|---|---|---|
| E000 | El simulador + evaluator + action API + safety funcionan de extremo a extremo | Baseline 0 (scripted, estado privilegiado) | f363090 | level1_cube, sim_default | 100 ep: 0/100 TIMEOUT | el objetivo de lift se movía con el objeto (bug en la policy) y servo con caída por gravedad | fix |
| E001 | Compensación de gravedad en servos + objetivo de lift fijo arreglan E000 | `gravity_compensation`, `base_z` fijo en lift/transport, z máx workspace 0.22 | f363090 | idem | 100 ep: 100 % éxito, 0 colisiones, ciclo 6.7 s | pipeline validado | keep |
| E002 | La randomización no estaba activa (todas las posiciones idénticas) | `mj_setConst` antes de fijar qpos | 9dfc4ea | idem (±15 mm, ±10°) | 200 ep: 100 % éxito, 0 colisiones, 0 drops, p95 ciclo 7.0 s | Baseline 0 pasa el gate inicial a nivel 1 | keep |
