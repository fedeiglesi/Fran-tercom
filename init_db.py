import os
import csv
import psycopg2
import requests
import time
from io import StringIO

DATABASE_URL = os.environ["DATABASE_URL"].strip()
CSV_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/Fran-4.0/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"
TABLE_NAME = "catalogo"

def to_numeric(value):
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    return float(value)

def connect_with_retry(max_attempts=15, delay=5):
    """Intenta conectar a la base de datos con reintentos"""
    for attempt in range(max_attempts):
        try:
            print(f"🔄 Intento de conexión {attempt + 1}/{max_attempts}...")
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            print("✅ Conexión exitosa a PostgreSQL")
            return conn
        except psycopg2.OperationalError as e:
            if attempt < max_attempts - 1:
                print(f"❌ Fallo en conexión: {str(e)[:100]}")
                print(f"⏳ Reintentando en {delay}s...")
                time.sleep(delay)
            else:
                print(f"💥 Error final después de {max_attempts} intentos")
                raise e

print("📥 Descargando CSV...")
response = requests.get(CSV_URL)
response.raise_for_status()
csv_data = response.text
print(f"✅ CSV descargado ({len(csv_data)} bytes)")

print("🔌 Conectando a PostgreSQL...")
conn = connect_with_retry()
cur = conn.cursor()

# Verificar si ya hay datos
try:
    cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
    row_count = cur.fetchone()[0]
    if row_count > 0:
        print(f"✅ Tabla ya tiene {row_count} registros. Saltando inicialización.")
        cur.close()
        conn.close()
        exit(0)
except psycopg2.Error:
    print("ℹ️  Tabla no existe aún, procediendo con creación...")

print("🏗️  Creando tabla...")
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
print("✅ Tabla creada")

print("📊 Importando datos...")
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

count = 0
for row in reader:
    for k in row:
        if row[k] == "":
            row[k] = None
    row["precio_dolares"] = to_numeric(row["precio_dolares"])
    row["precio_pesos"] = to_numeric(row["precio_pesos"])
    cur.execute(insert_query, row)
    count += 1
    if count % 1000 == 0:
        print(f"  ↳ Importados {count} registros...")

conn.commit()
print(f"✅ Base inicializada correctamente con {count} registros")
cur.close()
conn.close()
