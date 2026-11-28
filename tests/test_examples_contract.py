"""Examples must remain executable from the project checkout."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys


ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"


def _run(name: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT), POWER_MACHINE_OUTPUT_DIR=str(tmp_path / "results"))
    return subprocess.run([sys.executable, str(EXAMPLES / name)], cwd=ROOT, env=env, text=True,
                          encoding="utf-8", capture_output=True, timeout=60, check=False)


def test_static_examples_run(tmp_path):
    for name in ("01_flujo_ieee39.py", "02_cortocircuito.py", "03_n1.py", "05_importar_csv.py", "06_batch.py", "07_cct.py"):
        result = _run(name, tmp_path)
        assert result.returncode == 0, result.stderr


def test_rms_example_runs(tmp_path):
    result = _run("04_rms_falla.py", tmp_path)
    assert result.returncode == 0, result.stderr
