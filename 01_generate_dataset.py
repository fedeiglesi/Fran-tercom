#!/usr/bin/env python3
"""
01_generate_dataset.py  →  versión 2.1  (TERCOM real)
- Lee CSV real (precios, códigos, stock)
- Lee tercom_data.json  (horarios, CBU, descuentos, envíos)
- Lee zonas_envio.csv   (tarifas por CP)
- Genera 20k diálogos reales: cambios, urgencias, proformas, etc.
- Append al JSON que ya existe → 120k final
Output: data/fran_100k.jsonl
"""

import json
import random
import pandas as pd
import argparse
import os
from datetime import datetime
import jinja2

# ---------- CONFIG ----------
CSV_PATH          = "catalogo_tercom_faiss.csv"
TERCOM_DATA_PATH  = "tercom_data.json"
ZONAS_ENVIO_PATH  = "zonas_envio.csv"
OUTPUT_APPEND     = "data/fran_100k.jsonl"
SAMPLES_TO_ADD    = 20_000
TC_BLUE           = 980  # tipo de cambio blue (no lo decimos)

# ---------- CARGA DE DATOS REALES ----------
def load_catalog():
    df = pd.read_csv(CSV_PATH)
    out = []
    for _, row in df.iterrows():
        price_raw = row.get("price_ars", 0)

        # Manejo robusto de precio
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            continue

        if pd.isna(price) or price <= 0:
            continue

        # Variación ±3 %
        price = int(price * random.uniform(0.97, 1.03))

        out.append({
            "name": row.get("name", "") or "",
            "code": row.get("code", "") or "",
            "price": price,
            "brand": row.get("brand", "") or "",
            "model": row.get("model", "") or "",
            "vehicle_type": row.get("vehicle_type", "") or "",
        })
    return out

def load_tercom_data():
    with open(TERCOM_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_zonas_envio():
    df = pd.read_csv(ZONAS_ENVIO_PATH)
    return df.to_dict(orient="records")

CATALOG       = load_catalog()
TERCOM_DATA   = load_tercom_data()
ZONAS_ENVIO   = load_zonas_envio()

# ---------- UTILS ----------
def round_price(p):
    return int(p)

def convert_usd_to_ars(usd):
    return int(usd * TC_BLUE)

def descuento_aplica(total):
    if total > TERCOM_DATA["descuento_umbral_ars"]:
        porc = TERCOM_DATA["descuento_porcentaje"]
        return porc, int(total * porc / 100)
    return 0, 0

def tarifa_envio(cp):
    cp_int = int(cp)
    for z in ZONAS_ENVIO:
        # Suponemos que inicio/fin son numéricos
        if z["inicio"] <= cp_int <= z["fin"]:
            return z["precio_ars"], z["nombre_zona"]
    return 3_000, "Interior"  # default

def numero_proforma():
    # Leemos el último número usado (si existe)
    counter_file = "data/ultima_proforma.txt"
    if os.path.exists(counter_file):
        with open(counter_file, "r") as f:
            n = int(f.read())
    else:
        n = 0
    n += 1
    with open(counter_file, "w") as f:
        f.write(str(n))
    return f"{TERCOM_DATA['proforma_prefijo']}-{n}"

def render_proforma_html(cliente, items, total_sin_iva, descuento, total_final, numero_pf):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader('.'))
    template = env.get_template('proforma_template.html')
    html_out = template.render(
        prefijo=TERCOM_DATA["proforma_prefijo"],
        numero=numero_pf,
        cliente_nombre=cliente["nombre"],
        cliente_direccion=cliente["direccion"],
        cliente_telefono=cliente["telefono"],
        fecha=datetime.now().strftime("%d/%m/%Y"),
        items=items,
        total_sin_iva=total_sin_iva,
        descuento=descuento,
        total_final=total_final,
        cbu=TERCOM_DATA["cbu"],
        alias_cbu="TERCOM.PY",
        horarios=TERCOM_DATA["horarios"],
        direccion=TERCOM_DATA["direccion"],
        telefono=TERCOM_DATA["telefono"],
        envios_texto=TERCOM_DATA["envios"]["texto"],
    )
    return html_out

# ---------- GENERADORES POR SITUACIÓN REAL ----------

