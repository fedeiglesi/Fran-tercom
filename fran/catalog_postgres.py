"""
Módulo para cargar el catálogo desde PostgreSQL con fallback a CSV.

Variables de entorno:
    DATABASE_URL - URL de conexión PostgreSQL (Railway, etc.)
    USE_POSTGRES_CATALOG - "true" para habilitar PostgreSQL (default: false)

Uso:
    from fran.catalog_postgres import load_catalog_from_postgres

    catalog = load_catalog_from_postgres()
    if catalog is None:
        # Fallback a CSV
        catalog = load_catalog_enriched_from_csv()
"""

import logging
import os
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

# Intentar importar psycopg2, puede no estar disponible
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    logger.debug("psycopg2 no disponible - PostgreSQL deshabilitado")


def is_postgres_enabled() -> bool:
    """Verifica si PostgreSQL está habilitado y configurado."""
    if not PSYCOPG2_AVAILABLE:
        return False

    use_pg = os.getenv("USE_POSTGRES_CATALOG", "false").lower()
    if use_pg not in ("true", "1", "yes"):
        return False

    database_url = os.getenv("DATABASE_URL")
    return bool(database_url)


def get_postgres_connection():
    """Obtiene una conexión a PostgreSQL."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL no configurada")

    return psycopg2.connect(database_url)


def load_catalog_from_postgres(
    exchange_rate: Optional[Decimal] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Carga el catálogo desde PostgreSQL.

    Args:
        exchange_rate: Tasa de cambio USD->ARS para calcular precios faltantes

    Returns:
        Lista de productos en formato compatible con load_catalog_enriched(),
        o None si PostgreSQL no está disponible/configurado.
    """
    if not is_postgres_enabled():
        logger.debug("PostgreSQL no habilitado para catálogo")
        return None

    try:
        conn = get_postgres_connection()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    codigo,
                    descripcion,
                    precio_dolares,
                    precio_pesos,
                    familia,
                    familia_nombre,
                    proveedor_nombre,
                    marca_moto,
                    modelo_moto,
                    descripcion_normalizada,
                    sinonimos,
                    cilindrada,
                    categoria_final
                FROM catalogo
                ORDER BY codigo
            """)

            rows = cur.fetchall()

        conn.close()

        if not rows:
            logger.warning("PostgreSQL: tabla 'catalogo' vacía")
            return None

        # Convertir a formato compatible con load_catalog_enriched()
        catalog = []
        exchange = exchange_rate or Decimal("1200")  # Default exchange rate

        for row in rows:
            try:
                code = (row.get("codigo") or "").strip()
                descripcion = (row.get("descripcion") or "").strip()
                descripcion_normalizada = (row.get("descripcion_normalizada") or "").strip()

                price_usd = Decimal(str(row.get("precio_dolares") or 0))
                price_ars = Decimal(str(row.get("precio_pesos") or 0))

                # Calcular precio ARS si falta
                if price_ars == 0 and price_usd > 0:
                    price_ars = (price_usd * exchange).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )

                marca_moto = (row.get("marca_moto") or "").strip()
                modelo_moto = (row.get("modelo_moto") or "").strip()
                categoria_final = (row.get("categoria_final") or "").strip()
                familia = (row.get("familia") or "").strip()
                familia_nombre = (row.get("familia_nombre") or "").strip()
                proveedor_nombre = (row.get("proveedor_nombre") or "").strip()
                sinonimos = (row.get("sinonimos") or "").strip()
                cilindrada = (row.get("cilindrada") or "").strip()

                # Construir search_text igual que en load_catalog_enriched
                name = descripcion_normalizada or descripcion
                search_text_parts = [
                    name,
                    f"familia {familia_nombre}" if familia_nombre else "",
                    f"marca {marca_moto}" if marca_moto else "",
                    f"modelo {modelo_moto}" if modelo_moto else "",
                    f"categoria {categoria_final}" if categoria_final else "",
                    f"marca moto {marca_moto}" if marca_moto else "",
                    f"modelo moto {modelo_moto}" if modelo_moto else "",
                    f"categoria final {categoria_final}" if categoria_final else "",
                    f"palabras clave {sinonimos}" if sinonimos else "",
                    f"proveedor {proveedor_nombre}" if proveedor_nombre else "",
                    f"cilindrada {cilindrada}" if cilindrada else "",
                ]
                search_text = " ".join([p for p in search_text_parts if p]).strip()

                if not name and not search_text:
                    continue

                catalog.append({
                    "code": code,
                    "name": name,
                    "raw_name": descripcion,
                    "price_usd": float(price_usd),
                    "price_ars": float(price_ars),
                    "brand": marca_moto,
                    "moto_brand": marca_moto,
                    "model": modelo_moto,
                    "moto_model": modelo_moto,
                    "category": categoria_final,
                    "final_category": categoria_final,
                    "keywords": sinonimos,
                    "oem": "",
                    "alt_names": "",
                    "vehicle_type": "",
                    "family_name": familia_nombre,
                    "family_code": familia,
                    "provider_name": proveedor_nombre,
                    "displacement": cilindrada,
                    "search_text": search_text or name,
                    # Campos adicionales para v3.17
                    "descripcion": descripcion,
                    "descripcion_normalizada": descripcion_normalizada,
                    "categoria": categoria_final,
                    "cilindrada": cilindrada,
                })

            except Exception as e:
                logger.warning(f"Error procesando fila PostgreSQL: {e}")
                continue

        logger.info(f"Catálogo cargado desde PostgreSQL: {len(catalog)} productos")
        return catalog

    except Exception as e:
        logger.error(f"Error cargando catálogo desde PostgreSQL: {e}")
        return None


def get_catalog_stats() -> Optional[Dict[str, Any]]:
    """Obtiene estadísticas del catálogo en PostgreSQL."""
    if not is_postgres_enabled():
        return None

    try:
        conn = get_postgres_connection()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(precio_dolares) as con_precio_usd,
                    COUNT(precio_pesos) as con_precio_ars,
                    COUNT(DISTINCT marca_moto) as marcas,
                    COUNT(DISTINCT categoria_final) as categorias,
                    MAX(updated_at) as ultima_actualizacion
                FROM catalogo
            """)
            stats = cur.fetchone()

        conn.close()
        return dict(stats) if stats else None

    except Exception as e:
        logger.error(f"Error obteniendo estadísticas: {e}")
        return None


