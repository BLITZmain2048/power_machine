"""Aplicación ordenada de comandos sobre copias privadas del inventario."""

from copy import deepcopy
from dataclasses import replace
import math
import uuid

from .schema import (GROUPS, PORTS, VariantCheck, VariantCommand, VariantError,
                     object_sha256)

CLASSES = {"buses": ("ElmTerm",), "branches": ("ElmLne", "ElmTr2", "ElmZpu"),
           "branches3": ("ElmTr3",), "loads": ("ElmLod",),
           "gens": ("ElmSym", "ElmGenstat", "ElmPvsys", "ElmXnet"), "shunts": ("ElmShnt",),
           "switches": ("ElmCoup", "StaSwitch")}
IDENTITY = frozenset(("id", "path", "object_id", "relative_path", "class", "provenance"))


def index_model(model):
    return {item["id"]: (group, item) for group in GROUPS
            for item in model.get(group, [])}


def port_field(group, element, port):
    """Puerto PF explícito a campo real. No se resuelve por nombre visible."""
    aliases = {"bus1": "bus", "bus2": "bus"} if group in ("loads", "gens", "shunts") else {}
    if group == "branches":
        aliases = ({"bushv": "bus_from", "buslv": "bus_to"} if element["class"] == "ElmTr2"
                   else {"bus1": "bus_from", "bus2": "bus_to"})
    elif group == "branches3":
        aliases = {"bushv": "bus_hv", "busmv": "bus_mv", "buslv": "bus_lv"}
    field = aliases.get(port, port)
    if element.get("class") == "StaSwitch" and field != "bus1":
        raise ValueError("StaSwitch tiene un único puerto bus1")
    if field not in PORTS[group] or (group in ("loads", "gens", "shunts") and port == "bus2"):
        raise ValueError(f"Puerto incompatible: {port}")
    return field


def native_port(group, element, field):
    if group == "branches":
        return ({"bus_from": "bushv", "bus_to": "buslv"} if element["class"] == "ElmTr2"
                else {"bus_from": "bus1", "bus_to": "bus2"})[field]
    if group == "branches3":
        return {"bus_hv": "bushv", "bus_mv": "busmv", "bus_lv": "buslv"}[field]
    return "bus1" if group in ("loads", "gens", "shunts") else field


def create_command(variant_id, group, data):
    """Genera identidad sintética. Los parámetros eléctricos siguen explícitos."""
    item_id = "power_machine_" + str(uuid.uuid4())
    item = deepcopy(data)
    for key in IDENTITY:
        item.pop(key, None)
    item["class"] = data.get("class", CLASSES.get(group, (None,))[0])
    item.update(id=item_id, path=f"power_machine://variants/{variant_id}/{item_id}",
                provenance={"origin": "power_machine_created", "pf_object_id": None,
                            "pf_relative_path": None})
    item["origin"] = "power_machine_created"
    if data.get("provenance", {}).get("conversions"):
        item["provenance"]["conversions"] = deepcopy(data["provenance"]["conversions"])
    return VariantCommand("create", group, item_id, item)


def edit_command(model, op, group, object_id, data=None):
    entry = index_model(model).get(object_id)
    if entry is None or entry[0] != group:
        raise ValueError("Objeto o grupo desconocido")
    return VariantCommand(op, group, object_id, deepcopy(data or {}), object_sha256(entry[1]))


def merge_fields(target, changes):
    """Mezcla mapas de parámetros, conservando atributos no editados."""
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_fields(target[key], value)
        else:
            target[key] = deepcopy(value)


def sync_power_fields(item, changes):
    """Sincroniza potencias públicas con atributos nativos que consume el LF.

    P/Q públicas están en MW/Mvar por unidad para carga y totales para generador.
    scale0 y ngnum conservan el significado del motor, PV nativo usa kW/kvar.
    """
    cls = item.get("class")
    params = item.setdefault("params", {})
    if cls == "ElmLod":
        for public, native in (("p_mw", "plini"), ("q_mvar", "qlini"), ("scale", "scale0")):
            if public in changes:
                params[native] = changes[public]
            elif native in changes.get("params", {}):
                item[public] = params[native]
    elif cls == "ElmXnet":
        for public,native in (("p_mw","pgini"),("q_mvar","qgini"),("v_set_pu","usetp")):
            if public in changes and native not in changes.get('params',{}):params[native]=changes[public]
        from power_machine.external_grid import normalize_external
        normalize_external(item)
    elif cls in ("ElmSym", "ElmGenstat", "ElmPvsys"):
        n, scale = params.get("ngnum"), params.get("scale0")
        if n is not None and scale is not None and n > 0 and scale > 0:
            divisor = 1000 if cls == "ElmPvsys" else 1
            for public, native in (("p_mw", "pgini"), ("q_mvar", "qgini")):
                if public in changes:
                    params[native] = None if changes[public] is None else changes[public] * divisor / (n * scale)
                elif native in changes.get("params", {}):
                    item[public] = None if params[native] is None else params[native] * n * scale / divisor
        if "v_set_pu" in changes:
            params["usetp"] = changes["v_set_pu"]


