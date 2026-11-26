"""Inspect built distributions rather than trusting package-data declarations."""
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile
import pytest

@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_distribution_excludes_external_data(tmp_path, kind):
    if kind == "wheel":
        pytest.importorskip("wheel", reason="wheel build dependency is unavailable")
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "MANIFEST.in", "README.md", "NOTICE", "LICENSE"):
        shutil.copy2(root / name, source / name)
    shutil.copytree(root / "power_machine", source / "power_machine",
                    ignore=shutil.ignore_patterns("__pycache__", "results"))
    subprocess.run([sys.executable, "-c", "import setuptools.build_meta as b; b.build_" + kind + "('dist')"],
                   cwd=source, check=True, capture_output=True)
    artifact = next((source / "dist").iterdir())
    if kind == "wheel":
        with zipfile.ZipFile(artifact) as archive: names = archive.namelist()
    else:
        with tarfile.open(artifact) as archive: names = archive.getnames()
    for name in names:
        parts = Path(name).parts
        assert "results" not in parts
        assert not any(part.startswith("E39-") for part in parts)
        assert not name.endswith(("signals_native.csv", "fault_results.json.gz", "contributions.csv.gz"))
    assert any(name.endswith("cases/ieee39/model.json") for name in names)
