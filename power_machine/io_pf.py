"""Read portable PowerFactory CSV files without electrical conversions."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = []
    for row in rows:
        if row.get("status") == "NULL":
            continue
        if "value_json" in row and row["value_json"] != "":
            row["value"] = json.loads(row["value_json"])
        result.append(row)
    return result


def load_scope(directory: str | Path) -> dict[str, list[dict]]:
    """Read all CSV tables in a scope, skipping rows with status=NULL."""
    directory = Path(directory)
    return {path.stem: _read_csv(path) for path in sorted(directory.glob("*.csv"))}


@dataclass
class Package:
    """Tables, global indexes and fingerprints of the source files read."""

    root: Path
    scopes: dict[str, dict[str, list[dict]]]
    by_id: dict[str, dict]
    by_path: dict[str, dict]
    sha256_inputs: dict[str, str]
    interface_map: list[dict]


def load_package(package_root: str | Path, *, scopes=None, interface_map=None) -> Package:
    """Read discovered or explicit scopes and an optional boundary map."""
    root = Path(package_root).resolve()
    requested_scopes = scopes
    scopes = {}
    by_id = {}
    by_path = {}
    digests = {}
    portable = root / "04_csv_portable" if (root / "04_csv_portable").is_dir() else root
    directories = [portable] if (portable / "objects.csv").is_file() else sorted(p for p in portable.iterdir() if p.is_dir() and (p / "objects.csv").is_file())
    if requested_scopes is not None:
        directories = [portable / scope for scope in requested_scopes]
    if not directories:
        raise FileNotFoundError("No objects.csv tables found")
    for directory in directories:
        scope = directory.name
        scopes[scope] = load_scope(directory)
        for path in sorted(directory.glob("*.csv")):
            digests[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        for obj in scopes[scope]["objects"]:
            object_id, relative_path = obj["object_id"], obj["relative_path"]
            if object_id in by_id and by_id[object_id]["relative_path"] != relative_path:
                raise ValueError(f"Duplicate object_id with different paths: {object_id}")
            if relative_path in by_path and by_path[relative_path]["object_id"] != object_id:
                raise ValueError(f"Duplicate relative_path with different IDs: {relative_path}")
            by_id.setdefault(object_id, obj)
            by_path.setdefault(relative_path, obj)
    interface_path = Path(interface_map) if interface_map is not None else root / "interface_map.csv"
    if interface_map is not None and not interface_path.is_absolute():
        interface_path = root / interface_path
    interface_map = _read_csv(interface_path) if interface_path.is_file() else []
    if interface_path.is_file():
        digests[interface_path.relative_to(root).as_posix()] = hashlib.sha256(interface_path.read_bytes()).hexdigest()
    return Package(root, scopes, by_id, by_path, digests, interface_map)
