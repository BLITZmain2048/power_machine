"""Cotejo E39-PRO-01 con corrientes locales REF, independiente del modelo de red.

Corrientes en A primarios/secundarios y tiempos puros de relé en s.
La tolerancia relativa es 0.01, con el tiempo nativo como denominador.
Los perfiles normativos proceden del estudio protection, no de una norma íntegra.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
from ..resources import output_root, writable_root
import struct

from ..protection import RelayStage, operating_time, standard_curve


WORKSPACE = output_root()
DEFAULT_SOURCE = None
DEFAULT_OUTPUT = WORKSPACE / "comparison_results/ieee39_ref/E39-PRO-01"
TIME_REL_TOL = 0.01
NO_TRIP_SENTINEL_S = 9999.999
PROFILES = {"IEC": ("iec", "normal_inverse", 0.2),
            "IEEE": ("ieee", "very_inverse", 0.5)}
INPUT_NAMES = (
    "LEEME.txt", "assessment.json", "native_curve_points.csv",
    "actual_fault_relay_times.csv", "offline_actual_fault_audit.csv",
    "offline_curve_audit.csv", "offline_audit.json", "configuration_failure.json",
    "first_calculation.json", "native_curve_validation.json",
    "protection_curves_manifest.json",
)


def read_csv(path):
    """Preserva celdas vacías, no sustituye por cero."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def csv_number(row, key, *, optional=False):
    value = row[key]
    if value in (None, "") and optional:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{key}: falta magnitud numérica") from exc
    if not math.isfinite(result):
        raise ValueError(f"{key}: magnitud no finita")
    return result


def attr(block, key, unit=None):
    entry = block[key]
    value = entry["value"]
    if value is None:
        raise ValueError(f"{key}: ajuste ausente")
    if unit is not None and entry["unit"].strip() != unit:
        raise ValueError(f"{key}: unidad incompatible con {unit}")
    return value


def relative_path(full_path):
    """Retira solo el prefijo hasta el proyecto IntPrj explícito."""
    marker = ".IntPrj\\"
    if marker not in full_path:
        raise ValueError("Ruta REF sin proyecto IntPrj")
    return full_path.split(marker, 1)[1]


def check_primary_pickup(raw_primary_a, effective_primary_a):
    """cpIpset puede ser float32 de Ipsetr por ratio, comprueba ese redondeo exacto."""
    rounded_a = struct.unpack("f", struct.pack("f", effective_primary_a))[0]
    if raw_primary_a not in (effective_primary_a, rounded_a):
        raise ValueError("Pickup discordante con TC y redondeo float32")


