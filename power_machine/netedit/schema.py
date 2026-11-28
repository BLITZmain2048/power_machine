"""Contratos de variantes y huellas canónicas, sin geometría en el hash eléctrico."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import uuid

VARIANT_SCHEMA = "power_machine-network-variant-v1"
LAYOUT_SCHEMA = "power_machine-variant-layout-v1"
GROUPS = ("buses", "branches", "branches3", "loads", "gens", "shunts", "switches")
PORTS = {
    "branches": ("bus_from", "bus_to"),
    "branches3": ("bus_hv", "bus_mv", "bus_lv"),
    "loads": ("bus",), "gens": ("bus",), "shunts": ("bus",),
    "switches": ("bus1", "bus2"), "buses": (),
}
GEOMETRY = frozenset(("layout", "geometry", "x_canvas", "y_canvas", "diagram_group",
                      "positions", "results", "result", "lf_results"))


def canonical_json(value):
    """JSON UTF-8 determinista. Rechaza NaN e infinito sin convertir ausentes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def value_sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def electrical_view(value):
    if isinstance(value, dict):
        return {key: electrical_view(item) for key, item in value.items()
                if key not in GEOMETRY}
    if isinstance(value, (list, tuple)):
        return [electrical_view(item) for item in value]
    return value


def model_sha256(model):
    return value_sha256(electrical_view(model))


def object_sha256(element):
    return value_sha256(electrical_view(element))


@dataclass(frozen=True)
class VariantCommand:
    op: str
    group: str
    object_id: str
    data: dict
    expected_sha256: str | None = None


@dataclass(frozen=True)
class NetworkVariant:
    schema: str
    variant_id: str
    name: str
    base_model_sha256: str
    revision: int
    commands: tuple[VariantCommand, ...] = ()
    layout: dict = field(default_factory=lambda: {"schema": LAYOUT_SCHEMA, "groups": {}})
    metadata: dict = field(default_factory=lambda: {"ref_comparison": "sin validar contra la referencia externa"})


@dataclass(frozen=True)
class VariantCheck:
    severity: str
    code: str
    object_id: str | None
    message: str


class VariantError(ValueError):
    """Fallo de transacción con diagnósticos conservados por identidad."""

    def __init__(self, checks):
        self.checks = tuple(checks)
        super().__init__(" | ".join(f"{c.code}: {c.message}" for c in self.checks))


def new_variant(base_model, name="Alternativa de red", variant_id=None):
    return NetworkVariant(VARIANT_SCHEMA, variant_id or "v_" + uuid.uuid4().hex,
                          name, model_sha256(base_model), 0)


def variant_to_dict(variant):
    result = asdict(variant)
    result["commands"] = list(result["commands"])
    return deepcopy(result)


def variant_from_dict(value):
    if not isinstance(value, dict) or value.get("schema") != VARIANT_SCHEMA:
        raise ValueError("Esquema de variante incompatible")
    if not isinstance(value.get("revision"), int) or isinstance(value["revision"], bool) or value["revision"] < 0:
        raise ValueError("Revisión de variante inválida")
    for key in ("variant_id", "name", "base_model_sha256"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError("Campo de variante ausente: " + key)
    layout = value.get("layout", {})
    if layout.get("schema") != LAYOUT_SCHEMA or not isinstance(layout.get("groups"), dict):
        raise ValueError("Esquema de geometría incompatible")
    commands = tuple(VariantCommand(**deepcopy(command)) for command in value.get("commands", []))
    canonical_json(value)
    return NetworkVariant(value["schema"], value["variant_id"], value["name"],
                          value["base_model_sha256"], value["revision"], commands,
                          deepcopy(layout), deepcopy(value.get("metadata", {})))


def variant_sha256(variant):
    return value_sha256(variant_to_dict(variant))


def element_ref(element):
    return {"object_id": element.get("id"), "relative_path": element.get("path"),
            "origin": element.get("provenance", {}).get("origin", element.get("origin", "external_reference"))}
