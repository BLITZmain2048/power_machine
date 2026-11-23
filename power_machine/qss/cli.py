"""CLI: python -m power_machine.qss.cli, resultados sin validar contra PF."""
from __future__ import annotations

import argparse
import json

from .runner import run_series
from .series import read_series


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--series", required=True)
    parser.add_argument("--output", required=True, help="Directorio nuevo obligatorio")
    parser.add_argument("--dispatch", choices=("reference", "proportional"), default="reference")
    parser.add_argument("--policy", choices=("continue", "stop"), default="continue")
    parser.add_argument("--default-factor", type=float)
    parser.add_argument("--flat-start", action="store_true")
    parser.add_argument("--resume-from", help="Corrida fuente con checkpoints previos")
    start = parser.add_mutually_exclusive_group()
    start.add_argument("--resume-index", type=int, default=0, help="Índice cero basado, se recalcula")
    start.add_argument("--resume-t-s", type=float, help="Instante exacto [s], se recalcula")
    parser.add_argument("--end-index", type=int, help="Fin exclusivo para una corrida parcial")
    args = parser.parse_args(argv)
    try:
        model, series = read_series(args.model, args.manifest, args.series, default_factor=args.default_factor)
        start_index = args.resume_index
        if args.resume_t_s is not None:
            matches = [i for i, step in enumerate(series.steps) if step.t_s == args.resume_t_s]
            if not matches:
                raise ValueError("resume-t-s no coincide con un instante de la serie")
            start_index = matches[0]
        result = run_series(model, series, args.output, rule=args.dispatch,
                            policy=args.policy, warm_start=not args.flat_start,
                            resume_from=args.resume_from, start_index=start_index, end_index=args.end_index)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error QSS: {exc}\n")
    summary = result["summary"]
    print(json.dumps({k: summary[k] for k in ("series_name", "rule", "steps_executed", "nonconverged_steps",
                                             "voltage_violation_count", "elapsed_s", "validation_status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