def load_settings(source):
    """Extrae ajustes e identidades, sin publicar los snapshots completos."""
    native = read_json(Path(source) / "native_curve_validation.json")
    first = read_json(Path(source) / "first_calculation.json")
    first_relays = {r["label"]: r for r in first["relays"]}
    output = {}
    for item in native["settings"]:
        label = item["label"]
        if label in output:
            raise ValueError("Ajuste de relé duplicado")
        family, name, nominal_dial = PROFILES[label]
        curve = standard_curve(family, name)
        params = item["curve_parameters"]
        matrix = attr(params, "vmat")
        expected_matrix = ([[0.14], [0.02], [1.0]] if family == "iec" else
                           [[19.61], [2.0], [1.0], [0.491]])
        if matrix != expected_matrix or attr(params, "i_type") != (1 if family == "iec" else 2):
            raise ValueError("Definición REF distinta del perfil protection, requiere revisión")
        first_item = first_relays[label]
        stage_attrs = first_item["stage_attrs"]
        for section, original in (("curve_parameters", "curve_attrs"),
                                  ("stage_settings", "stage_attrs"),
                                  ("ct_settings", "ct_attrs")):
            for key, entry in item[section].items():
                if entry["value"] != attr(first_item[original], key):
                    raise ValueError(f"Snapshots discordantes: {label}/{key}")
        if attr(stage_attrs, "Tadder", "s") != 0 or attr(stage_attrs, "minresptime", "s") != 0:
            raise ValueError("Modificadores de tiempo no cubiertos por este ensayo")
        if attr(stage_attrs, "outserv") != 0 or attr(first_item["relay_attrs"], "outserv") != 0:
            raise ValueError("Relé o etapa fuera de servicio")
        ct_primary_a = attr(item["ct_settings"], "ptapset", "A")
        ct_secondary_a = attr(item["ct_settings"], "stapset", "A")
        ct_ratio = ct_primary_a / ct_secondary_a
        raw_pickup_a = attr(item["stage_settings"], "cpIpset", "pri.A")
        pickup_secondary_a = attr(item["stage_settings"], "Ipsetr", "sec.A")
        pickup_a = pickup_secondary_a * ct_ratio
        check_primary_pickup(raw_pickup_a, pickup_a)
        stage_path = item["stage"]["path"]
        relay_path = first_item["relay"]["path"]
        if stage_path.rsplit("\\", 1)[0] != relay_path:
            raise ValueError("Etapa ajena al relé de referencia")
        stage = RelayStage(
            attr(stage_attrs, "oid_"), relative_path(stage_path),
            attr(first_item["relay_attrs"], "oid_"), None, None, pickup_a,
            attr(item["stage_settings"], "Tpset"), curve, True, "phase", ct_ratio,
        )
        output[label] = {
            "stage": stage, "source_full_path": stage_path,
            "relay_full_path": relay_path, "curve_full_path": item["curve"]["path"],
            "curve_name": item["curve"]["name"], "nominal_dial": nominal_dial,
            "ct_primary_rated_a": ct_primary_a, "ct_secondary_rated_a": ct_secondary_a,
            "secondary_pickup_a": pickup_secondary_a,
            "ref_primary_pickup_raw_a": raw_pickup_a,
            "pickup_conversion": "Ipsetr [sec.A] por ptapset/stapset, cpIpset [pri.A] se conserva como dato redondeado",
            "curve_min_m": attr(params, "imin", "I/Ip"),
            "curve_max_m": attr(params, "imax", "I/Ip"),
            "curve_min_time_s": attr(params, "tmin", "s"),
            "minimum_response_time_s": attr(item["stage_settings"], "minresptime", "s"),
            "breaker_opening_setting_s": attr(item["output_logic_settings"], "Tbrkopen", "s"),
            "instantaneous_time_s": None, "instantaneous_status": "sin validar",
        }
    if set(output) != set(PROFILES):
        raise ValueError("Falta un perfil IEC/IEEE")
    return output


def formula_time(stage, current_primary_a, dial):
    """Evaluación directa del perfil, independiente del algoritmo expm1 de power_machine."""
    multiple = current_primary_a / stage.pickup_primary_a
    if multiple <= 1:
        return None
    c = stage.curve.coefficients
    if stage.curve.family == "iec":
        return dial * c["K"] / (multiple ** c["alpha"] - 1)
    return dial * (c["A"] / (multiple ** c["p"] - 1) + c["B"])


