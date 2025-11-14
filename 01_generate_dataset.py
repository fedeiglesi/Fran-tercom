#!/usr/bin/env python3
"""
Genera 100k diálogos WhatsApp estilo Fran para fine-tune Mistral-7B
Uso: python 01_generate_dataset.py --output data/fran_100k.jsonl --num-samples 100000
"""

import json
import random
import argparse
from datetime import datetime

# Precios reales ±5 %
BASE_PRICES = {
    "GEL 12N-7A-3A": 28371,
    "GEL 12N-5-3B": 23967,
    "YB 4L-B s/acido": 40355,
}

CODES = {
    "GEL 12N-7A-3A": "1234/56789-012",
    "GEL 12N-5-3B": "1234/56789-013",
    "YB 4L-B s/acido": "1234/56789-014",
}

MOTOS = ["Honda Wave 110", "Wave 125", "AX-1 100", "XR-190", "Bajaj Rouser 135"]
CANTIDADES = [1, 2, 5, 10, 20, 50]

def round_price(p):
    return int(p * random.uniform(0.97, 1.03))

def generate_dialog():
    prod = random.choice(list(BASE_PRICES.keys()))
    price = round_price(BASE_PRICES[prod])
    code = CODES[prod]
    moto = random.choice(MOTOS)
    qty = random.choice(CANTIDADES)
    total = price * qty

    # Turnos
    user_1 = f"tenes {prod.lower()} para {moto.lower()}?"
    bot_1 = f"Sí, tengo estas opciones que van como piña:\n- {prod} (cod {code}) → ${price:,}\n¿Cuál te interesa?"
    user_2 = f"esas sirven para la {moto.lower()}?"
    bot_2 = f"Exacto, ambas son específicas para {moto}. La {prod.split()[-1]} tiene un poco más de capacidad. ¿Querés una de cada o te alcanza con la más económica?"
    user_3 = f"dame {qty} de la más barata"
    bot_3 = f"Listo, {qty}× {prod} = ${total:,}. ¿Confirmamos?"
    user_4 = "dale"
    bot_4 = "Perfecto, pedido confirmado. Envío gratis a CABA. Te llega mañana."

    # Formato Mistral-7B chat-template
    return {
        "conversations": [
            {"from": "human", "value": user_1},
            {"from": "gpt", "value": bot_1},
            {"from": "human", "value": user_2},
            {"from": "gpt", "value": bot_2},
            {"from": "human", "value": user_3},
            {"from": "gpt", "value": bot_3},
            {"from": "human", "value": user_4},
            {"from": "gpt", "value": bot_4},
        ]
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/fran_100k.jsonl")
    parser.add_argument("--num-samples", type=int, default=100_000)
    args = parser.parse_args()

    import os
    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        for i in range(args.num_samples):
            dialog = generate_dialog()
            f.write(json.dumps(dialog, ensure_ascii=False) + "\n")
            if (i + 1) % 10_000 == 0:
                print(f"{i + 1:,} samples generated")

    print(f"✅ Dataset guardado en {args.output}")

if __name__ == "__main__":
    main()
