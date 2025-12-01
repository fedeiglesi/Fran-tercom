"""Herramienta de diagnóstico rápido para Fran 3.17.

Ejecuta validaciones básicas de catálogo, centroid y routing sin
necesidad de levantar el servidor completo.
"""

from pprint import pprint

from app import get_v317_resources
from pipeline.router_dynamic import router_fase0_dynamic


SAMPLE_MESSAGES = [
    "filtro aceite",
    "bujia honda",
    "pastillas freno",
    "hola",
]


def main() -> None:
    print("[Fran][Diag] Cargando recursos v3.17...")
    catalog, centroid, schema = get_v317_resources()

    if not catalog:
        print("[Fran][Diag] ❌ Catálogo no disponible. Revisa logs y variable CATALOG_URL.")
        return

    print(f"[Fran][Diag] ✅ Catálogo cargado: {len(catalog)} productos")
    print(f"[Fran][Diag] Centroid disponible: {bool(centroid)}")
    print(f"[Fran][Diag] Schema listo: {schema is not None}")

    print("\n[Fran][Diag] Test rápido de router (fallback incluido):")
    for message in SAMPLE_MESSAGES:
        routed = router_fase0_dynamic(message, catalog_centroid=centroid)
        print(f"  - '{message}' -> {routed['route']} | score={routed['score']:.2f} | reason={routed.get('reason')} | fallback={routed.get('fallback', False)}")

    print("\n[Fran][Diag] Ejemplo de schema del clasificador:")
    pprint({k: v for k, v in (schema or {}).items() if k in {"type", "required", "properties"}})


if __name__ == "__main__":
    main()
