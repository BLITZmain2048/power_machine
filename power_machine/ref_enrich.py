"""Completa atributos de modelo ausentes desde el volcado REF de solo lectura."""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path


_MODEL_ATTRIBUTES = {
    "ElmTr2": {"ntnum", "nntap", "nntap2", "pQlim"},
    "ElmTr3": {"ntnum", "n3tap_h", "n3tap_m", "n3tap_l"},
    "ElmSym": {"q_min", "q_max", "pQlim"},
    "ElmGenstat": {"q_min", "q_max", "pQlim"},
    "ElmLod": {"scale0", "mode_inp"},
    "ElmShnt": {"ncapa", "ncapx"},
    "ElmLne": {"nlnum"},
    "TypTr2": {"itrdr", "itrdl", "ntpmn", "ntpmx", "ntpmn2", "ntpmx2",
                 "pcutr", "curmg", "pfe", "dutap", "nntap0", "phitr"},
    "TypTr3": {"pcut3_h", "pcut3_m", "pcut3_l", "uktrr3_h", "uktrr3_m",
                 "uktrr3_l", "curm3", "pfe", "du3tp_h", "du3tp_m", "du3tp_l",
                 "n3tp0_h", "n3tp0_m", "n3tp0_l", "n3tmn_h", "n3tmn_m",
                 "n3tmn_l", "n3tmx_h", "n3tmx_m", "n3tmx_l",
                 "ph3tr_h", "ph3tr_m", "ph3tr_l"},
    "TypLod": {"aP", "bP", "cP", "aQ", "bQ", "cQ",
               "e_aP", "e_bP", "e_cP", "e_aQ", "e_bQ", "e_cQ"},
    "TypSym": {"sgn"},
}


def enrich_model(model: dict, attributes_path: str | Path) -> tuple[dict, dict]:
    """Agrega solo valores PRESENT ausentes; nunca convierte NULL en cero.

    Retorna modelo copiado y conteos de incorporaciones/atributos no disponibles.
    Las entradas originales conservan prioridad. La procedencia se guarda en
    ``enrichment`` de cada elemento o tipo asociado.
    """
    result = copy.deepcopy(model)
    by_id = {item["id"]: item for group in ("buses", "switches", "branches", "branches3",
                                           "shunts", "loads", "gens", "controllers", "other_elements")
             for item in result.get(group, [])}
    by_type = {}
    for item in by_id.values():
        if item.get("type_id"):
            by_type.setdefault(item["type_id"], []).append(item)
    added = 0
    unavailable = []
    with Path(attributes_path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            cls, key = row["class"], row["attribute"]
            if key not in _MODEL_ATTRIBUTES.get(cls, ()):
                continue
            targets = ([(by_id[row["object_id"]], "params", "units")]
                       if row["object_id"] in by_id and by_id[row["object_id"]]["class"] == cls
                       else [(item, "type_params", "type_units") for item in by_type.get(row["object_id"], [])
                             if item.get("type_class") == cls])
            if not targets:
                continue
            if row["status"] != "PRESENT" or not row["value_json"]:
                unavailable.append({"object_id": row["object_id"], "attribute": key,
                                    "status": row["status"]})
                continue
            value = json.loads(row["value_json"])
            if value is None:
                continue
            for item, pkey, ukey in targets:
                if item.setdefault(pkey, {}).get(key) is not None:
                    continue
                item[pkey][key] = value
                if row["unit"]:
                    item.setdefault(ukey, {})[key] = row["unit"]
                provenance = item.setdefault("enrichment", {"params": [], "type_params": [],
                                                             "sources": {}})
                provenance[pkey].append(key)
                provenance["sources"].setdefault(pkey, {})[key] = str(attributes_path)
                added += 1
    return result, {"added_values": added, "unavailable": unavailable}
