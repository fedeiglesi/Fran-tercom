"""Sincroniza el catálogo CSV alojado en GitHub con Postgres en Railway.

Características principales:
- Valida la estructura del CSV y permite configurar la URL vía variable de
  entorno `CATALOGO_CSV_URL`.
- Usa *upserts* en lotes configurables (`BATCH_SIZE`) contra la tabla
  `catalogo1125` o la definida en `CATALOGO_TABLE`.
- Comprueba que `DATABASE_URL` exista y sea un DSN de Postgres válido.
"""
from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass
from io import StringIO
from itertools import islice
from typing import Iterable, Iterator, List
from urllib.parse import urlparse

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_batch
import requests

# Columnas esperadas para asegurar consistencia con la tabla de destino.
EXPECTED_COLUMNS = [
    "codigo",
    "descripcion",
    "precio_dolares",
    "precio_pesos",
    "familia",
    "familia_nombre",
    "proveedor_nombre",
    "marca_moto",
    "modelo_moto",
    "descripcion_normalizada",
    "sinonimos",
    "cilindrada",
    "categoria_final",
]


@dataclass
class CatalogSyncConfig:
    """Configuración de la sincronización desde entorno o valores por defecto."""

    database_url: str
    csv_url: str
    table_name: str = "catalogo1125"
    batch_size: int = 500

    @classmethod
    def from_env(cls) -> "CatalogSyncConfig":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL no está definida en Railway")

        csv_url = os.getenv(
            "CATALOGO_CSV_URL",
            (
                "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/Fran-3.17/"
                "catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"
            ),
        )

        table_name = os.getenv("CATALOGO_TABLE", "catalogo1125")
        batch_size_str = os.getenv("BATCH_SIZE", "500")

        try:
            batch_size = max(1, int(batch_size_str))
        except ValueError as exc:
            raise ValueError("BATCH_SIZE debe ser un entero positivo") from exc

        return cls(
            database_url=database_url,
            csv_url=csv_url,
            table_name=table_name,
            batch_size=batch_size,
        )


def validate_database_url(database_url: str) -> None:
    """Asegura que el DSN tenga esquema de Postgres y host definido."""

    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("DATABASE_URL debe ser un DSN de Postgres válido")


def chunked(iterable: Iterable[dict], size: int) -> Iterator[List[dict]]:
    """Agrupa el iterable en listas de tamaño `size` para inserts batched."""

    iterator = iter(iterable)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            return
        yield batch


def clean_row(row: dict) -> dict:
    """Normaliza celdas vacías a `None` y conserva solo las columnas esperadas."""

    return {column: (row.get(column) or None) for column in EXPECTED_COLUMNS}


def fetch_csv(url: str) -> csv.DictReader:
    """Descarga el CSV y retorna un DictReader validado."""

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    response.encoding = response.encoding or "utf-8"
    csvfile = StringIO(response.text)
    reader = csv.DictReader(csvfile)

    missing_columns = [c for c in EXPECTED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_columns:
        raise ValueError(f"Columnas faltantes en el CSV: {missing_columns}")

    return reader


def build_upsert_query(table_name: str) -> sql.Composed:
    """Crea la sentencia de upsert con el nombre de tabla indicado."""

    return sql.SQL(
        """
        INSERT INTO {table} (
            codigo, descripcion, precio_dolares, precio_pesos,
            familia, familia_nombre, proveedor_nombre,
            marca_moto, modelo_moto, descripcion_normalizada,
            sinonimos, cilindrada, categoria_final
        )
        VALUES (
            %(codigo)s, %(descripcion)s, %(precio_dolares)s, %(precio_pesos)s,
            %(familia)s, %(familia_nombre)s, %(proveedor_nombre)s,
            %(marca_moto)s, %(modelo_moto)s, %(descripcion_normalizada)s,
            %(sinonimos)s, %(cilindrada)s, %(categoria_final)s
        )
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
        """
    ).format(table=sql.Identifier(table_name))


def main() -> None:
    config = CatalogSyncConfig.from_env()
    validate_database_url(config.database_url)

    reader = fetch_csv(config.csv_url)
    upsert_query = build_upsert_query(config.table_name)

    total_rows = 0
    with psycopg2.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            for batch in chunked(reader, size=config.batch_size):
                cleaned_batch = [clean_row(row) for row in batch]
                execute_batch(cur, upsert_query, cleaned_batch, page_size=config.batch_size)
                total_rows += len(cleaned_batch)
            conn.commit()

    print(
        "✔ Catálogo %s actualizado correctamente desde GitHub (%d filas procesadas)."
        % (config.table_name, total_rows)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - script de operación manual
        print(f"✖ Error al sincronizar catálogo: {exc}", file=sys.stderr)
        raise
