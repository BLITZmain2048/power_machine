"""Geometría determinista del unifilar. Coordenadas en unidades de lienzo."""

from __future__ import annotations
from power_machine.gui.i18n import tr

from power_machine.gui.theme import theme

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import tempfile

SCHEMA = "power_machine-gui-layout-v1"
GROUPS = ("buses", "branches", "branches3", "switches", "gens", "loads", "shunts")
VOLTAGE_COLORS = {345: theme.color('kv_345'), 220: theme.color('kv_230'), 110: theme.color('kv_138'), 100: theme.color('ok'),
                  66: theme.color('kv_low'), 33: theme.color('kv_low'), 23: theme.color('kv_low'),
                  18: theme.color('kv_low'), 13.8: theme.color('kv_low'), 0.69: theme.color('kv_low')}
OFF_COLOR = theme.color('deenergized')


def clean_label(value):
    """Los rótulos nunca contienen punto y coma, incluso desde datos PF."""
    return str(value).replace(chr(59), ",")


def voltage_color(un_kv):
    level = float(un_kv or 0)
    return theme.color('kv_345' if level >= 300 else 'kv_230' if level >= 200 else
                       'kv_138' if level >= 100 else 'kv_low')


def element_index(model):
    return {item["id"]: dict(item, group=group) for group in GROUPS
            for item in model.get(group, [])}


def terminals(item):
    return tuple(dict.fromkeys(item[key] for key in
                 ("bus", "bus_from", "bus_to", "bus_hv", "bus_mv", "bus_lv", "bus1", "bus2")
                 if item.get(key)))


def group_key(item):
    """Identidad por ámbito y ruta completa hasta ElmSubstat o ElmNet."""
    parts = item.get("relative_path", item.get("path", "")).split("\\")
    stops = [i for i, p in enumerate(parts) if p.endswith(".ElmSubstat")]
    if not stops:
        stops = [i for i, p in enumerate(parts) if p.endswith(".ElmNet")]
    path = "\\".join(parts[:stops[-1] + 1]) if stops else 'Raíz'
    return item.get("scope", "sistema_interno") + "::" + path


def diagram_groups(model):
    groups = {}
    for bus in model.get("buses", []):
        key = group_key(bus)
        name = key.split("\\")[-1].split("::")[-1].rsplit(".", 1)[0]
        if name == "Raíz":
            name = tr("integration.root")
        if bus.get("scope") == "equivalente":
            name = tr('integration.equivalent') + name
        elif name == "Red":
            name = tr('integration.internal_system_network_root')
        group = groups.setdefault(key, dict(id=key, name=name, bus_ids=[], un_kv_levels=[]))
        group["bus_ids"].append(bus["id"])
        if bus.get("un_kv") is not None:
            group["un_kv_levels"].append(round(float(bus["un_kv"]), 3))
    for group in groups.values():
        group["bus_ids"].sort()
        group["un_kv_levels"] = sorted(set(group["un_kv_levels"]), reverse=True)
    return sorted(groups.values(), key=lambda g: (-max(g["un_kv_levels"], default=0), g["name"], g["id"]))


@dataclass
class Symbol:
    id: str
    kind: str
    x: float
    y: float
    width: float
    height: float
    label: str
    un_kv: float | None = None
    item: dict = field(default_factory=dict)
    label_box: tuple | None = None
    result_box: tuple | None = None
    remote_group: str | None = None

    @property
    def bbox(self):
        if self.label_box is not None:
            return (self.x - self.width / 2, self.y - self.height / 2,
                    self.x + self.width / 2, self.y + self.height / 2)
        if self.kind == "port_switch":
            return (self.x - 8, self.y - 8, self.x + 8, self.y + 8)
        if self.kind not in ("bus", "group", "overview_branch", "overview_circuit"):
            return (self.x - 24, self.y - 28, self.x + 200, self.y + self.height - 28)
        return (self.x - self.width / 2, self.y - 28,
                self.x + self.width / 2, self.y + self.height - 28)


@dataclass
class Wire:
    id: str
    start: str
    end: str
    points: list[tuple[float, float]]
    un_kv: float | None = None
    label: str = ""
    members: tuple = ()
    label_box: tuple | None = None