def replay_commands(base_model, variant):
    """No publica estados intermedios. Los errores identifican el comando fallido."""
    model, layout = deepcopy(base_model), deepcopy(variant.layout)
    for command in variant.commands:
        def fail(code, message):
            raise VariantError((VariantCheck("error", code, command.object_id, message),))
        if command.group not in GROUPS:
            fail("unknown_group", "Grupo inexistente: " + command.group)
        if command.op not in ("create", "update", "retire", "connect", "disconnect", "move"):
            fail("unknown_operation", "Operación desconocida: " + command.op)
        if not isinstance(command.data, dict):
            fail("invalid_command", "data debe ser un mapa")
        entries = index_model(model)
        entry = entries.get(command.object_id)
        if command.op == "create":
            if entry is not None:
                fail("duplicate_id", "ID repetido en el inventario")
            item = deepcopy(command.data)
            if item.get("class") not in CLASSES[command.group]:
                fail("unsupported_class", "Clase fuera del dominio del grupo")
            expected_path = f"power_machine://variants/{variant.variant_id}/{command.object_id}"
            try:
                uuid.UUID(command.object_id.removeprefix("power_machine_"))
            except (ValueError, AttributeError):
                fail("invalid_identity", "La identidad nueva requiere power_machine_ y UUID")
            if not command.object_id.startswith("power_machine_") or item.get("id") != command.object_id or item.get("path") != expected_path:
                fail("invalid_identity", "Identidad o ruta sintética inconsistente")
            provenance = item.get("provenance", {})
            if any(provenance.get(key, "missing") != value for key, value in
                   {"origin": "power_machine_created", "pf_object_id": None, "pf_relative_path": None}.items()):
                fail("invalid_origin", "La procedencia nueva no puede fabricar identidad PF")
            sync_power_fields(item, command.data)
            model.setdefault(command.group, []).append(item)
            continue
        if entry is None or entry[0] != command.group:
            fail("unknown_object", "Objeto ausente o pertenece a otro grupo")
        item = entry[1]
        if command.expected_sha256 is None and command.op != "move":
            fail("missing_object_hash", "La edición requiere expected_sha256")
        if command.expected_sha256 is not None and command.expected_sha256 != object_sha256(item):
            fail("object_hash_mismatch", "El objeto cambió respecto del comando anterior")
        if command.op == "move":
            data = command.data
            if set(data) != {"x_canvas", "y_canvas", "diagram_group"}:
                fail("invalid_layout", "Mover requiere x_canvas, y_canvas y diagram_group")
            if any(isinstance(data[k], bool) or not isinstance(data[k], (int, float)) or not math.isfinite(data[k])
                   for k in ("x_canvas", "y_canvas")) or not isinstance(data["diagram_group"], str):
                fail("invalid_layout", "Posición o grupo geométrico inválido")
            layout["groups"].setdefault(data["diagram_group"], {})[command.object_id] = [data["x_canvas"], data["y_canvas"]]
        elif command.op == "update":
            if IDENTITY.intersection(command.data) or set(PORTS[command.group]).intersection(command.data):
                fail("immutable_identity", "Identidad inmutable, use connect para cambiar puertos")
            if item.get("class") == "StaSwitch" and {"element_id", "port"}.intersection(command.data):
                fail("immutable_cubicle", "La asociación de cubículo no se edita por nombre")
            merge_fields(item, command.data)
            sync_power_fields(item, command.data)
        elif command.op == "retire":
            if command.data:
                fail("invalid_command", "retire no admite datos")
            item["outserv"] = 1
        else:
            allowed = {"port", "bus_id"} if command.op == "connect" else {"port"}
            if set(command.data) != allowed:
                fail("invalid_port", "Conexión requiere puerto y barra explícitos")
            try:
                field = port_field(command.group, item, command.data["port"])
            except ValueError as exc:
                fail("invalid_port", str(exc))
            bid = command.data.get("bus_id") if command.op == "connect" else None
            if command.op == "connect" and (bid not in entries or entries[bid][0] != "buses" or entries[bid][1].get("outserv") == 1):
                fail("unknown_bus", "Barra de destino inexistente o retirada")
            item[field] = bid
            if item.get("class") == "StaSwitch":
                target_entry = entries.get(item.get("element_id"))
                if target_entry is None:
                    fail("unknown_switch_target", "Cubículo sin elemento identificable")
                target_group, target_item = target_entry
                try:
                    target_field = port_field(target_group, target_item, item.get("port"))
                except ValueError as exc:
                    fail("invalid_port", str(exc))
                if field != "bus1":
                    fail("invalid_port", "StaSwitch requiere su único puerto bus1")
                target_item[target_field] = bid
                for cubicle in model.get("switches", []):
                    if cubicle.get("element_id") == target_item["id"] and cubicle.get("port") == item["port"]:
                        cubicle["bus1"] = bid
            # Cada cubículo sigue su puerto real, incluso si está abierto.
            for switch in model.get("switches", []):
                if switch.get("class") == "StaSwitch" and switch.get("element_id") == item["id"] and switch.get("port") == native_port(command.group, item, field):
                    switch["bus1"] = bid
    return model, layout


