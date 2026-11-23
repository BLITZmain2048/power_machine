"""Pruebas del contrato de datos RMS sin licencia PowerFactory."""
import csv
import json

import pytest

from power_machine.dynamics.schema import FILES, read_dynamic_export, sha256_file


def fixture_export(tmp_path, *, state="PRESENT"):
    rows = {
        "objects.csv": [{"object_id": "pf_a", "relative_path": "x", "class": "BlkSig"}],
        "attributes.csv": [{"object_id": "pf_a", "attribute": name,
                            "status": state if name == "pnodfrom" else "PRESENT",
                            "value_json": '"x"' if state == "PRESENT" or name != "pnodfrom" else ""}
                           for name in ("pnodfrom", "pnodto", "inodfrom", "inodto",
                                        "iconfrom", "iconto", "GetFromSigName()",
                                        "GetToSigName()")],
        "references.csv": [{"source_id": "pf_a", "attribute": "pnodfrom", "position": "0",
                            "target_id": "pf_b"}],
    }
    for name, values in rows.items():
        with (tmp_path / name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=values[0])
            writer.writeheader()
            writer.writerows(values)
    (tmp_path / "manifest.json").write_text(json.dumps({"schema": "pf-dynamic-v1",
        "sha256": {name: sha256_file(tmp_path / name) for name in FILES}}), encoding="utf-8")
    return tmp_path


def test_complete_blksig(tmp_path):
    export = read_dynamic_export(fixture_export(tmp_path))
    assert export["blksig_complete"]
    assert len(export["attributes"]) == 8


def test_missing_blksig_is_reported(tmp_path):
    export = read_dynamic_export(fixture_export(tmp_path, state="UNAVAILABLE"))
    assert not export["blksig_complete"]
    assert export["blksig_missing"] == [("pf_a", "pnodfrom")]


def test_manifest_detects_change(tmp_path):
    fixture_export(tmp_path)
    with (tmp_path / "objects.csv").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="SHA-256"):
        read_dynamic_export(tmp_path)


def test_null_is_not_zero(tmp_path):
    fixture_export(tmp_path)
    path = tmp_path / "attributes.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    rows[0]["status"] = "NULL"
    rows[0]["value_json"] = ""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["sha256"]["attributes.csv"] = sha256_file(path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    export = read_dynamic_export(tmp_path)
    assert export["attributes"][0]["value_json"] == ""

