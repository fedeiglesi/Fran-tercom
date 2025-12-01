from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))


def test_orquestar_fran_v317_fallback(monkeypatch):
    import app

    # Forzar recursos mínimos para evitar dependencias externas
    monkeypatch.setattr(app, "get_v317_resources", lambda: ([{"descripcion": "x", "search_text": "x"}], None, {}))
    monkeypatch.setattr(app, "save_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "log_interaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "log_performance", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "update_sales_phase_from_intent", lambda *args, **kwargs: None)

    def _raise(*args, **kwargs):
        raise RuntimeError("boom v317")

    monkeypatch.setattr(app, "orquestar_v317", _raise)
    monkeypatch.setattr(app, "orquestar_fran_v316", lambda msg, phone: f"fallback:{msg}")

    reply = app.orquestar_fran_v317("hola", "+54911")

    assert reply == "fallback:hola"
