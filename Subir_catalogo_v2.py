import csv
import os
import urllib.parse
import psycopg2
from psycopg2.extras import execute_values
import requests
from io import StringIO

def build_database_url(verbose=True):
    """Return a Postgres connection string from env vars."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        if verbose:
            print("🔌 Usando DATABASE_URL")
        return database_url

    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    host = os.getenv("POSTGRES_HOST")
    dbname = os.getenv("POSTGRES_DB")
    port = os.getenv("POSTGRES_PORT", "5432")

    if all([user, password, host, dbname]):
        if verbose:
            print(f"🔌 Conectando a {host}:{port}/{dbname}")
        safe_password = urllib.parse.quote_plus(password)
        return f"postgresql://{user}:{safe_password}@{host}:{port}/{dbname}"

    missing = [v for v in ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_DB"] if not os.getenv(v)]
    raise Exception(f"Faltan variables de conexión: {', '.join(missing)}")


def parse_decimal(value):
    """Convierte string a decimal o None si está vacío."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def migrate_catalog():
    CSV_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/Fran-3.17/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"

    print("📥 Descargando catálogo desde GitHub...")
    response = requests.get(CSV_URL, timeout=30)
    response.raise_for_status()

    reader = csv.DictReader(StringIO(response.text))

    # Preparar datos con conversión de tipos
    rows = []
    for row in reader:
        rows.append((
            row.get("codigo", "").strip(),
            row.get("descripcion", "").strip() or None,
            parse_decimal(row.get("precio_dolares")),
            parse_decimal(row.get("precio_pesos")),
            row.get("familia", "").strip() or None,
            row.get("familia_nombre", "").strip() or None,
            row.get("proveedor_nombre", "").strip() or None,
            row.get("marca_moto", "").strip() or None,
            row.get("modelo_moto", "").strip() or None,
            row.get("descripcion_normalizada", "").strip() or None,
            row.get("sinonimos", "").strip() or None,
            row.get("cilindrada", "").strip() or None,
            row.get("categoria_final", "").strip() or None,
        ))

    print(f"📊 {len(rows)} productos parseados")

    conn = psycopg2.connect(build_database_url())

    try:
        with conn.cursor() as cur:
            # Crear tabla
            cur.execute("""
                DROP TABLE IF EXISTS products CASCADE;
                CREATE TABLE IF NOT EXISTS products (
                    codigo TEXT PRIMARY KEY,
                    descripcion TEXT,
                    precio_dolares NUMERIC(12,2),
                    precio_pesos NUMERIC(12,2),
                    familia TEXT,
                    familia_nombre TEXT,
                    proveedor_nombre TEXT,
                    marca_moto TEXT,
                    modelo_moto TEXT,
                    descripcion_normalizada TEXT,
                    sinonimos TEXT,
                    cilindrada TEXT,
                    categoria_final TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)

            # Crear índices para búsqueda eficiente
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_products_marca ON products(marca_moto);
                CREATE INDEX IF NOT EXISTS idx_products_modelo ON products(modelo_moto);
                CREATE INDEX IF NOT EXISTS idx_products_categoria ON products(categoria_final);
                CREATE INDEX IF NOT EXISTS idx_products_familia ON products(familia);
                CREATE INDEX IF NOT EXISTS idx_products_descripcion_gin ON products
                    USING gin(to_tsvector('spanish', COALESCE(descripcion_normalizada, '')));
            """)

            # Bulk upsert con execute_values (mucho más rápido)
            execute_values(
                cur,
                """
                INSERT INTO products (
                    codigo, descripcion, precio_dolares, precio_pesos,
                    familia, familia_nombre, proveedor_nombre,
                    marca_moto, modelo_moto, descripcion_normalizada,
                    sinonimos, cilindrada, categoria_final
                ) VALUES %s
                ON CONFLICT (codigo) DO UPDATE SET
                    descripcion = EXCLUDED.descripcion,
                    precio_dolares = EXCLUDED.precio_dolares,
                    precio_pesos = EXCLUDED.precio_pesos,
                    familia = EXCLUDED.familia,
                    familia_nombre = EXCLUDED.familia_nombre,
                    proveedor_nombre = EXCLUDED.proveedor_nombre,
                    marca_moto = EXCLUDED.marca_moto,
                    modelo_moto = EXCLUDED.modelo_moto,
                    descripcion_normalizada = EXCLUDED.descripcion_normalizada,
                    sinonimos = EXCLUDED.sinonimos,
                    cilindrada = EXCLUDED.cilindrada,
                    categoria_final = EXCLUDED.categoria_final;
                """,
                rows,
                page_size=500
            )

            # Verificar conteo
            cur.execute("SELECT COUNT(*) FROM products")
            count = cur.fetchone()[0]

        conn.commit()
        print(f"✅ Migración completada: {count} productos en la tabla 'products'")

    except Exception as e:
        conn.rollback()
        print(f"❌ Error durante la migración: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_catalog()