def compare_rows(rows, settings, *, curve_points):
    """Compara cada etapa y relé con corriente local, sin consumir tiempos analíticos REF."""
    output = []
    seen = set()
    for line_number, row in enumerate(rows, 2):
        label = row["relay"]
        config = settings[label]
        base_stage = config["stage"]
        current_a = csv_number(row, "primary_current_A")
        raw_pickup_a = csv_number(row, "primary_pickup_A")
        secondary_a = csv_number(row, "secondary_current_A")
        pickup_secondary_a = csv_number(row, "secondary_pickup_A")
        pickup_a = pickup_secondary_a * base_stage.ct_ratio
        if current_a < 0 or pickup_a <= 0:
            raise ValueError("Corriente o pickup fuera de dominio")
        if not math.isclose(secondary_a * base_stage.ct_ratio, current_a, rel_tol=1e-12):
            raise ValueError("Corriente primaria/secundaria discordante con TC")
        check_primary_pickup(raw_pickup_a, pickup_a)
        if not curve_points and (pickup_a != base_stage.pickup_primary_a or raw_pickup_a != config["ref_primary_pickup_raw_a"]):
            raise ValueError("Pickup no restaurado en ensayo de falla")
        stage = replace(base_stage, pickup_primary_a=pickup_a)
        multiple = current_a / pickup_a
        if not math.isclose(multiple, csv_number(row, "current_multiple"), rel_tol=1e-12):
            raise ValueError("M no coincide con I/Ip")
        if row["curve_name"] != config["curve_name"]:
            raise ValueError("Característica CSV ajena al ajuste")
        for field, setting in (("curve_min_M", "curve_min_m"), ("curve_max_M", "curve_max_m"),
                               ("curve_min_time_s", "curve_min_time_s")):
            if csv_number(row, field) != config[setting]:
                raise ValueError("Límites CSV discordantes con característica")
        target_m = csv_number(row, "target_M") if curve_points else None
        key = (label, int(row["fault_bus"]), target_m)
        if key in seen:
            raise ValueError("Punto CSV duplicado")
        seen.add(key)
        ref_stage_s = csv_number(row, "native_stage_time_s")
        ref_relay_s = csv_number(row, "native_relay_time_s")
        if ref_stage_s < 0 or ref_relay_s < 0:
            raise ValueError("Tiempo REF negativo")
        ref_operates = ref_stage_s != NO_TRIP_SENTINEL_S
        if ref_operates != (ref_relay_s != NO_TRIP_SENTINEL_S):
            raise ValueError("Estado de etapa y relé discordante")
        power_machine_s = operating_time(stage, current_a)
        nominal_s = formula_time(stage, current_a, config["nominal_dial"])
        effective_s = formula_time(stage, current_a, stage.time_dial)
        raw_pickup_s = operating_time(replace(stage, pickup_primary_a=raw_pickup_a), current_a)
        inside = config["curve_min_m"] <= multiple <= config["curve_max_m"]
        if row["within_curve_domain"] not in ("True", "False"):
            raise ValueError("Dominio CSV ausente")
        if inside != (row["within_curve_domain"] == "True"):
            raise ValueError("Dominio CSV discordante con M")
        comparable = inside and ref_operates and power_machine_s is not None
        delta_s = ref_stage_s - power_machine_s if comparable else None
        rel_error = abs(delta_s) / ref_stage_s if comparable and ref_stage_s > 0 else None
        relay_rel_error = abs(ref_relay_s - power_machine_s) / ref_relay_s if comparable and ref_relay_s > 0 else None
        nominal_error = abs(ref_stage_s - nominal_s) / ref_stage_s if comparable and ref_stage_s > 0 else None
        operation_matches = ref_operates == (power_machine_s is not None)
        time_pass = (rel_error <= TIME_REL_TOL and relay_rel_error <= TIME_REL_TOL) if rel_error is not None else None
        if not ref_operates and multiple <= 1 and operation_matches:
            interpretation = "sin arranque, 9999.999 es centinela"
            accepted = True
        elif not comparable:
            interpretation = "sin validar, fuera del dominio o estado discordante"
            accepted = False
        else:
            accepted = operation_matches and time_pass
            if rel_error <= 1e-12 and stage.time_dial != config["nominal_dial"]:
                interpretation = "redondeo del dial REF, fórmula con ajuste efectivo coincide"
            elif rel_error <= 1e-12:
                interpretation = "coincide con fórmula, residuo de aritmética flotante"
            else:
                interpretation = "diferencia sin resolver, revisar definición o límites REF"
        output.append({
            "source_csv": "native_curve_points.csv" if curve_points else "actual_fault_relay_times.csv",
            "source_row": line_number, "relay": label, "object_id": stage.object_id,
            "relative_path": stage.relative_path, "relay_id": stage.relay_id,
            "source_full_path": config["source_full_path"],
            "fault_bus": int(row["fault_bus"]), "line": row["line"],
            "target_m": target_m, "primary_current_a": current_a, "secondary_current_a": secondary_a,
            "primary_pickup_a": pickup_a, "secondary_pickup_a": pickup_secondary_a,
            "ref_primary_pickup_raw_a": raw_pickup_a,
            "ref_minus_effective_pickup_a": raw_pickup_a-pickup_a,
            "current_multiple": multiple, "target_m_error": abs(multiple-target_m) if target_m is not None else None,
            "effective_dial": stage.time_dial, "nominal_dial": config["nominal_dial"],
            "ref_stage_time_raw_s": ref_stage_s, "ref_relay_time_raw_s": ref_relay_s,
            "ref_operating": ref_operates, "power_machine_operating": power_machine_s is not None,
            "power_machine_time_s": power_machine_s, "formula_effective_time_s": effective_s,
            "formula_nominal_time_s": nominal_s,
            "time_using_raw_ref_pickup_s": raw_pickup_s,
            "ref_minus_power_machine_s": delta_s, "relative_error": rel_error,
            "relative_error_percent": rel_error * 100 if rel_error is not None else None,
            "relay_relative_error": relay_rel_error, "nominal_relative_error": nominal_error,
            "relative_tolerance": TIME_REL_TOL, "within_curve_domain": inside,
            "minimum_time_active": effective_s <= config["curve_min_time_s"] if comparable else None,
            "operation_matches": operation_matches, "time_pass": time_pass,
            "accepted": accepted, "interpretation": interpretation,
        })
    return output


