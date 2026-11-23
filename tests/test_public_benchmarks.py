"""The public benchmark is self-contained and records executable samples."""
from pathlib import Path
import json
import os
import subprocess
import sys


def test_public_static_benchmark_smoke(tmp_path):
    script = Path(__file__).resolve().parents[1] / "benchmarks" / "run_benchmarks.py"
    output = tmp_path / "measurements.json"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", POWER_MACHINE_OUTPUT_DIR=str(tmp_path))
    completed = subprocess.run([sys.executable, str(script), "--sizes", "5", "--repeats", "1",
                                "--backend", "both", "--output", str(output)],
                               cwd=tmp_path, env=env, text=True, capture_output=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    dense = [row for row in data["records"] if row["backend"] == "dense"]
    assert {row["study"] for row in dense} == {"lf", "n1", "sc", "synthetic"}
    assert all(row["status"] == "completed" and len(row["samples"]) == 1 for row in dense)
    assert all(row["samples"][0]["wall_s"] > 0 and row["samples"][0]["cpu_s"] >= 0 for row in dense)
    assert data["environment"]["python"] and data["environment"]["numpy"]
    sparse = [row for row in data["records"] if row["backend"] == "sparse"]
    expected = "completed" if data["environment"]["scipy"] is not None else "unavailable"
    assert all(row["status"] == expected for row in sparse)
    assert data["environment"]["threads"]["OPENBLAS_NUM_THREADS"] == "1"
