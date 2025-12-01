import os
from pathlib import Path

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("CATALOG_URL", "https://example.com/catalog.csv")
os.environ.setdefault("OPENAI_MODEL", "gpt-test")

import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import orquestar_fran


@pytest.mark.parametrize(
    "label, consulta",
    [
        ("busqueda_simple", "Tenés pastillas de freno para YBR 125?"),
        ("categoria_amplia", "Buscame amortiguadores para motos"),
        (
            "intencion_multiple",
            "Necesito una batería y un kit de transmisión para Honda Wave",
        ),
        (
            "dos_pedidos_conector",
            "Pasame cadena para CG150 y también necesito espejos universales",
        ),
        ("anio_incompleto", "Tenés un CDI para una XR? No sé el año."),
        ("producto_inventado", "Tenés carburador eléctrico para Honda Biz 125?"),
        ("pedido_vago", "Mostrame baterías"),
        ("con_error_ortografico", "Tenes amortiguadoress para una hondaa wave?"),
        ("varias_motos", "Buscame filtros para Tornado y para Titan 150"),
        (
            "modelo_inexistente",
            "Tenés pastillas de freno para Honda Wave 250?",
        ),
        (
            "referencia_previa",
            "La segunda batería que me pasaste, ¿la tenés en stock?",
        ),
        ("con_cantidad", "Pasame 3 juegos de pastillas para CG150"),
        ("comparacion", "Cuál es mejor para la Wave, la batería gel o AGM?"),
        ("intencion_carrito", "Agregame esa batería al carrito"),
        (
            "pedido_consecutivo",
            "Sí, listo. Ahora pasame una transmisión reforzada para YBR 125",
        ),
        (
            "contradiccion",
            "Necesito una batería sin ácido, pero que venga llena",
        ),
        ("social", "Buen día, cómo andás?"),
        (
            "pedido_largo",
            "Necesito una patada para CG125, una bobina para Tornado y un kit de arrastre para Biz 125",
        ),
        (
            "compatibilidad_cruzada",
            "El bulbo de freno que me pasaste, ¿sirve para YBR y CG también?",
        ),
        ("pedido_corto", "Amortiguador"),
    ],
)
def test_user_provided_prompts_are_handled(label, consulta):
    """Ensure the orchestrator returns a non-empty answer for diverse prompts."""

    # Each label gets a unique phone so contexts do not mix across parametrized runs.
    phone = f"+549110000{abs(hash(label)) % 10000:04d}"

    respuesta = orquestar_fran(consulta, phone)

    assert respuesta is not None
    assert isinstance(respuesta, str)
    assert len(respuesta) > 1
    assert "error" not in respuesta.lower()
    assert "traceback" not in respuesta.lower()
