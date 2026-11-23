"Explicit data, numerical methods and traceable results for contingency studies."
from __future__ import annotations

from dataclasses import asdict
import hashlib
import itertools
import json

from .schema import BudgetExceeded, ContingencyOptions, Outage

GROUPS = ("buses", "branches", "branches3", "gens", "switches", "loads", "shunts")
CLASSES = {"ElmLne", "ElmZpu", "ElmTr2", "ElmTr3", "ElmSym", "ElmGenstat", "ElmPvsys"}


def index_model(model: dict) -> dict:
    index = {}
    for group in GROUPS:
        for item in model.get(group, []):
            key = item["id"]
            if not isinstance(key, str) or not key or key in index:
                raise ValueError('[invalid_input] ' + f"Empty or duplicate element ID: {key}")
            index[key] = item
    return index


def identity(item: dict) -> dict:
    return {"model_id": item.get("id"),
            "object_id": item.get("object_id", item.get("id")),
            "relative_path": item.get("relative_path", item.get("path"))}


def enumerate_outages(model: dict, *, groups=None) -> tuple[Outage, ...]:
    "List physical in-service elements and explicit groups. Couplers require an explicit request.\n\nGroups replace their physical members. Duplicates are removed and overlaps rejected."
    index = index_model(model)
    tr3_ids = {item["id"] for item in model.get("branches3", [])}
    candidates = {}
    for group in ("branches", "branches3", "gens"):
        for item in model.get(group, []):
            if (item.get("outserv", 0) == 1 or item.get("class") not in CLASSES
                    or item.get("virtual", False)
                    or item.get("physical_parent_id", item.get("parent_id")) in tr3_ids):
                continue
            ident = identity(item)
            # Los IDs de ejecución son las claves reales del contrato model-v1.
            candidates[item["id"]] = Outage(item["id"], ident["relative_path"], item["class"])
    records = groups or []
    if isinstance(records, dict):
        records = [dict(object_id=key, member_ids=value, kind="group", relative_path=None)
                   for key, value in records.items()]
    declared = []
    seen_members = set()
    declared_ids = {}
    for record in sorted(records, key=lambda r: r.object_id if isinstance(r, Outage) else r["object_id"]):
        outage = record if isinstance(record, Outage) else Outage(
            record["object_id"], record.get("relative_path"), record.get("kind", "group"),
            tuple(record.get("member_ids", ())))
        members = tuple(sorted(set(outage.physical_ids)))
        if not isinstance(outage.object_id, str) or not outage.object_id:
            raise ValueError('[invalid_input] ' + "Empty group ID")
        if outage.object_id in declared_ids:
            if declared_ids[outage.object_id] == frozenset(members):
                continue
            raise ValueError('[invalid_input] ' + f"Duplicate group ID: {outage.object_id}")
        declared_ids[outage.object_id] = frozenset(members)
        for key in members:
            if key not in index:
                raise ValueError('[invalid_input] ' + f"Unknown physical member: {key}")
            item = index[key]
            if item.get("outserv", 0) == 1:
                raise ValueError('[invalid_input] ' + f"Group member is out of service: {key}")
            if item.get("class") not in CLASSES | {"ElmCoup", "StaSwitch"}:
                raise ValueError('[invalid_input] ' + f"Element class cannot be removed: {key}")
            if item.get("virtual", False) or item.get("physical_parent_id", item.get("parent_id")) in tr3_ids:
                raise ValueError('[invalid_input] ' + f"Virtual outage is not allowed: {key}")
        member_set = frozenset(members)
        if member_set in seen_members:
            continue
        if any(member_set & other for other in seen_members):
            raise ValueError('[invalid_input] ' + "Physical groups have overlapping members")
        seen_members.add(member_set)
        path = outage.relative_path
        kind = outage.kind
        if len(members) == 1 and members[0] == outage.object_id and members[0] in index:
            path = path if path is not None else identity(index[members[0]])["relative_path"]
            kind = index[members[0]]["class"]
        declared.append(Outage(outage.object_id, path, kind, members))
        for key in members:
            candidates.pop(key, None)
    for outage in declared:
        if outage.object_id in candidates:
            raise ValueError('[invalid_input] ' + f"Group ID collides with another element: {outage.object_id}")
        candidates[outage.object_id] = outage
    return tuple(candidates[key] for key in sorted(candidates))


def total_cases(n: int, options: ContingencyOptions) -> int:
    return sum(n if order == 1 else n * (n - 1) // 2 for order in options.orders)


def enumerate_cases(outages: tuple[Outage, ...],
                    options: ContingencyOptions) -> tuple[tuple[Outage, ...], ...]:
    ordered = tuple(sorted(outages, key=lambda item: item.object_id))
    ids = [item.object_id for item in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError('[invalid_input] ' + "Duplicate outage IDs")
    used_physical_ids = set()
    for outage in ordered:
        if used_physical_ids.intersection(outage.physical_ids):
            raise ValueError('[invalid_input] ' + "Two outages contain the same physical element")
        used_physical_ids.update(outage.physical_ids)
    expected = total_cases(len(ordered), options)
    if expected > options.max_cases:
        raise BudgetExceeded(expected, options.max_cases)
    return tuple(case for order in options.orders for case in itertools.combinations(ordered, order))


def case_id(outages: tuple[Outage, ...]) -> str:
    raw = json.dumps([asdict(o) for o in sorted(outages, key=lambda o: o.object_id)],
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"N{len(outages)}_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