@dataclass
class Scene:
    group: str | None
    title: str
    symbols: dict[str, Symbol] = field(default_factory=dict)
    wires: list[Wire] = field(default_factory=list)

    @property
    def bounds(self):
        boxes = [s.bbox for s in self.symbols.values()]
        boxes += [s.label_box for s in self.symbols.values() if s.label_box]
        boxes += [s.result_box for s in self.symbols.values() if s.result_box]
        boxes += [w.label_box for w in self.wires if w.label_box]
        points = [p for w in self.wires for p in w.points]
        if not boxes:
            return (0, 0, 900, 500)
        return (min([b[0] for b in boxes] + [p[0] for p in points]),
                min([b[1] for b in boxes] + [p[1] for p in points]),
                max([b[2] for b in boxes] + [p[0] for p in points]),
                max([b[3] for b in boxes] + [p[1] for p in points]))


def intersections(scene):
    """Intersecciones estrictas de cajas de símbolos y sus rótulos reservados."""
    symbols = list(scene.symbols.values())
    pairs = []
    for i, a in enumerate(symbols):
        ax, ay, ar, ab = a.bbox
        for b in symbols[i + 1:]:
            bx, by, br, bb = b.bbox
            if min(ar, br) > max(ax, bx) and min(ab, bb) > max(ay, by):
                pairs.append((a.id, b.id))
    return pairs


def build_scene(model, group=None, *, result_boxes=False):
    """No fusiona nodos ni deduce tensiones calculadas. Conserva IDs PF."""
    from .design import make_scene
    return make_scene(model, group, result_boxes=result_boxes)


def load_positions(path, group):
    """Un archivo ausente es normal. Un archivo corrupto se informa al llamador."""
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(tr('integration.incompatible_layout_schema'))
    if not isinstance(data.get("groups", {}), dict):
        raise ValueError(tr('integration.incompatible_layout_groups'))
    positions = data.get("groups", {}).get(group or "__general__", {})
    if not isinstance(positions, dict):
        raise ValueError(tr('integration.incompatible_layout_positions'))
    return {key: (float(value[0]), float(value[1])) for key, value in positions.items()
            if isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value)}


def save_positions(path, group, positions):
    """Reemplazo atómico en el mismo directorio, preservando los demás grupos."""
    path = Path(path)
    data = {"schema": SCHEMA, "groups": {}}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError(tr('integration.incompatible_layout_schema'))
        if not isinstance(data.get("groups", {}), dict):
            raise ValueError(tr('integration.incompatible_layout_groups'))
    normalized = {}
    for key, value in positions.items():
        if len(value) != 2 or not all(math.isfinite(float(v)) for v in value):
            raise ValueError(tr('integration.nonfinite_position'))
        normalized[key] = [float(v) for v in value]
    data.setdefault("groups", {})[group or "__general__"] = normalized
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def apply_positions(scene, positions):
    """Mueve símbolos conservando conexiones ortogonales a sus extremos."""
    deltas = {}
    for key, (x, y) in positions.items():
        if key in scene.symbols:
            symbol = scene.symbols[key]
            if (x, y) == (symbol.x, symbol.y):
                continue
            deltas[key] = (x - symbol.x, y - symbol.y)
            dx, dy = deltas[key]
            for field_name in ('label_box', 'result_box'):
                box = getattr(symbol, field_name)
                if box:
                    setattr(symbol, field_name, (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))
            symbol.x, symbol.y = x, y
    for wire in scene.wires:
        points = list(wire.points)
        if scene.group is None:
            for key, pos in ((wire.start, 0), (wire.end, -1)):
                if key in deltas:
                    dx, dy = deltas[key]
                    points[pos] = (points[pos][0] + dx, points[pos][1] + dy)
            wire.points = points
            continue
        for key, pos in ((wire.start, 0), (wire.end, -1)):
            if key in deltas:
                dx, dy = deltas[key]
                old = points[pos]
                new = (old[0] + dx, old[1] + dy)
                if pos == 0:
                    points = [new, (new[0], old[1]), old] + points[1:]
                else:
                    points = points[:-1] + [old, (new[0], old[1]), new]
        wire.points = points
