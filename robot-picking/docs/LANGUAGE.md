# Órdenes en español argentino

Los VLA abiertos (SmolVLA, π₀.₅) y las demos están en inglés, así que la orden del operario se normaliza antes
de llegar a la policy y a la percepción (`src/picking/language/orders.py`):

```
"Agarrá la palanca de freno y ponela en la bandeja 3"
   → Order(object_id=brake_lever, destination_id=bin_3,
           instruction_en="Pick the brake lever and place it in order bin 3.")
```

* **Backend por reglas** (`RuleOrderParser`): catálogo `configs/language/catalogo_es_ar.yaml` con alias en
  español rioplatense (voseo, diminutivos, jerga de taller), SKUs y destinos. Determinista, sin red, el alias
  más largo gana ("palanca de freno" > "palanca"), los destinos numerados ganan sobre "la bandeja".
* **Backend LLM** (`LLMOrderParser`, opcional): Claude (`claude-opus-5-5`) con salida JSON estructurada
  restringida al catálogo, para frases libres o nuevas. Requiere `pip install anthropic` y credenciales. Si el
  objeto no está en el catálogo la orden NO es ejecutable (nunca se inventa un objetivo).
* `parse_order(texto)` usa reglas y cae al LLM sólo si las reglas no reconocen el objeto.

Uso:
```
uv run python scripts/order.py "llevame el rulemán al cajón 2"
uv run python scripts/evaluate_sim.py --config sim/level4_motoparts.yaml --policy perception --detector height \
    --orden "agarrá la pastilla de freno y ponela en la bandeja"
uv run python scripts/run_policy.py --policy act --checkpoint outputs/<ckpt> --orden "tomá el cubo rojo"
```
El texto original se conserva en los reportes (`orden_es`) y puede guardarse en el dataset como `task_es`.
Agregar un producto nuevo = una línea en el catálogo (alias + SKU + nombre en inglés).