def search_in_postgres(
    query: str,
    limit: int = 20,
    marca: Optional[str] = None,
    modelo: Optional[str] = None,
    categoria: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Búsqueda directa en PostgreSQL usando full-text search.

    Esta función es opcional y puede usarse para búsquedas simples
    sin necesidad de FAISS/BM25.
    """
    if not is_postgres_enabled():
        return []

    try:
        conn = get_postgres_connection()

        conditions = ["to_tsvector('spanish', COALESCE(descripcion_normalizada, '')) @@ plainto_tsquery('spanish', %s)"]
        params = [query]

        if marca:
            conditions.append("LOWER(marca_moto) = LOWER(%s)")
            params.append(marca)

        if modelo:
            conditions.append("LOWER(modelo_moto) LIKE LOWER(%s)")
            params.append(f"%{modelo}%")

        if categoria:
            conditions.append("LOWER(categoria_final) = LOWER(%s)")
            params.append(categoria)

        params.append(limit)

        sql = f"""
            SELECT *,
                ts_rank(to_tsvector('spanish', COALESCE(descripcion_normalizada, '')),
                        plainto_tsquery('spanish', %s)) as rank
            FROM catalogo
            WHERE {' AND '.join(conditions)}
            ORDER BY rank DESC
            LIMIT %s
        """

        # Agregar query al principio para ts_rank
        params = [query] + params

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            results = cur.fetchall()

        conn.close()
        return [dict(r) for r in results]

    except Exception as e:
        logger.error(f"Error en búsqueda PostgreSQL: {e}")
        return []
