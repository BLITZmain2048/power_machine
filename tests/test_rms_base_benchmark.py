"""Medición reproducible de BASE hasta 2 s, ejecutable manual de desktop benchmark."""
from pathlib import Path
import argparse
import ctypes
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_benchmark_is_a_manual_entrypoint_without_import_side_effects():
    assert callable(main)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT not in output.parents:
        raise ValueError('La salida debe permanecer dentro del proyecto')
    import numpy as np
    from power_machine.dynamics.runner import RMSRunner
    library = next((Path(np.__file__).parent.parent/'numpy.libs').glob('*openblas*.dll'))
    dll = ctypes.CDLL(str(library))
    threads = dll.scipy_openblas_get_num_threads64_()
    runner = RMSRunner(ROOT, 'BASE_15s')
    last = [-1e6]
    def progress(t, end, ms):
        if t-last[0] >= .25 or t == end:
            print(f't={t:.6f} s, {ms:.3f} ms/paso', flush=True)
            last[0] = t
    report = runner.run(end_s=2., output=output, on_progress=progress)
    metadata = {'python': sys.executable, 'numpy': np.__version__,
                'openblas_env': os.environ.get('OPENBLAS_NUM_THREADS'),
                'openblas_threads': threads, 'steps': report['steps'],
                'wall_s': report['wall_s'], 'ms_per_step': report['ms_per_step'],
                'completed': report['completed'], 'failure': report['failure']}
    (output/'benchmark.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(json.dumps(metadata), flush=True)
    return 0 if report['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