def compare_order(faults):
    """Orden de tiempos estáticos por falla, sin simular aperturas sucesivas."""
    output = []
    for bus in sorted({r["fault_bus"] for r in faults}):
        rows = [r for r in faults if r["fault_bus"] == bus]
        if {r["relay"] for r in rows} != set(PROFILES) or len(rows) != 2:
            raise ValueError("Falla sin ambos relés")
        ref_rows = sorted((r for r in rows if r["ref_operating"]), key=lambda r: r["ref_relay_time_raw_s"])
        power_machine_rows = sorted((r for r in rows if r["power_machine_operating"]), key=lambda r: r["power_machine_time_s"])
        ref_order = [r["object_id"] for r in ref_rows]
        power_machine_order = [r["object_id"] for r in power_machine_rows]
        # Compara también igualdad de tiempos, para no convertir empates en precedencia.
        tie_matches = True
        if len(ref_rows) == len(power_machine_rows) == 2 and set(ref_order) == set(power_machine_order):
            tie_matches = ((ref_rows[0]["ref_relay_time_raw_s"] == ref_rows[1]["ref_relay_time_raw_s"]) ==
                           (power_machine_rows[0]["power_machine_time_s"] == power_machine_rows[1]["power_machine_time_s"]))
        output.append({
            "fault_bus": bus, "ref_order": ">".join(r["relay"] for r in ref_rows),
            "power_machine_order": ">".join(r["relay"] for r in power_machine_rows),
            "ref_order_object_ids": ">".join(map(str, ref_order)),
            "power_machine_order_object_ids": ">".join(map(str, power_machine_order)),
            "same_operating_set": set(ref_order) == set(power_machine_order),
            "same_order": ref_order == power_machine_order and tie_matches,
            "scope": "tiempos estáticos con corrientes locales REF, secuencia de aperturas sin validar",
        })
    return output


