"""Informe de estudio reproducible en LaTeX blanco y negro.

El módulo recibe instantáneas y resultados serializables, conserva los valores
completos en CSV y deja una huella de entradas y salidas en manifest.json.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from power_machine.lf_status import energized_status
from .i18n import tr


TEMPLATE_DIR = Path(__file__).with_name("report_templates")
MANIFEST_SCHEMA = "power_machine-study-report-manifest-v1"
LOCAL_PACKAGES = ("article", "polyglossia", "inputenc", "fontenc", "babel", "geometry",
                  "longtable", "booktabs", "array", "graphicx")
_SPECIAL = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
            "#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&",
            "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
            ";": ","}


def latex_escape(value: Any) -> str:
    """Devuelve texto seguro para LaTeX, incluso para nombres y rutas."""
    return "".join(_SPECIAL.get(char, char) for char in str(value))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _format(value: Any, field: str = "") -> str:
    if value is None:
        return "ausente"
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, float):
        decimals = {
            "u_pu": 4, "u_kv": 2, "angulo_deg": 2,
            "p_desde_mw": 2, "q_desde_mvar": 2, "p_hacia_mw": 2,
            "q_hacia_mvar": 2, "p_mw": 2, "q_mvar": 2,
            "p_entrada_mw": 2, "q_entrada_mvar": 2,
            "u_min_pu": 4, "u_max_pu": 4,
            "loading_pct": 1, "generation_mw": 2, "demand_mw": 2,
            "losses_mw": 2, "interface_mw": 2,
        }.get(field)
        return format(value, ".%df" % decimals) if decimals is not None else format(value, ".6g")
    return str(value)


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    return str(value)


def _safe_slug(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_.")
    return text or "resultado"


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = [dict(row) for row in rows]
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})


def _table(columns: list[tuple[str, str, float, str]], rows: list[dict[str, Any]], caption: str,
           label: str, legend: str = "") -> str:
    """Compone una longtable cuyo ancho útil nunca supera 0,96 textwidth."""
    width = sum(column[2] for column in columns)
    if width > 0.960001:
        raise ValueError("Los anchos de tabla exceden el ancho de texto")
    layout = " ".join(
        (r">{\raggedleft\arraybackslash}p{%.3f\textwidth}" if align == "right"
         else r">{\raggedright\arraybackslash}p{%.3f\textwidth}") % width
        for _, _, width, align in columns
    )
    header = " & ".join(latex_escape(title) for _, title, _, _ in columns) + r" \\"
    legend_line = (r"\multicolumn{" + str(len(columns)) + r"}{p{0.94\textwidth}}"
                   r"{\footnotesize " + latex_escape(legend) + r"}\\") if legend else ""
    lines = [r"{\small\sloppy\emergencystretch=2em\setlength{\tabcolsep}{1pt}\renewcommand{\arraystretch}{1.08}",
             r"\begin{longtable}{" + layout + "}",
             r"\caption{" + latex_escape(caption) + r"}\label{" + label + r"}\\",
             r"\toprule", header, r"\midrule", legend_line, r"\endfirsthead",
             r"\toprule", header, r"\midrule", legend_line, r"\endhead"]
    def cell(value, key):
        raw = _format(value, key)
        pieces = re.split(r"(\S{19,})", raw)
        text = "".join(r"\allowbreak{}".join(latex_escape(piece[i:i+14]) for i in range(0,len(piece),14)) if len(piece)>18 and not any(c.isspace() for c in piece) else latex_escape(piece) for piece in pieces).replace(r"\_", r"\_\allowbreak{}").replace("/", r"/\allowbreak{}")
        return text
    for row in rows:
        lines.append(" & ".join(cell(row.get(key), key) for key, _, _, _ in columns) + r" \\")
    lines.extend((r"\bottomrule", r"\end{longtable}", r"}"))
    return "\n".join(lines)


def _result_meta(item: dict[str, Any], index: int) -> dict[str, str]:
    status = str(item.get("status", "completed"))
    return {"module": str(item.get("module", item.get("kind", "resultado"))),
            "run_id": str(item.get("run_id", "run-%d" % (index + 1))), "status": status,
            "freshness": str(item.get("freshness", item.get("vigencia", tr('integration.current')))),
            "validation_label": str(item.get("validation_label", tr('integration.no_pf_validation_08f858')))}


def _result_heading(meta: dict[str, str]) -> str:
    if meta["module"].lower() in ("loadflow", "lf", "flujo"):
        case = {"power_machine_lf_base": "caso power_machine base",
                "ieee39_lf_base": "caso IEEE 39 base"}.get(meta["run_id"], meta["run_id"].replace("_", " "))
        return "Resultados del flujo de potencia · " + case
    return "Resultados de " + meta["module"] + " · " + meta["run_id"]


def _option_label(key: str, value: Any) -> tuple[str, str]:
    names = {"q_limits": tr('integration.reactive_power_limits'), "missing": "Ramas con datos faltantes",
             "provisional_tr3": tr('integration.provisional_3w_transformer_losses'),
             "tolerance_pu": "Tolerancia de flujo p.u.", "max_iter": tr('integration.maximum_iterations'),
             "warm_start": "Arranque desde flujo previo"}
    choices = {("q_limits", True): "sí", ("q_limits", False): "no",
               ("provisional_tr3", True): "sí", ("provisional_tr3", False): "no",
               ("warm_start", True): "sí", ("warm_start", False): "no",
               ("missing", "open"): tr('integration.open_for_diagnostics_only'),
               ("missing", "error"): tr('integration.reject_calculation')}
    return names.get(key, key.replace("_", " ").capitalize()), str(choices.get((key, value), _format(value)))


def _short_warning(value: Any, model=None) -> str:
    """Reduce rutas PF a elemento y subestacion antes de componer una tabla."""
    text = re.sub(r"pf_[a-f0-9]+", lambda match: _visible_name(match.group(), model), str(value)).replace("\\", " / ")
    parts = [part.strip() for part in text.split(" / ") if part.strip()]
    net_index = next((index for index, part in enumerate(parts)
                      if part.endswith(".ElmNet")), None)
    if net_index is not None and net_index + 1 < len(parts):
        substation = parts[net_index][:-7]
        element = parts[-1]
        prefix = " / ".join(parts[:net_index])
        visible = "%s en %s" % (element, substation)
        text = (prefix + " " + visible).strip()
    if len(text) <= 160:
        return text
    tail = " / ".join(parts[-2:]) if len(parts) > 1 else text
    return (tail if len(tail) <= 160 else tail[-157:]).lstrip()


def _substation(path: Any) -> str:
    """Extrae una referencia visible de subestación desde la ruta de origen."""
    parts = str(path or "").replace("/", "\\").split("\\")
    for part in parts:
        if part.endswith(".ElmNet"):
            return part[:-7]
    return tr('integration.not_specified')


def _network_index(model: dict | None) -> tuple[dict[str, dict], dict[str, dict]]:
    model = model or {}
    buses = {row.get("id"): row for row in model.get("buses", [])}
    elements = {}
    for group in ("branches", "branches3", "gens", "loads"):
        elements.update({row.get("id"): row for row in model.get(group, [])})
    return buses, elements


def _visible_bus(row: dict, buses: dict[str, dict]) -> dict[str, Any]:
    source = buses.get(row.get("id"), {})
    return {"barra": row.get("name") or source.get("name") or tr('integration.unnamed'),
            "subestacion": _substation(row.get("path") or source.get("path")),
            "nominal_kv": source.get("un_kv"),
            "u_pu": row.get("u_pu"), "u_kv": row.get("u_kv"),
            "angulo_deg": row.get("angle_deg")}


def _visible_branch(row: dict, buses: dict[str, dict]) -> dict[str, Any]:
    bus = buses.get(row.get("bus_from"), {})
    return {"elemento": row.get("name") or tr('integration.unnamed'),
            "subestacion": _substation(row.get("path")),
            "nominal_kv": bus.get("un_kv"),
            "p_desde_mw": row.get("p_from_mw"), "q_desde_mvar": row.get("q_from_mvar"),
            "p_hacia_mw": row.get("p_to_mw"), "q_hacia_mvar": row.get("q_to_mvar")}


def _visible_gen(row: dict, buses: dict[str, dict]) -> dict[str, Any]:
    bus = buses.get(row.get("bus"), {})
    return {"elemento": row.get("name") or tr('integration.unnamed'),
            "subestacion": _substation(row.get("path")),
            "nominal_kv": bus.get("un_kv"), "p_mw": row.get("p_mw"),
            "q_mvar": row.get("q_mvar")}


def _loadflow_tables(result: dict[str, Any], model: dict | None) -> list[tuple[str, list[dict[str, Any]], list[tuple[str, str, float, str]]]]:
    """Filas completas para anexos. Los IDs técnicos permanecen solo en CSV."""
    buses, elements = _network_index(model)
    branches = [{**elements.get(row.get("id"), {}), **row} for row in result.get("branches", [])]
    gens = [{**elements.get(row.get("id"), {}), **row} for row in result.get("gens", [])]
    return [
        ("barras", [_visible_bus(row, buses) for row in result.get("buses", []) if row.get("u_pu") is not None],
         [("barra", tr('integration.busbar'), .27, "left"), ("subestacion", tr('integration.substation'), .21, "left"),
          ("nominal_kv", "Nominal kV", .10, "right"), ("u_pu", "U p.u.", .09, "right"),
          ("u_kv", "U kV", .11, "right"), ("angulo_deg", "Ángulo deg", .13, "right")]),
        ("ramas", [_visible_branch(row, buses) for row in branches],
         [("elemento", tr('integration.element_49fa9f'), .24, "left"), ("subestacion", tr('integration.substation'), .15, "left"),
          ("nominal_kv", "Nominal kV", .08, "right"), ("p_desde_mw", "P desde MW", .115, "right"),
          ("q_desde_mvar", "Q desde Mvar", .115, "right"), ("p_hacia_mw", "P hacia MW", .115, "right"),
          ("q_hacia_mvar", "Q hacia Mvar", .115, "right")]),
        ("generadores", [_visible_gen(row, buses) for row in gens],
         [("elemento", tr('integration.element_49fa9f'), .32, "left"), ("subestacion", tr('integration.substation'), .22, "left"),
          ("nominal_kv", "Nominal kV", .12, "right"), ("p_mw", "P MW", .13, "right"),
          ("q_mvar", "Q Mvar", .13, "right")]),
    ]


def _generic_rows(result: dict[str, Any], meta: dict[str, str]) -> list[dict[str, Any]]:
    rows = result.get("rows") or result.get("results") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        rows = [{key: value for key, value in result.items() if not isinstance(value, (dict, list))}]
    common = {"run_id": meta["run_id"], "estado": meta["status"], "vigencia": meta["freshness"],
              "rotulo_pf": meta["validation_label"]}
    return [{**common, **dict(row)} for row in rows if isinstance(row, dict)]


def _loading_rows(model: dict | None, result: dict, buses: dict[str, dict],
                  scope: str | None = None) -> list[dict[str, Any]]:
    """Obtiene solo cargabilidades de islas resueltas, sin inventar ratings."""
    values = {}
    if model:
        try:
            from power_machine.loading import branch_loading
            values = branch_loading(model, result)
        except (KeyError, TypeError, ValueError):
            values = {}
    rows = []
    _, elements = _network_index(model)
    for branch in result.get("branches", []):
        source = elements.get(branch.get("id"), {})
        if scope is not None and source.get("scope") != scope:
            continue
        loading = values.get(branch.get("id"), {})
        percentage = loading.get("loading_pct", branch.get("loading_rate_a_pct"))
        if percentage is None or branch.get("status") not in (None, "resuelto", "solved"):
            continue
        visible = _visible_branch(branch, buses)
        visible["loading_pct"] = percentage
        rows.append(visible)
    return sorted(rows, key=lambda row: float(row["loading_pct"]), reverse=True)


def _deenergized_rows(result: dict, buses: dict[str, dict]) -> list[dict[str, Any]]:
    rows = []
    for island in result.get("islands") or []:
        if island.get("status") != "sin_referencia":
            continue
        members = island.get("buses") or []
        names = [buses.get(bus_id, {}).get("name", bus_id) for bus_id in members[:3]]
        suffix = tr('integration.and_more') if len(members) > 3 else ""
        rows.append({"isla": island.get("id"), "barras": len(members),
                     "referencia": "sin referencia", "muestra": ", ".join(names) + suffix})
    return rows


def _interface_interchange(model: dict | None, result: dict) -> tuple[float | None, float | None, list[dict[str, Any]]]:
    if not model:
        return None, None, []
    solved = {row.get("id") for row in result.get("islands", []) if row.get("status") == "resuelto"}
    rows = {row.get("id"): row for row in result.get("branches", [])}
    values, reactive, links = [], [], []
    for interface in model.get("interface", []):
        row = rows.get(interface.get("link_id"))
        if not row or row.get("island") not in solved:
            continue
        host = interface.get("host_bus")
        if row.get("bus_from") == host:
            p_value, q_value = row.get("p_from_mw"), row.get("q_from_mvar")
        elif row.get("bus_to") == host:
            p_value, q_value = row.get("p_to_mw"), row.get("q_to_mvar")
        else:
            continue
        if isinstance(p_value, (int, float)) and isinstance(q_value, (int, float)):
            if math.isfinite(p_value) and math.isfinite(q_value):
                links.append({"enlace": interface.get("circuit", "sin nombre"),
                              "p_entrada_mw": -float(p_value), "q_entrada_mvar": -float(q_value)})
                values.append(-float(p_value))
                reactive.append(-float(q_value))
    if not values:
        return None, None, links
    return sum(values), sum(reactive), links


def _scope_voltage_rows(result: dict, model: dict | None, solved_islands: set[Any]) -> list[dict[str, Any]]:
    buses, _ = _network_index(model)
    grouped: dict[tuple[str, float], list[dict]] = {}
    equivalent: list[dict] = []
    for row in result.get("buses", []):
        source = buses.get(row.get("id"), {})
        if row.get("island") not in solved_islands or row.get("u_pu") is None:
            continue
        scope, level = source.get("scope"), source.get("un_kv")
        if scope not in ("sistema_interno", "equivalente") or not isinstance(level, (int, float)):
            continue
        if scope == "equivalente":
            equivalent.append(row)
            continue
        if not any(abs(float(level) - nominal) < 0.2 for nominal in (220.0, 100.0, 33.0, 13.8)):
            continue
        grouped.setdefault((scope, float(level)), []).append(row)
    rows = []
    for (scope, level), values in sorted(grouped.items()):
        minimum, maximum = min(values, key=lambda item: item["u_pu"]), max(values, key=lambda item: item["u_pu"])
        rows.append({"alcance": "Red interna" if scope == "sistema_interno" else "Equivalente externo",
                     "nivel_kv": level, "u_min_pu": minimum["u_pu"],
                     "barra_min": _visible_bus(minimum, buses)["barra"],
                     "u_max_pu": maximum["u_pu"],
                     "barra_max": _visible_bus(maximum, buses)["barra"]})
    if equivalent:
        minimum, maximum = min(equivalent, key=lambda item: item["u_pu"]), max(equivalent, key=lambda item: item["u_pu"])
        rows.append({"alcance": "Equivalente externo", "nivel_kv": tr('integration.all'),
                     "u_min_pu": minimum["u_pu"], "barra_min": _visible_bus(minimum, buses)["barra"],
                     "u_max_pu": maximum["u_pu"], "barra_max": _visible_bus(maximum, buses)["barra"]})
    return rows


def _generation_by_type(result: dict, model: dict | None, solved_islands: set[Any]) -> list[dict[str, Any]]:
    _, elements = _network_index(model)
    grouped: dict[str, list[float]] = {}
    for row in result.get("gens", []):
        source = elements.get(row.get("id"), {})
        if row.get("island") not in solved_islands or source.get("scope") != "sistema_interno":
            continue
        key = source.get("class", "Clase no informada")
        grouped.setdefault(key, [0.0, 0.0])
        grouped[key][0] += float(row.get("p_mw") or 0.0)
        grouped[key][1] += float(row.get("q_mvar") or 0.0)
    labels = {"ElmSym": tr('integration.synchronous_machine'), "ElmGenstat": tr('integration.static_converter'),
              "ElmPvsys": "Fotovoltaico"}
    return [{"tipo": labels.get(kind, kind), "p_mw": values[0], "q_mvar": values[1]}
            for kind, values in sorted(grouped.items())]


def _summary_rows(result: dict, model: dict | None, limits: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    buses, _ = _network_index(model)
    solved_islands = {row.get("id") for row in result.get("islands", []) if row.get("status") == "resuelto"}
    has_equivalent = any(item.get("scope") == "equivalente"
                         for group in ("buses", "gens", "loads", "branches", "branches3")
                         for item in (model or {}).get(group, []))
    solved_buses = [row for row in result.get("buses", [])
                    if row.get("island") in solved_islands and row.get("u_pu") is not None]
    min_bus = min(solved_buses, key=lambda row: row["u_pu"], default=None)
    max_bus = max(solved_buses, key=lambda row: row["u_pu"], default=None)
    _, elements = _network_index(model)
    def belongs(row: dict, scope: str | None = None) -> bool:
        return row.get("island") in solved_islands and (scope is None or elements.get(row.get("id"), {}).get("scope") == scope)
    scope = "sistema_interno" if has_equivalent else None
    solved_gens = [row for row in result.get("gens", []) if belongs(row, scope)]
    solved_loads = [row for row in result.get("loads", []) if belongs(row, scope)]
    solved_branches = [row for row in result.get("branches", []) if belongs(row, scope)]
    generation_mw = sum(float(row["p_mw"]) for row in solved_gens if isinstance(row.get("p_mw"), (int, float)))
    demand_mw = sum(float(row["p_mw"]) for row in solved_loads if isinstance(row.get("p_mw"), (int, float)))
    losses_mw = sum(float(row["p_loss_mw"]) for row in solved_branches if isinstance(row.get("p_loss_mw"), (int, float)))
    interface_mw, interface_q_mvar, _ = _interface_interchange(model, result)
    if has_equivalent:
        losses_mw = generation_mw + (interface_mw or 0.0) - demand_mw
        balance_mw = generation_mw + (interface_mw or 0.0) - demand_mw - losses_mw
        summary = [{"indicador": "Demanda interna P", "valor": _format(demand_mw, "demand_mw"), "unidad": "MW", "ubicacion": ""},
                   {"indicador": "Demanda interna Q", "valor": _format(sum(float(row.get("q_mvar") or 0.0) for row in solved_loads), "demand_mw"), "unidad": "Mvar", "ubicacion": ""},
                   {"indicador": tr('integration.internal_p_generation'), "valor": _format(generation_mw, "generation_mw"), "unidad": "MW", "ubicacion": ""},
                   {"indicador": tr('integration.internal_q_generation'), "valor": _format(sum(float(row.get("q_mvar") or 0.0) for row in solved_gens), "generation_mw"), "unidad": "Mvar", "ubicacion": ""},
                   {"indicador": "Intercambio interface P, entrada a red interna", "valor": _format(interface_mw, "interface_mw"), "unidad": "MW", "ubicacion": ""},
                   {"indicador": "Intercambio interface Q, entrada a red interna", "valor": _format(interface_q_mvar, "interface_mw"), "unidad": "Mvar", "ubicacion": ""},
                   {"indicador": tr('integration.computed_internal_losses'), "valor": _format(losses_mw, "losses_mw"), "unidad": "MW", "ubicacion": ""},
                   {"indicador": "Cierre de balance activo", "valor": _format(balance_mw, "losses_mw"), "unidad": "MW", "ubicacion": "tolerancia 0.000001 MW"}]
    else:
        summary = [{"indicador": tr('integration.total_generation'), "valor": _format(generation_mw, "generation_mw"), "unidad": "MW", "ubicacion": ""},
                   {"indicador": "Demanda total", "valor": _format(demand_mw, "demand_mw"), "unidad": "MW", "ubicacion": ""},
                   {"indicador": tr('integration.total_losses'), "valor": _format(losses_mw, "losses_mw"), "unidad": "MW", "ubicacion": ""},
                   {"indicador": "Intercambio en la frontera", "valor": _format(interface_mw, "interface_mw") if interface_mw is not None else "No aplica", "unidad": "MW" if interface_mw is not None else "", "ubicacion": ""}]
    if not has_equivalent:
        summary += [{"indicador": tr('integration.minimum_voltage'), "valor": _format(min_bus.get("u_pu") if min_bus else None, "u_pu"),
                    "unidad": "p.u.", "ubicacion": _visible_bus(min_bus, buses)["barra"] if min_bus else "No disponible"},
                   {"indicador": tr('integration.maximum_voltage'), "valor": _format(max_bus.get("u_pu") if max_bus else None, "u_pu"),
                    "unidad": "p.u.", "ubicacion": _visible_bus(max_bus, buses)["barra"] if max_bus else "No disponible"}]
    violations = []
    low, high = limits.get("v_min_pu"), limits.get("v_max_pu")
    for row in solved_buses:
        value = row["u_pu"]
        if (low is not None and value < float(low)) or (high is not None and value > float(high)):
            visible = _visible_bus(row, buses)
            violations.append({"elemento": visible["barra"], "tipo": tr('integration.voltage'),
                               "valor": value, "limite": "%s a %s p.u." % (low, high)})
    for row in _loading_rows(model, result, buses, scope=scope):
        limit = limits.get("loading_max_pct")
        if limit is not None and row["loading_pct"] > float(limit):
            violations.append({"elemento": row["elemento"], "tipo": tr('integration.loading_heatmap'),
                               "valor": row["loading_pct"], "limite": "%s %%" % limit})
    return summary, _loading_rows(model, result, buses, scope=scope)[:10], violations


def _write_figure(output_dir: Path, item: dict[str, Any], index: int) -> str | None:
    from .report_templates.calculations import write_figure
    return write_figure(output_dir, item, index)


def _envelope_payload(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("result", item)
    if isinstance(source, dict) and isinstance(source.get("payload"), dict):
        return source["payload"]
    return source if isinstance(source, dict) else {}


def _visible_name(item_id: Any, model: dict | None) -> str:
    buses, elements = _network_index(model)
    element = buses.get(item_id, elements.get(item_id, next((row for row in (model or {}).get("switches", []) if row.get("id") == item_id), {})))
    if element.get("name"):
        return str(element["name"])
    kind = str(element.get("class") or tr('integration.element_49fa9f'))
    ends = [element.get(key) for key in ("bus_from", "bus_to", "bus") if element.get(key)]
    return kind + (" " + " - ".join(str(buses.get(value, {}).get("name", "barra sin nombre")) for value in ends) if ends else tr('integration.without_an_exported_busbar_association'))


def _restriction_rows(item: dict[str, Any], snapshot: dict[str, Any], module: str) -> list[dict[str, str]]:
    capability = ((snapshot.get("capabilities") or {}).get("calculations") or {}).get(module, {})
    values = list(item.get("restrictions") or []) + list(capability.get("restrictions") or [])
    values.append(tr('integration.no_pf_validation_f85232'))
    return [{"restriccion": value} for value in dict.fromkeys(str(value) for value in values if value)]


def _render_envelope(item, meta, snapshot, model, index, output_dir):
    from .report_templates.calculations import render_calculation
    return render_calculation(item, meta, snapshot, model, index, output_dir)


def verify_tex_envelope_values(tex_path: str | Path, envelope: dict[str, Any]) -> None:
    """Contrasta valores representativos de sobres con la fuente LaTeX."""
    text = Path(tex_path).read_text(encoding="utf-8")
    payload = envelope.get("payload", envelope)
    for group in (payload.get("points", []), payload.get("buses", []), payload.get("branches", []), payload.get("rows", [])):
        for row in group if isinstance(group, list) else []:
            for key in ("increase_mw", "voltage_set_pu", "source_q_mvar", "u_pu", "du_dp_pu_per_mw", "du_dq_pu_per_mvar", "ptdf_mw_per_mw", "lodf_mw_per_mw", "ik_initial_sym_ka"):
                value = row.get(key)
                if isinstance(value, float) and math.isfinite(value) and _format(value, key) not in text:
                    raise AssertionError("Falta %s del sobre en LaTeX" % key)


def detect_latex_engine() -> str | None:
    """Encuentra un compilador local permitido, con preferencia por XeLaTeX."""
    for name in ("xelatex", "pdflatex"):
        location = shutil.which(name)
        if location:
            return location
    return None


def compile_latex(output_dir: str | Path, *, engine: str | None = None) -> dict[str, Any]:
    """Compila dos pasadas sin shell escape y conserva el log de cada intento."""
    directory = Path(output_dir)
    executable = engine or detect_latex_engine()
    if not executable:
        return {"status": "not_available", "engine": None, "pdf": None,
                "log": None, "message": "No hay compilador LaTeX local disponible"}
    command = [executable, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "informe.tex"]
    scratch = (directory / ".cache/latex").resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({"MIKTEX_USERDATA": str(scratch / "userdata"),
                        "MIKTEX_USERCONFIG": str(scratch / "config"),
                        "TEXMFVAR": str(scratch / "texmfvar")})
    text_log: list[str] = []
    completed = None
    for _ in range(2):
        completed = subprocess.run(command, cwd=directory, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", shell=False,
                                   env=environment)
        text_log.append(completed.stdout + completed.stderr)
        if completed.returncode:
            break
    log_path = directory / "compilacion_latex.log"
    log_path.write_text("\n".join(text_log), encoding="utf-8", newline="\n")
    pdf = directory / "informe.pdf"
    status = "compiled" if completed and completed.returncode == 0 and pdf.is_file() else "failed"
    return {"status": status, "engine": executable, "pdf": pdf.name if status == "compiled" else None,
            "log": log_path.name, "message": tr('integration.compilation_completed') if status == "compiled" else tr('integration.latex_compilation_failed')}


def verify_tex_table_values(tex_path: str | Path, loadflow_result: dict[str, Any]) -> None:
    """Comprueba que las magnitudes de tablas LF aparecen en el informe.

    Esta verificación contrasta el texto de presentación con el resultado
    original, sin confundirla con la conservación de precisión completa en CSV.
    """
    text = Path(tex_path).read_text(encoding="utf-8")
    fields = {
        "buses": {"u_pu": "u_pu", "u_kv": "u_kv", "angle_deg": "angulo_deg"},
        "branches": {"p_from_mw": "p_desde_mw", "q_from_mvar": "q_desde_mvar",
                     "p_to_mw": "p_hacia_mw", "q_to_mvar": "q_hacia_mvar"},
        "gens": {"p_mw": "p_mw", "q_mvar": "q_mvar"},
    }
    for group, keys in fields.items():
        for row in loadflow_result.get(group, []):
            for source_key, display_key in keys.items():
                value = row.get(source_key)
                if value is not None and latex_escape(_format(value, display_key)) not in text:
                    raise AssertionError("Falta %s.%s en la tabla LaTeX" % (group, source_key))


def verify_tex_utf8(tex_path: str | Path, input_texts: Iterable[Any]) -> None:
    """Rechaza interrogantes dentro de palabras y pérdidas UTF-8 de la entrada."""
    text = Path(tex_path).read_text(encoding="utf-8")
    if re.search(r"(?<=\w)\?(?=\w)", text, flags=re.UNICODE):
        raise AssertionError(tr('integration.the_report_contains_a_question_mark_inside'))
    for source in input_texts:
        for char in str(source):
            if ord(char) > 127 and char not in text:
                raise AssertionError(tr('integration.utf_8_character_r_was_lost') % char)


def generate_study_report(output_dir: str | Path, snapshot: dict[str, Any],
                          results: list[dict[str, Any]], *, study_id: str | None = None,
                          compile_pdf: bool = True) -> dict[str, Any]:
    """Genera un informe legible desde una instantánea cerrada y sus resultados."""
    if not isinstance(snapshot, dict) or not isinstance(results, list):
        raise TypeError("snapshot debe ser diccionario y results debe ser lista")
    directory = Path(output_dir)
    from power_machine.resources import output_root
    if not directory.resolve().is_relative_to(output_root()):
        raise ValueError("La salida debe permanecer dentro de la carpeta de resultados")
    directory.mkdir(parents=True, exist_ok=True)
    stale_pdf = directory / "informe.pdf"

    csv_dir = directory / "tablas"
    csv_dir.mkdir(exist_ok=True)
    manifest_inputs = {"snapshot_sha256": _sha256_bytes(_canonical_bytes(snapshot)),
                       "results_sha256": _sha256_bytes(_canonical_bytes(results))}
    title = str(snapshot.get("title", tr('report.study_title')))
    identifier = study_id or str(snapshot.get("study_id", "estudio"))
    tex = [TEMPLATE_DIR.joinpath("article_preamble.tex").read_text(encoding="utf-8"),
           r"\title{" + latex_escape(title) + r"}",
           r"\author{Power Machine}", r"\date{" + latex_escape(snapshot.get("date", "")) + r"}",
           r"\maketitle", tr('report.section.identification'),
           "Identificador del estudio: " + latex_escape(identifier) + r"\\",
           latex_escape(snapshot.get("objective", "Objetivo no informado")),
           tr('report.section.model')]
    model = snapshot.get("model", {})
    provenance_table = _table([("campo", tr('integration.field'), .25, "left"), ("valor", tr('integration.value'), .69, "left")],
                      [{"campo": key, "valor": value} for key, value in model.items()] or
                      [{"campo": tr('integration.model'), "valor": "No declarado"}], "Modelo y procedencia", "tab:modelo")
    tex.append("Red y escenario abiertos por el usuario. Las huellas y los tiempos se conservan en el anexo Procedencia.")
    tex.extend((tr('report.section.scenario'),
                _table([("campo", tr('integration.field'), .25, "left"), ("valor", tr('integration.value'), .69, "left")],
                       [{"campo": key, "valor": value} for key, value in (snapshot.get("scenario") or {}).items()] or
                       [{"campo": "escenario", "valor": "No declarado"}],
                       "Escenario del estudio", "tab:escenario"),
                tr('report.section.assumptions_missing')))
    assumptions = snapshot.get("assumptions") or [tr('integration.no_assumptions_declared')]
    missing = snapshot.get("missing_data") or ["No se declararon datos faltantes"]
    tex.extend((tr('report.subsection.assumptions'), r"\begin{itemize}",
                *[r"\item " + latex_escape(value) for value in assumptions], r"\end{itemize}",
                tr('report.subsection.missing'), r"\begin{itemize}",
                *[r"\item " + latex_escape(value) for value in missing], r"\end{itemize}",
                tr('report.section.coverage')))
    coverage_rows: list[dict[str, Any]] = []
    figures: list[str] = []
    historical: list[str] = []
    appendices: list[str] = []
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise TypeError("Cada resultado debe ser un diccionario")
        meta = _result_meta(item, index)
        payload = item.get("result", item)
        if not isinstance(payload, dict):
            raise TypeError("result debe ser un diccionario")
        coverage_rows.append({"modulo": meta["module"], "corrida": meta["run_id"],
                              "cobertura": item.get("coverage", payload.get("coverage", "no informada"))})
        legend = (tr('integration.run_s_freshness_s_pf_label_s')
                  % (meta["run_id"], meta["freshness"], meta["validation_label"]))
        network_model = item.get("model") or snapshot.get("network_model")
        if network_model is not None and not isinstance(network_model, dict):
            raise TypeError("model de resultado debe ser diccionario")
        coverage_rows[-1].update({"estado": meta["status"], "vigencia": meta["freshness"],
                                  "rotulo_pf": meta["validation_label"],
                              "cobertura": item.get("coverage", payload.get("coverage", "no informada"))})

        target = historical if meta["freshness"].lower() in (tr('integration.stale_a4e976'), "stale", "historical") else tex
        is_loadflow = meta["module"].lower() in ("loadflow", "lf", "flujo") or {"buses", "branches", "gens"}.issubset(payload)
        if target is historical:
            target.append(r"\subsection{" + latex_escape(meta["module"] + " " + meta["run_id"]) + "}")
        elif is_loadflow:
            tex.append(r"\section{" + latex_escape(_result_heading(meta)) + "}")
        if is_loadflow:
            state = energized_status(payload)
            buses, _ = _network_index(network_model)
            summary, loading, violations = _summary_rows(payload, network_model, snapshot.get("limits") or {})
            options = item.get("options") or snapshot.get("calculation_options", {}).get(meta["run_id"], {})
            if options:
                target.append(_table(
                    [("opcion", tr('integration.option'), .34, "left"), ("valor", tr('integration.value'), .60, "left")],
                    [{"opcion": _option_label(key, value)[0], "valor": _option_label(key, value)[1]}
                     for key, value in options.items()],
                    tr('integration.calculation_configuration'), "tab:configuracion_%02d" % (index + 1), legend))
            target.append(_table(
                [("indicador", tr('integration.indicator'), .34, "left"), ("valor", tr('integration.value'), .16, "right"),
                 ("unidad", tr('integration.unit'), .10, "left"), ("ubicacion", tr('integration.busbar'), .34, "left")],
                summary, "Resumen ejecutivo", "tab:resumen_%02d" % (index + 1),
                legend + " Estado de islas: " + state["message"]))
            has_equivalent = any(source.get("scope") == "equivalente"
                                 for group in ("buses", "gens", "loads", "branches", "branches3")
                                 for source in (network_model or {}).get(group, []))
            if has_equivalent:
                solved_ids = {row.get("id") for row in payload.get("islands", [])
                              if row.get("status") == "resuelto"}
                p_in, q_in, links = _interface_interchange(network_model, payload)
                target.append(_table(
                    [("tipo", tr('integration.internal_generation_type'), .42, "left"),
                     ("p_mw", "P MW", .20, "right"), ("q_mvar", "Q Mvar", .20, "right")],
                    _generation_by_type(payload, network_model, solved_ids),
                    tr('integration.internal_generation_by_type'), "tab:generacion_%02d" % (index + 1), legend))
                target.append(_table(
                    [("enlace", "Enlace interface", .22, "left"), ("p_entrada_mw", "P entrada MW", .26, "right"),
                     ("q_entrada_mvar", "Q entrada Mvar", .26, "right")],
                    links, "Intercambio por enlace", "tab:interface_%02d" % (index + 1),
                    legend + " Positivo entrando a la red interna desde la frontera."))
                target.append(_table(
                    [("alcance", tr('integration.scope'), .14, "left"), ("nivel_kv", "Nivel kV", .10, "right"),
                     ("u_min_pu", tr('integration.minimum_u_p_u'), .12, "right"), ("barra_min", tr('integration.minimum_busbar'), .24, "left"),
                     ("u_max_pu", tr('integration.maximum_u_p_u'), .12, "right"), ("barra_max", tr('integration.maximum_busbar'), .24, "left")],
                    _scope_voltage_rows(payload, network_model, solved_ids),
                    "Tensiones por alcance y nivel", "tab:tensiones_%02d" % (index + 1), legend))
                target.append(tr('integration.the_external_system_equivalent_is_retained_for'))
            target.append(_table(
                [("elemento", tr('integration.element_49fa9f'), .28, "left"), ("subestacion", tr('integration.substation'), .18, "left"),
                 ("nominal_kv", "Nominal kV", .10, "right"), ("loading_pct", "Carga %", .12, "right"),
                 ("p_desde_mw", "P MW", .14, "right"), ("q_desde_mvar", "Q Mvar", .14, "right")],
                loading, tr('integration.ten_most_heavily_loaded_branches'), "tab:carga_%02d" % (index + 1), legend))
            if violations:
                target.append(_table(
                    [("elemento", tr('integration.element_49fa9f'), .35, "left"), ("tipo", tr('integration.type'), .18, "left"),
                     ("valor", tr('integration.value'), .16, "right"), ("limite", tr('integration.limit'), .25, "left")],
                    violations, tr('integration.violations_against_configured_limits'),
                    "tab:violaciones_%02d" % (index + 1), legend))
            warnings = [_short_warning(warning, network_model) for warning in (payload.get("warnings") or [])[:5]]
            if warnings:
                target.append(_table(
                    [("aviso", "Aviso principal", .94, "left")],
                    [{"aviso": warning} for warning in warnings],
                    "Avisos principales", "tab:avisos_%02d" % (index + 1), legend))
            deenergized = _deenergized_rows(payload, buses)
            if deenergized:
                target.append(_table(
                    [("isla", tr('integration.island'), .10, "right"), ("barras", tr('integration.busbars_6ebb1c'), .12, "right"),
                     ("referencia", tr('integration.status'), .22, "left"), ("muestra", "Muestra de barras", .50, "left")],
                    deenergized, tr('integration.de_energized_islands'), "tab:desenergizadas_%02d" % (index + 1),
                    legend + " Estas barras no se mezclan con las tablas resueltas."))
            target.append("El estado de las islas energizadas es: " +
                          latex_escape(state["message"]) + ".")
            for table_name, rows, columns in _loadflow_tables(payload, network_model):
                csv_name = "%02d_%s_%s.csv" % (index + 1, _safe_slug(meta["run_id"]), table_name)
                raw_group = {"barras": "buses", "ramas": "branches", "generadores": "gens"}[table_name]
                _write_csv(csv_dir / csv_name, payload.get(raw_group, []))
                appendices.append(_table(columns, rows, "Tabla completa de %s" % table_name,
                                         "tab:anexo_%s_%s" % (index + 1, table_name), legend))
            _write_csv(csv_dir / ("%02d_%s_cargas.csv" % (index + 1, _safe_slug(meta["run_id"]))),
                       payload.get("loads", []))
        else:
            rendered, envelope_figures = _render_envelope(item, meta, snapshot, network_model, index + 1, directory)
            target.extend(rendered)
            figures.extend(envelope_figures)
            envelope_path = directory / "sobres" / ("%02d_%s.json" % (index + 1, _safe_slug(meta["run_id"])))
            envelope_path.parent.mkdir(exist_ok=True)
            envelope_path.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                     encoding="utf-8", newline="\n")
    provenance_extra = []
    for number, item in enumerate(results, 1):
        provenance_extra.append({"campo": "Corrida %d" % number, "valor": item.get("run_id", tr('integration.unnamed'))})
        source = item.get("result", {})
        for key, label in (("schema", "Esquema"), ("model_sha256", "Huella del modelo"), ("study_sha256", "Huella del estudio"), ("elapsed_s", tr('integration.duration_s'))):
            if source.get(key) is not None:
                provenance_extra.append({"campo": label + " de " + item.get("run_id", "corrida"), "valor": source[key]})
    tex.extend((tr('report.section.diagnostics'), r"\begin{itemize}",
                *[r"\item " + latex_escape(value) for value in (snapshot.get("diagnostics") or [tr('integration.no_diagnostics_reported')])],
                r"\end{itemize}", tr('report.section.conclusions'),
                *[latex_escape(value) for value in (snapshot.get("conclusions") or ["Las conclusiones quedan limitadas a la evidencia de este informe."])]))
    tex.extend((tr('report.section.limitations'),
                *[latex_escape(value) for value in (snapshot.get("limitations") or [tr('integration.calculations_have_no_powerfactory_validation'), tr('integration.synthetic_data_are_used_only_for_numerical')])]))
    tex.extend((tr('report.section.pf_pending'),
                tr('integration.references_with_matching_cases_and_options_are'),
                r"\appendix", tr('report.section.provenance'), provenance_table,
                _table([("campo", tr('integration.field'), .40, "left"), ("valor", tr('integration.value'), .54, "left")], provenance_extra, "Procedencia de las corridas", "tab:procedencia")))
    if historical:
        tex.extend((r"\appendix", tr('report.section.historical'),
                    "Los resultados de este anexo no forman parte de las conclusiones del caso activo.", *historical))
    if appendices:
        tex.extend((tr('report.section.full_tables'), *appendices))
    tex.append(TEMPLATE_DIR.joinpath("article_end.tex").read_text(encoding="utf-8"))
    tex_path = directory / "informe.tex"
    tex_path.write_text("\n".join(tex) + "\n", encoding="utf-8", newline="\n")
    compilation = compile_latex(directory) if compile_pdf else {"status": "not_requested", "engine": None, "pdf": None, "log": None}
    outputs = {path.relative_to(directory).as_posix(): _sha256_file(path)
               for path in directory.rglob("*") if path.is_file() and path.name != "manifest.json"}
    manifest = {"schema": MANIFEST_SCHEMA, "study_id": identifier,
                "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "inputs": manifest_inputs, "outputs": outputs, "latex": compilation,
                "latex_packages": list(LOCAL_PACKAGES)}
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8", newline="\n")
    return {"output_dir": str(directory), "tex": str(tex_path), "manifest": str(manifest_path),
            "csv": [str(path) for path in sorted(csv_dir.glob("*.csv"))], "latex": compilation}
