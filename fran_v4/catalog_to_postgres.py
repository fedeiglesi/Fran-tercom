"""Carga el catálogo desde un CSV o URL remota a PostgreSQL en una tabla nueva.

El script crea una tabla llamada ``catalogo3`` (por defecto) tomando los
encabezados del CSV como columnas. Los campos ``precio_pesos`` y
``precio_dolares`` se convierten a valores numéricos y quedan en ``NULL``
cuando el CSV no trae valor. El resto de las columnas se cargan como texto.

Uso local con archivo CSV:
    python -m fran_v4.catalog_to_postgres /ruta/al/catalogo.csv \
        --table-name catalogo3 --drop-existing

Uso directo con una URL (por ejemplo, GitHub raw):
    python -m fran_v4.catalog_to_postgres \
        "https://raw.githubusercontent.com/usuario/repo/ruta/catalogo.csv" \
        --table-name catalogo3 --drop-existing
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator, List, Mapping
from contextlib import contextmanager
from urllib.parse import urlparse
from urllib.request import urlopen

from sqlalchemy import Column, MetaData, Numeric, String, Table, Text, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from fran_v4 import config

PRICE_COLUMNS = {"precio_pesos", "precio_dolares"}
CHUNK_SIZE = 500
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"
)


def _parse_decimal(value: str | None) -> Decimal | None:
    """Convierte precios del CSV a ``Decimal`` o ``None`` si están vacíos."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace(",", "")  # permite valores con separador de miles
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _sanitize_row(row: Mapping[str, str]) -> Mapping[str, object]:
    """Normaliza un registro del CSV para que sea compatible con SQLAlchemy."""
    normalized = {}
    for key, value in row.items():
        if key in PRICE_COLUMNS:
            normalized[key] = _parse_decimal(value)
        else:
            normalized[key] = value.strip() if isinstance(value, str) else value
    return normalized


def _is_url(path: str) -> bool:
    parsed = urlparse(path)
    return parsed.scheme in {"http", "https"}


def _download_csv(url: str) -> Path:
    """Descarga el CSV remoto a un archivo temporal y devuelve su ruta."""
    with urlopen(url) as response, tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        tmp.write(response.read())
        return Path(tmp.name)


@contextmanager
def _resolve_csv_source(csv_path: str) -> Iterator[Path]:
    """Devuelve una ruta local al CSV, descargándolo si es una URL."""
    expanded_path = os.path.expandvars(csv_path)
    if expanded_path.startswith("$"):
        fallback = DEFAULT_CATALOG_PATH
        if fallback.exists():
            print(
                "No se encontró la variable de entorno para el CSV; "
                f"se usará el archivo local {fallback}"
            )
            expanded_path = str(fallback)
        else:
            raise FileNotFoundError(
                "No se encontró la ruta al CSV porque la variable de entorno no está definida"
            )

    csv_path = expanded_path

    if _is_url(csv_path):
        tmp_path = _download_csv(csv_path)
        try:
            yield tmp_path
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"No se encontró el archivo CSV: {csv_path}")
        yield path


def _build_table(headers: Iterable[str], table_name: str) -> Table:
    """Genera dinámicamente la tabla destino usando los encabezados del CSV."""
    metadata = MetaData()
    columns: List[Column] = []
    for header in headers:
        if header == "codigo":
            columns.append(Column(header, String(128), primary_key=True))
        elif header in PRICE_COLUMNS:
            columns.append(Column(header, Numeric(14, 2), nullable=True))
        else:
            columns.append(Column(header, Text, nullable=True))
    return Table(table_name, metadata, *columns)


async def _create_table(engine: AsyncEngine, table: Table, drop_existing: bool) -> None:
    """Crea la tabla en base a los metadatos generados."""
    async with engine.begin() as conn:
        if drop_existing:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{table.name}"'))
        await conn.run_sync(table.metadata.create_all)


async def _bulk_insert(engine: AsyncEngine, table: Table, rows: List[Mapping[str, object]]) -> None:
    """Inserta filas en bloques para evitar statements demasiado grandes."""
    for start in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[start : start + CHUNK_SIZE]
        async with AsyncSession(engine) as session:
            await session.execute(insert(table), chunk)
            await session.commit()


def _to_async_database_url(url: str) -> str:
    """Normaliza la URL de base de datos para que use el dialecto asyncpg."""

    # Railway suele exponer ``postgres://`` o ``postgresql://``.
    # SQLAlchemy async requiere ``postgresql+asyncpg://`` para funcionar.
    normalized = url.replace("postgres://", "postgresql://", 1)
    if "+asyncpg" not in normalized.partition("://")[0]:
        normalized = normalized.replace("postgresql://", "postgresql+asyncpg://", 1)
    return normalized


async def _run_with_retries(coro_factory, *, label: str) -> None:
    """Ejecuta una operación con reintentos y backoff."""

    attempt = 0
    max_retries = config.DB_INIT_MAX_RETRIES
    delay = config.DB_INIT_BASE_DELAY
    while True:
        try:
            await coro_factory()
            return
        except Exception as exc:  # pragma: no cover - logging solamente
            attempt += 1
            if max_retries is not None and attempt > max_retries:
                raise

            wait_time = min(delay, config.DB_INIT_MAX_DELAY)
            print(
                f"[{label}] fallo intento {attempt}: {exc}. Reintentando en {wait_time} s..."
            )
            await asyncio.sleep(wait_time)
            delay *= 2


async def ingest_catalog(
    csv_path: str, *, table_name: str = "catalogo3", drop_existing: bool = False
) -> None:
    """Lee el CSV y lo vuelca en PostgreSQL dentro de ``table_name``."""
    with _resolve_csv_source(csv_path) as path:
        with path.open("r", encoding="utf-8") as handler:
            reader = csv.DictReader(handler)
            headers = reader.fieldnames or []
            if not headers:
                raise ValueError("El CSV no tiene encabezados válidos")
            sanitized_rows = [_sanitize_row(row) for row in reader]

    database_url = _to_async_database_url(config.DATABASE_URL)
    engine = create_async_engine(database_url, echo=False, future=True)
    table = _build_table(headers, table_name)

    await _run_with_retries(
        lambda: _create_table(engine, table, drop_existing), label="creacion_tabla"
    )
    await _run_with_retries(lambda: _bulk_insert(engine, table, sanitized_rows), label="insercion_catalogo")
    await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Carga el catálogo CSV en PostgreSQL.")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=config.CATALOG_URL or str(DEFAULT_CATALOG_PATH),
        help=(
            "Ruta al CSV del catálogo (por defecto usa la variable de entorno CATALOG_URL "
            "o el archivo local incluido en el proyecto)"
        ),
    )
    parser.add_argument(
        "--table-name",
        default="catalogo3",
        help="Nombre de la tabla destino (por defecto catalogo3)",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Si se pasa, borra la tabla antes de recrearla",
    )
    return parser.parse_args()


def _main() -> None:
    args = _parse_args()
    if not args.csv_path:
        raise SystemExit(
            "Debes proporcionar la ruta del CSV o definir la variable de entorno CATALOG_URL"
        )
    asyncio.run(
        ingest_catalog(args.csv_path, table_name=args.table_name, drop_existing=args.drop_existing)
    )


if __name__ == "__main__":
    _main()