def compare(source):
    """Lee solo el ensayo PRO, calcula resultados y huellas sin escribir."""
    source = Path(source).resolve()
    settings = load_settings(source)
    curves = compare_rows(read_csv(source / "native_curve_points.csv"), settings, curve_points=True)
    faults = compare_rows(read_csv(source / "actual_fault_relay_times.csv"), settings, curve_points=False)
    if {(r["relay"], r["target_m"]) for r in curves} != {(label, m) for label in PROFILES for m in (2, 5, 10)} or len(curves) != 6:
        raise ValueError("Malla nativa incompleta")
    if {r["fault_bus"] for r in faults} != {2, 3, 16, 19}:
        raise ValueError("Faltan fallas del ensayo")
    order = compare_order(faults)
    summaries = []
    for kind, rows in (("curve", curves), ("fault", faults)):
        for label in PROFILES:
            items = [r for r in rows if r["relay"] == label]
            measured = [r for r in items if r["relative_error"] is not None]
            summaries.append({
                "kind": kind, "relay": label, "points": len(items), "timed_points": len(measured),
                "no_pickup_points": sum(not r["ref_operating"] for r in items),
                "max_absolute_error_s": max((abs(r["ref_minus_power_machine_s"]) for r in measured), default=None),
                "max_relative_error_percent": max((r["relative_error_percent"] for r in measured), default=None),
                "max_nominal_relative_error_percent": max((100*r["nominal_relative_error"] for r in measured), default=None),
                "all_pass": all(r["accepted"] for r in items),
            })
    settings_rows = []
    for label, config in settings.items():
        stage = config["stage"]
        settings_rows.append({
            "relay": label, "object_id": stage.object_id, "relative_path": stage.relative_path,
            "relay_id": stage.relay_id, "identity_origin": "REF oid_ original",
            "source_full_path": config["source_full_path"], "relay_full_path": config["relay_full_path"],
            "curve_full_path": config["curve_full_path"], "curve_name": config["curve_name"],
            "standard": stage.curve.source["standard"], "edition": stage.curve.source["edition"],
            "coefficients": json.dumps(stage.curve.coefficients, sort_keys=True),
            "primary_pickup_a": stage.pickup_primary_a, "effective_dial": stage.time_dial,
            "protocol_pickup_multiple_in": 1.2,
            "pickup_convention_source": "native_curve_validation.json/protocol_nominal_current_convention",
            **{k: v for k, v in config.items() if k not in ("stage", "source_full_path", "relay_full_path", "curve_full_path", "curve_name")},
        })
    return {
        "curves": curves, "faults": faults, "order": order, "settings": settings_rows,
        "summary": summaries,
        "all_pass": all(r["accepted"] for r in curves+faults) and
                    all(r["same_operating_set"] and r["same_order"] for r in order),
        "provenance": {
            "source": str(source), "time_relative_tolerance": TIME_REL_TOL,
            "error_denominator": "tiempo nativo REF", "current_basis": "corriente local del TC exportada por REF",
            "pickup_basis": "Ipsetr [sec.A] por ratio del TC, cpIpset redondeado a float32 conservado por separado",
            "inputs": [{"path": name, "sha256": hashlib.sha256((source/name).read_bytes()).hexdigest()} for name in INPUT_NAMES],
            "normative_evidence": "Perfiles del estudio protection, norma íntegra sin consultar, sin uso de red",
            "network_current_comparison": "sin validar, depende del modelo REF-nativo de PFC-MODEL",
            "instantaneous_stage": "sin validar, no hay evaluación nativa de etapa 50 exportada",
            "ct_saturation": "sin validar, solo relación ideal 2000/1 comprobada",
            "graph_source": read_json(source / "protection_curves_manifest.json")["transforms"],
            "assessment_status": read_json(source / "assessment.json")["status"],
            "historical_configuration_error": read_json(source / "configuration_failure.json")["error"],
        },
    }


def write_tables(result, output=None):
    """Escribe exclusivamente dentro de la raíz, sin borrar ni sobrescribir archivos."""
    from ..resources import confined_output
    output = Path(output).resolve() if output is not None else writable_root() / "comparacion_ref" / "proteccion"
    tables = {name + ".csv": result[name] for name in ("curves", "faults", "order", "settings", "summary")}
    paths = [output / name for name in (*tables, "provenance.json")]
    if any(path.exists() for path in paths):
        raise FileExistsError("Hay resultados previos, elija otra carpeta dentro de la raíz")
    output.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with (output/name).open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    with (output/"provenance.json").open("x", encoding="utf-8") as handle:
        json.dump({"all_pass": result["all_pass"], **result["provenance"]}, handle,
                  ensure_ascii=False, indent=2, allow_nan=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    result = compare(args.source)
    write_tables(result, args.output)
    print(json.dumps({"all_pass": result["all_pass"], "summary": result["summary"], "order": result["order"]}, ensure_ascii=False, indent=2))
    return 0 if result["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
