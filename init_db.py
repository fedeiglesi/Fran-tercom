import os
import csv
import psycopg2
import requests
from io import StringIO

DATABASE_URL = os.environ["DATABASE_URL"]
CSV_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/Fran-4.0/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"
TABLE_NAME = "catalogo"

def to_numeric(value):
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    return float(value)

print("Descargando CSV...")
response = requests.get(CSV_URL)
response.raise_for_status()
csv_data = response.text

print("Conectando a PostgreSQL...")
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("Creando tabla...")
cur.execute(f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    codigo TEXT PRIMARY KEY,
    descripcion TEXT,
    precio_dolares NUMERIC,
    precio_pesos NUMERIC,
    familia TEXT,
    familia_nombre TEXT,
    proveedor_nombre TEXT,
    marca_moto TEXT,
    modelo_moto TEXT,
    descripcion_normalizada TEXT,
    sinonimos TEXT,
    cilindrada TEXT,
    categoria_final TEXT
);
""")
conn.commit()

print("Importando datos...")
f = StringIO(csv_data)
reader = csv.DictReader(f)

insert_query = f"""
INSERT INTO {TABLE_NAME} (
    codigo, descripcion, precio_dolares, precio_pesos,
    familia, familia_nombre, proveedor_nombre,
    marca_moto, modelo_moto,
    descripcion_normalizada, sinonimos,
    cilindrada, categoria_final
)
VALUES (
    %(codigo)s, %(descripcion)s, %(precio_dolares)s, %(precio_pesos)s,
    %(familia)s, %(familia_nombre)s, %(proveedor_nombre)s,
    %(marca_moto)s, %(modelo_moto)s,
    %(descripcion_normalizada)s, %(sinonimos)s,
    %(cilindrada)s, %(categoria_final)s
)
ON CONFLICT (codigo) DO NOTHING;
"""

for row in reader:
    for k in row:
        if row[k] == "":
            row[k] = None

    row["precio_dolares"] = to_numeric(row["precio_dolares"])
    row["precio_pesos"] = to_numeric(row["precio_pesos"])

    cur.execute(insert_query, row)

conn.commit()
print("✅ Base inicializada correctamente")

cur.close()
conn.close()