def dialog_cambio_cantidad():
    p = random.choice(CATALOG)
    qty_ini = random.choice([10, 20, 50, 100])
    qty_fin = random.choice([5, 15, 30, 80, 150])
    total_ini = qty_ini * p["price"]
    total_fin = qty_fin * p["price"]
    return {
        "conversations": [
            {"from": "human", "value": f"Quiero {qty_ini} unidades de {p['name']} (cod {p['code']})"},
            {"from": "gpt", "value": f"Dale, {qty_ini}× {p['name']} = ${total_ini:,}. ¿Confirmamos?"},
            {"from": "human", "value": f"Mejor dame {qty_fin}, ¿se puede cambiar?"},
            {"from": "gpt", "value": f"Joya, cambio a {qty_fin}× {p['name']} = ${total_fin:,}. ¿Confirmás?"},
            {"from": "human", "value": "Sí, dale"},
            {"from": "gpt", "value": "✅ Listo, pedido actualizado. Te llega mañana."},
        ]
    }

def dialog_arrepentimiento():
    p = random.choice(CATALOG)
    qty = random.choice([5, 10, 20])
    total = qty * p["price"]
    return {
        "conversations": [
            {"from": "human", "value": f"Dame {qty} de {p['name']}"},
            {"from": "gpt", "value": f"Listo, {qty}× {p['name']} = ${total:,}. ¿Confirmamos?"},
            {"from": "human", "value": "Dale"},
            {"from": "gpt", "value": "✅ Confirmado. Envío gratis a CABA."},
            {"from": "human", "value": "Me arrepentí, cancelá todo por favor"},
            {"from": "gpt", "value": "Sin drama, cancelo el pedido. ¿Querés algo más o lo dejamos para la próxima?"},
        ]
    }

def dialog_urgencia():
    p = random.choice(CATALOG)
    qty = random.choice([1, 2, 5])
    total = qty * p["price"]
    stock_real = random.randint(qty, qty + 50)
    return {
        "conversations": [
            {"from": "human", "value": f"URGENTE necesito {qty} de {p['name']}, ¿tenés?"},
            {"from": "gpt", "value": f"Sí, tengo {stock_real} unidades. Te lo puedo dejar a ${total:,}. ¿Retirás hoy?"},
            {"from": "human", "value": "Sí, paso ahora"},
            {"from": "gpt", "value": "Joya, lo reservamos. Te esperamos hasta las 18 hs. Dirección: Av. Caseros 856, Villa Bernardo Monteagudo."},
        ]
    }

def dialog_cambio_producto():
    p1 = random.choice(CATALOG)
    p2 = random.choice(CATALOG)
    while p2["code"] == p1["code"]:
        p2 = random.choice(CATALOG)

    qty = random.choice([5, 10, 20])
    total1 = qty * p1["price"]
    total2 = qty * p2["price"]
    return {
        "conversations": [
            {"from": "human", "value": f"Dame {qty} de {p1['name']} (cod {p1['code']})"},
            {"from": "gpt", "value": f"Listo, {qty}× {p1['name']} = ${total1:,}. ¿Confirmamos?"},
            {"from": "human", "value": f"Pará, mejor cambiame por {p2['name']} (cod {p2['code']})"},
            {"from": "gpt", "value": f"Perfecto, cambio a {qty}× {p2['name']} = ${total2:,}. ¿Te sirve así?"},
            {"from": "human", "value": "Sí, dejalo así"},
            {"from": "gpt", "value": "✅ Pedido actualizado con el producto nuevo."},
        ]
    }

def dialog_consulta_tecnica():
    p = random.choice(CATALOG)
    medida = f"{random.randint(100, 200)} mm"
    amperaje = f"{random.randint(5, 12)} Ah"
    return {
        "conversations": [
            {"from": "human", "value": f"¿Qué medidas tiene el {p['name']} (cod {p['code']})?"},
            {"from": "gpt", "value": f"El {p['name']} mide {medida} y tiene {amperaje}. ¿Te sirve para tu moto?"},
            {"from": "human", "value": "Perfecto, dame 2"},
            {"from": "gpt", "value": f"Listo, 2× {p['name']} = ${2 * p['price']:,}. ¿Confirmamos?"},
        ]
    }

