'Schema support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import csv
import hashlib
import json
from pathlib import Path

SCHEMA = "pf-dynamic-v1"
FILES = ("objects.csv", "attributes.csv", "references.csv")
STATUSES = {"PRESENT", "NULL", "UNAVAILABLE", "ERROR"}
BLKSIG_FIELDS = ("pnodfrom", "pnodto", "inodfrom", "inodto", "iconfrom",
                 "iconto", "GetFromSigName()", "GetToSigName()")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_dynamic_export(directory: Path) -> dict:
    'Read UTF-8 CSV and verify identity, integrity and connection coverage.'
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise _coded_error(ValueError, 'invalid_schema_data', 'Unknown dynamic contract version')
    hashes = manifest.get("sha256", {})
    for name in FILES:
        if name not in hashes:
            raise _coded_error(ValueError, 'missing_schema_data', f'Missing SHA-256: {name}')
    for name, expected_hash in hashes.items():
        if Path(name).name != name or sha256_file(directory / name) != expected_hash:
            raise _coded_error(ValueError, 'invalid_schema_data', f'SHA-256 distinto: {name}')
    objects, attrs, refs = (read_csv(directory / name) for name in FILES)
    ids = [row["object_id"] for row in objects]
    if len(ids) != len(set(ids)):
        raise _coded_error(ValueError, 'invalid_schema_data', 'Duplicate object_id in objects.csv')
    known = set(ids)
    for row in attrs:
        if row["object_id"] not in known:
            raise _coded_error(ValueError, 'missing_schema_data', 'Missing object attribute')
        if row["status"] not in STATUSES:
            raise _coded_error(ValueError, 'invalid_schema_data', f"Invalid state: {row['status']}")
        if row["status"] == "PRESENT":
            if row["value_json"] == "":
                raise _coded_error(ValueError, 'missing_schema_data', 'PRESENT requires value_json')
            json.loads(row["value_json"])
        elif row["value_json"]:
            raise _coded_error(ValueError, 'missing_schema_data', 'Missing attribute has a value')
    for row in refs:
        if row["source_id"] not in known:
            raise _coded_error(ValueError, 'missing_schema_data', 'Reference source is missing')
        if not row["position"].isdigit():
            raise _coded_error(ValueError, 'invalid_schema_data', 'Invalid reference position')
    by_key = {(row["object_id"], row["attribute"]): row for row in attrs}
    missing = []
    for row in objects:
        if row["class"] != "BlkSig":
            continue
        for field in BLKSIG_FIELDS:
            value = by_key.get((row["object_id"], field))
            if value is None or value["status"] not in {"PRESENT", "NULL"}:
                missing.append((row["object_id"], field))
    return {"manifest": manifest, "objects": objects, "attributes": attrs,
            "references": refs, "blksig_complete": not missing and
            any(row["class"] == "BlkSig" for row in objects),
            "blksig_missing": missing}
