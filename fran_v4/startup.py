"""Inicialización de catálogo y esquema de productos para Fran 4.0."""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional

from fran_v4 import config
from fran_v4.database import Database
from fran_v4.search_engine import HybridSearchEngine

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"
)


def _parse_price(value: object | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    try:
        cleaned = str(value).replace(",", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def _resolve_catalog_path(explicit: Optional[str]) -> Optional[Path]:
    for candidate in (explicit, config.CATALOG_URL, str(DEFAULT_CATALOG_PATH)):
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path
    return None


def _load_catalog_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8") as handler:
        reader = csv.DictReader(handler)
        return [dict(row) for row in reader]


def _normalize_row(row: Dict[str, str]) -> Dict[str, object]:
    return {
        "codigo": row.get("codigo"),
        "nombre": row.get("descripcion") or row.get("descripcion_normalizada") or "",
        "descripcion": row.get("descripcion_normalizada") or row.get("descripcion") or "",
        "marca": row.get("marca_moto") or row.get("proveedor_nombre"),
        "categoria": row.get("categoria_final") or row.get("familia_nombre") or row.get("familia"),
        "precio_pesos": _parse_price(row.get("precio_pesos") or row.get("precio_dolares")),
        "sinonimos": row.get("sinonimos"),
        "familia": row.get("familia"),
        "familia_nombre": row.get("familia_nombre"),
    }


async def initialize_catalog(
    search_engine: HybridSearchEngine, database: Database, *, catalog_path: Optional[str] = None
) -> None:
    """Asegura el esquema vectorial y carga el catálogo CSV si no hay productos."""

    try:
        await search_engine.ensure_schema()
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("No se pudo asegurar el esquema de productos: %s", exc)
        return

    try:
        existing = await search_engine.count_products()
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("No se pudo contar productos existentes: %s", exc)
        return

    if existing:
        logger.info("Catálogo ya inicializado con %s productos, se omite carga inicial.", existing)
        return

    path = _resolve_catalog_path(catalog_path)
    if path is None:
        logger.warning("No se encontró un CSV de catálogo para cargar productos iniciales.")
        return

    raw_rows = _load_catalog_rows(path)
    if not raw_rows:
        logger.warning("El CSV de catálogo %s no contiene filas.", path)
        return

    normalized_rows = [_normalize_row(row) for row in raw_rows]
    payloads = await search_engine.prepare_catalog_payloads(normalized_rows)
    await search_engine.upsert_documents(payloads)

    if database.available:
        for row in normalized_rows:
            await database.upsert_product(
                {
                    "code": row.get("codigo"),
                    "name": row.get("nombre"),
                    "price_ars": row.get("precio_pesos"),
                    "family": row.get("categoria"),
                    "brand": row.get("marca"),
                    "metadata": row.get("sinonimos"),
                }
            )
    else:
        logger.warning("Base de datos SQLAlchemy no disponible; se omite carga auxiliar de catálogo.")

    logger.info("Se cargaron %s productos desde %s", len(payloads), path)