def dialog_negociacion_precio():
    p = random.choice(CATALOG)
    qty = random.choice([50, 100])
    precio_lista = p["price"]
    precio_neg = int(precio_lista * 0.92)  # 8 % descuento
    total_lista = qty * precio_lista
    total_neg = qty * precio_neg
    return {
        "conversations": [
            {"from": "human", "value": f"Quiero {qty} de {p['name']}, ¿me hacés mejor precio por volumen?"},
            {"from": "gpt", "value": f"Por {qty} unidades te dejo cada una a ${precio_neg:,} (8 % descuento). Total: ${total_neg:,}. ¿Te cerramos?"},
            {"from": "human", "value": "Perfecto, dale"},
            {"from": "gpt", "value": "✅ Listo, pedido confirmado. Te llega mañana."},
        ]
    }

def dialog_sin_stock():
    p = random.choice(CATALOG)
    qty = random.choice([100, 200])
    stock_real = random.randint(10, 50)
    return {
        "conversations": [
            {"from": "human", "value": f"Necesito {qty} de {p['name']}"},
            {"from": "gpt", "value": f"Tengo {stock_real} unidades en stock. ¿Te sirven esas o querés que te avise cuando entre el resto?"},
            {"from": "human", "value": "Dame las que tenés y avisame cuando entre el resto"},
            {"from": "gpt", "value": f"Listo, te mando {stock_real} ahora y te reservamos {qty - stock_real} para cuando entre. ¿Confirmamos?"},
        ]
    }

def dialog_proforma_completa():
    p1 = random.choice(CATALOG)
    p2 = random.choice(CATALOG)
    cliente = {
        "nombre": "Juan Pérez",
        "direccion": "Calle Falsa 123, CP 7600",
        "telefono": "011 5555-5555",
    }
    qty1, qty2 = random.choice([5, 10]), random.choice([3, 7])
    sub1 = qty1 * p1["price"]
    sub2 = qty2 * p2["price"]
    total_sin_iva = sub1 + sub2
    desc_porc, desc_ars = descuento_aplica(total_sin_iva)
    total_final = total_sin_iva - desc_ars

    items = [
        {"code": p1["code"], "name": p1["name"], "qty": qty1, "price_unit": p1["price"], "subtotal": sub1},
        {"code": p2["code"], "name": p2["name"], "qty": qty2, "price_unit": p2["price"], "subtotal": sub2},
    ]

    # Generamos un solo número de proforma y lo usamos en ambos lados
    numero_pf = numero_proforma()
    html = render_proforma_html(cliente, items, total_sin_iva, desc_ars, total_final, numero_pf)

    return {
        "conversations": [
            {"from": "human", "value": f"Necesito una proforma con {qty1} de {p1['name']} y {qty2} de {p2['name']}"},
            {"from": "gpt", "value": f"Te preparo la proforma con los productos solicitados. Total sin IVA: ${total_sin_iva:,}. ¿Te va?"},
            {"from": "human", "value": "Sí, enviamela"},
            {"from": "gpt", "value": f"✅ Proforma generada: {numero_pf}. Te la adjunto en PDF. Coordinás pago y envío al 011 4712-5151."},
        ],
        "extra": {"proforma_html": html}   # no se manda al modelo, solo log
    }

# ---------- MASTER GENERATOR ----------
SITUACIONES = [
    dialog_cambio_cantidad,
    dialog_arrepentimiento,
    dialog_urgencia,
    dialog_cambio_producto,
    dialog_consulta_tecnica,
    dialog_negociacion_precio,
    dialog_sin_stock,
    dialog_proforma_completa,
]

def generate_one():
    return random.choice(SITUACIONES)()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=OUTPUT_APPEND)
    parser.add_argument("--samples", type=int, default=SAMPLES_TO_ADD)
    parser.add_argument("--real-data", action="store_true", help="Usa datos reales de TERCOM")
    args = parser.parse_args()

    print(f"➕ Agregando {args.samples} diálogos reales a {args.output}")
    existing = []
    if os.path.exists(args.output):
        with open(args.output, "r", encoding="utf-8") as f:
            existing = [json.loads(line) for line in f]

    for i in range(args.samples):
        existing.append(generate_one())
        if (i + 1) % 5_000 == 0:
            print(f"  {i + 1:,} generados")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for dialog in existing:
            f.write(json.dumps(dialog, ensure_ascii=False) + "\n")

    print(f"✅ Dataset final: {len(existing):,} diálogos guardados en {args.output}")

if __name__ == "__main__":
    main()