class VariantHistory:
    """Historial por snapshots privados, con undo/redo exactos y revisiones preservadas."""

    def __init__(self, base_model, variant):
        from .materialize import materialize_variant
        materialize_variant(base_model, variant)
        self._base = deepcopy(base_model)
        self._snapshots = [deepcopy(variant)]
        self._cursor = 0
        self._next_revision = variant.revision + 1

    @property
    def variant(self):
        return deepcopy(self._snapshots[self._cursor])

    @property
    def model(self):
        from .materialize import materialize_variant
        return materialize_variant(self._base, self.variant)

    def apply(self, commands):
        from .materialize import materialize_variant
        candidate = self.propose(commands)
        materialize_variant(self._base, candidate)
        self.publish_checked(candidate)
        return self.variant

    def propose(self, commands):
        """Propuesta sin publicación. La materialización puede ocurrir en un trabajador."""
        if isinstance(commands, VariantCommand):
            commands = (commands,)
        candidate = replace(self.variant, revision=self._next_revision,
                            commands=self.variant.commands + tuple(deepcopy(commands)))
        return candidate

    def publish_checked(self, candidate):
        """Publica solo un snapshot que el llamador comprobó con el motor."""
        if (candidate.variant_id != self.variant.variant_id or candidate.base_model_sha256 != self.variant.base_model_sha256
                or candidate.revision != self._next_revision):
            raise ValueError("Propuesta obsoleta o ajena al historial")
        self._snapshots = self._snapshots[:self._cursor + 1] + [candidate]
        self._cursor += 1
        self._next_revision += 1

    def undo(self):
        if self._cursor > 0:
            self._cursor -= 1
        return self.variant

    def redo(self):
        if self._cursor + 1 < len(self._snapshots):
            self._cursor += 1
        return self.variant

    def to_dict(self):
        from .schema import variant_to_dict
        return {"schema": "power_machine-variant-history-v1", "cursor": self._cursor,
                "next_revision": self._next_revision,
                "snapshots": [variant_to_dict(v) for v in self._snapshots]}

    @classmethod
    def from_dict(cls, base_model, data):
        from .schema import variant_from_dict
        from .materialize import materialize_variant
        if not str(data.get("schema")).endswith("-variant-history-v1"):
            raise ValueError("Historial incompatible")
        variants = [variant_from_dict(v) for v in data["snapshots"]]
        if not variants or type(data.get("cursor")) is not int or not 0 <= data["cursor"] < len(variants):
            raise ValueError("Cursor de historial inválido")
        for variant in variants:
            if variant.variant_id != variants[0].variant_id:
                raise ValueError("Historial mezcla variantes")
            materialize_variant(base_model, variant)
        history = cls(base_model, variants[0])
        history._snapshots, history._cursor = deepcopy(variants), data["cursor"]
        history._next_revision = data["next_revision"]
        if type(history._next_revision) is not int or history._next_revision <= max(v.revision for v in variants):
            raise ValueError("Siguiente revisión inválida")
        return history
