"""Órdenes en español argentino -> instrucción canónica en inglés para el VLA y objetivo para la percepción.

Dos backends con la misma salida (`Order`):
  * RuleOrderParser: catálogo de alias/SKU (configs/language/catalogo_es_ar.yaml). Determinista, sin red.
  * LLMOrderParser: Claude (API de Anthropic) con salida JSON estructurada, para frases libres o nuevas.
    Si el LLM nombra un objeto que no está en el catálogo, la orden se marca como no ejecutable.
El VLA/percepción sólo ve `Order.instruction_en`; el texto original se guarda en el dataset (`task_es`).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from picking.utils.io import load_yaml, project_root

DEFAULT_CATALOG = project_root() / "configs" / "language" / "catalogo_es_ar.yaml"


@dataclass
class Order:
    raw_text: str
    object_id: str | None
    object_en: str | None
    color: str | None
    destination_id: str | None
    destination_en: str | None
    instruction_en: str | None
    executable: bool
    backend: str
    confidence: float
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class Catalog:
    def __init__(self, path: Path | str = DEFAULT_CATALOG):
        d = load_yaml(path)
        self.objects = d["objects"]
        self.destinations = d["destinations"]
        self.verbs_pick = [normalize(v) for v in d.get("verbs_pick", [])]
        self.verbs_place = [normalize(v) for v in d.get("verbs_place", [])]
        # alias index sorted longest-first so "palanca de freno" wins over "palanca"
        self.obj_aliases = sorted(((normalize(a), oid) for oid, o in self.objects.items() for a in [o["en"], *o.get("aliases", [])]), key=lambda t: -len(t[0]))
        self.skus = {normalize(str(s)): oid for oid, o in self.objects.items() for s in o.get("sku", [])}
        # numbered destinations ("bandeja 3") must win over the generic tray ("la bandeja")
        self.dest_aliases = sorted(((normalize(a), did) for did, o in self.destinations.items() for a in [o["en"], *o.get("aliases", [])]), key=lambda t: (-int(any(c.isdigit() for c in t[0])), -len(t[0])))

    def instruction(self, object_id: str, destination_id: str | None) -> str:
        dest = self.destinations[destination_id or "tray"]["en"]
        return f"Pick the {self.objects[object_id]['en']} and place it in {dest}."


class RuleOrderParser:
    name = "rules"

    def __init__(self, catalog: Catalog | None = None):
        self.cat = catalog or Catalog()

    def parse(self, text: str) -> Order:
        t = normalize(text)
        padded = f" {t} "
        obj = None
        conf = 0.0
        m = re.search(r"\bsku\s*([a-z0-9-]+)", t)
        if m and m.group(1) in self.cat.skus:
            obj, conf = self.cat.skus[m.group(1)], 1.0
        if obj is None:
            for alias, oid in self.cat.obj_aliases:
                if f" {alias} " in padded:
                    obj, conf = oid, (0.9 if " " in alias else 0.7)
                    break
        dest = None
        for alias, did in self.cat.dest_aliases:
            if f" {alias} " in padded:
                dest = did
                break
        has_pick_verb = any(f" {v} " in padded for v in self.cat.verbs_pick)
        if obj is None:
            return Order(text, None, None, None, dest, None, None, False, self.name, 0.0, "objeto no reconocido: agregar alias al catálogo o usar el backend LLM")
        o = self.cat.objects[obj]
        return Order(text, obj, o["en"], o.get("color"), dest or "tray", self.cat.destinations[dest or "tray"]["en"], self.cat.instruction(obj, dest), True, self.name, min(1.0, conf + (0.1 if has_pick_verb else 0.0)))


class LLMOrderParser:
    """Claude maps a free-form Argentine Spanish order onto the catalogue (structured JSON output).
    Requires `pip install anthropic` and ANTHROPIC_API_KEY (or `ant auth login`)."""

    name = "llm"

    def __init__(self, catalog: Catalog | None = None, model: str = "claude-opus-5-5"):
        self.cat = catalog or Catalog()
        self.model = model
        self.rules = RuleOrderParser(self.cat)

    def parse(self, text: str) -> Order:
        import anthropic  # lazy: optional dependency

        client = anthropic.Anthropic()
        obj_list = "\n".join(f"- {oid}: {o['en']} (es: {', '.join(o.get('aliases', []))}; sku: {', '.join(map(str, o.get('sku', [])))})" for oid, o in self.cat.objects.items())
        dest_list = "\n".join(f"- {did}: {o['en']} (es: {', '.join(o.get('aliases', []))})" for did, o in self.cat.destinations.items())
        schema = {
            "type": "object",
            "properties": {
                "object_id": {"type": ["string", "null"], "enum": [*self.cat.objects.keys(), None]},
                "destination_id": {"type": ["string", "null"], "enum": [*self.cat.destinations.keys(), None]},
                "confidence": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["object_id", "destination_id", "confidence", "notes"],
            "additionalProperties": False,
        }
        response = client.messages.create(
            model=self.model,
            max_tokens=512,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            system=(
                "Sos el intérprete de órdenes de un robot de picking de motopartes en un depósito argentino. "
                "Mapeá la orden del operario (español rioplatense, con voseo y jerga) a UN objeto y UN destino del catálogo. "
                "Si el objeto no está en el catálogo, object_id = null y explicalo en notes. Si no se menciona destino, destination_id = null.\n"
                f"Objetos:\n{obj_list}\nDestinos:\n{dest_list}"
            ),
            messages=[{"role": "user", "content": text}],
        )
        if response.stop_reason == "refusal":
            return Order(text, None, None, None, None, None, None, False, self.name, 0.0, "el modelo rechazó la orden")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        oid, did = data.get("object_id"), data.get("destination_id")
        if oid not in self.cat.objects:
            return Order(text, None, None, None, did, None, None, False, self.name, 0.0, data.get("notes", "objeto fuera de catálogo"))
        o = self.cat.objects[oid]
        did = did if did in self.cat.destinations else "tray"
        return Order(text, oid, o["en"], o.get("color"), did, self.cat.destinations[did]["en"], self.cat.instruction(oid, did), True, self.name, float(data.get("confidence", 0.5)), data.get("notes", ""))


def parse_order(text: str, backend: str = "auto", catalog: Catalog | None = None) -> Order:
    """auto: rules first; if the rules cannot identify the object, fall back to the LLM when available."""
    cat = catalog or Catalog()
    order = RuleOrderParser(cat).parse(text)
    if order.executable or backend == "rules":
        return order
    try:
        return LLMOrderParser(cat).parse(text)
    except Exception as e:  # noqa: BLE001 - no API key / no package / network: keep the rules verdict
        order.notes += f" | LLM no disponible: {type(e).__name__}"
        return order
