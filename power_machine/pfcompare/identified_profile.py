"""Entrega identified profile offline con perfil explícito, sin parchear comparadores."""
from __future__ import annotations

import argparse
from pathlib import Path

from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR
from power_machine.pfcompare import rms_network as rms_replay
from power_machine.pfcompare import modal_network as mod
from power_machine.pfcompare.rms_replay import inside_workspace


def run(pf_dir=DEFAULT_PF_DIR, output_dir=rms_replay.OUTPUT_DIR, trials=None):
    """Ejecuta los cuatro ensayos completos y rechaza sobrescrituras."""
    selected = list(trials) if trials is not None else [*rms_replay.CASES, "E39-MOD-02"]
    if len(set(selected)) != len(selected) or any(t not in (*rms_replay.CASES, "E39-MOD-02") for t in selected):
        raise ValueError("Ensayos identified profile desconocidos o duplicados")
    for trial in selected:
        target = inside_workspace(Path(output_dir)/trial/"identified_profile")
        if target.exists():
            raise FileExistsError(target)
    results = {}
    for trial in selected:
        if trial == "E39-MOD-02":
            result = mod.run_comparison(pf_dir, output_dir, avr_profile="pf_identificado")
        else:
            result = rms_replay.run_trial(pf_dir, output_dir, trial, avr_profile="pf_identificado",
                                   progress=lambda s: print(s, flush=True))
        results[trial] = result
        print(trial, "pf_identificado", "accepted", result["accepted"], flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=rms_replay.OUTPUT_DIR)
    parser.add_argument("--trial", choices=(*rms_replay.CASES, "E39-MOD-02"), action="append")
    args = parser.parse_args()
    run(args.pf_dir, args.output_dir, args.trial)


if __name__ == "__main__":
    main()
