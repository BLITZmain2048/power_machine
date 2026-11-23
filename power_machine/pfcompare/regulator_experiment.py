"""Ejecutor regulador experimental optativo para los comparadores native RMS, sin registro permanente.

La opción exponencial es un HALLAZGO experimental, sin validar contra PF.
Las referencias se inicializan desde LF, nunca desde señales DIN o Amat.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from pathlib import Path

import numpy as np

from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, WORKSPACE, build_rms_case
from power_machine.dynamics.avr_exponential import ExponentialIEEET1AVR
from power_machine.dynamics.regulator_diagnostics import coordinate_transform
from power_machine.pfcompare import rms_network as rms_replay
from power_machine.pfcompare import modal_network as mod
from power_machine.pfcompare.rms_replay import inside_workspace, write_json


def build_variant(pf_dir, variant):
    """Construye base independiente del objetivo y sustituye solo AVR."""
    rms = build_rms_case(pf_dir)
    if variant == "baseline":
        return rms
    if variant not in ("exponential", "exponential_frozen"):
        raise ValueError("Variante E2 desconocida")
    for d in rms.devices:
        if getattr(d, "avr", None):
            c = d.avr
            d.avr = ExponentialIEEET1AVR(c.object_id, c.relative_path, c.parameters,
                               frozen_measurement=variant == "exponential_frozen")
    rms.initial, _ = rms.solver.initialize(rms.case.voltages, rms.initial_values)
    rms.manifest = rms.state_manifest()
    f, _, net = rms.solver._evaluate(0., rms.initial.x, rms.initial.z, rms.initial.v)
    rms.equilibrium = {**rms.equilibrium, "derivatives": f.tolist(),
                       "max_derivative_per_s": float(max(abs(f))),
                       "max_kcl_pu": float(max(abs(net)))}
    if max(max(abs(f)), max(abs(net))) >= 1e-8:
        raise ValueError("Variante E2 sin equilibrio inicial")
    return rms


def fresh_output(output_dir, trial, label):
    """Crea subcarpeta nueva y rechaza sobrescritura de cualquier evidencia."""
    if Path(label).name != label or label in (".", "..") or not label:
        raise ValueError("Etiqueta E2 debe ser un nombre de carpeta")
    target = inside_workspace(Path(output_dir)/trial/"regulator_experiment"/label)
    target.mkdir(parents=True, exist_ok=False)
    write_json(target/"tolerances_before_measurement.json", rms_replay.TOLERANCES)
    return target


@contextmanager
def comparator_options(variant, label):
    """Opciones limitadas a esta llamada, restaura funciones aun con fallo."""
    old = rms_replay.build_rms_case, mod.build_rms_case, rms_replay.fresh_output, mod.fresh_output, mod.state_transform
    try:
        rms_replay.build_rms_case = mod.build_rms_case = lambda p: build_variant(p, variant)
        rms_replay.fresh_output = mod.fresh_output = lambda p, trial: fresh_output(p, trial, label)
        mod.state_transform = coordinate_transform
        yield
    finally:
        rms_replay.build_rms_case, mod.build_rms_case, rms_replay.fresh_output, mod.fresh_output, mod.state_transform = old


def run(pf_dir, output_dir, variant, label, trials):
    """Usa rms_network y modal_network con tolerancias intactas."""
    inside_workspace(output_dir)
    # El manifiesto y las tolerancias se publican antes de medir.
    plan = inside_workspace(Path(output_dir)/"E39-MOD-02"/"regulator_experiment"/(label+"_plan.json"))
    if plan.exists():
        raise FileExistsError(plan)
    plan.parent.mkdir(parents=True, exist_ok=True)
    code = [Path(__file__), WORKSPACE/"dynamics/avr_exponential.py",
            WORKSPACE/"dynamics/regulator_diagnostics.py"]
    write_json(plan, {"variant": variant, "trials": trials, "pf_dir": str(pf_dir),
                      "parameters_fitted": False, "status": "hallazgo, sin validar contra PF",
                      "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in code},
                      "din_tolerances": rms_replay.TOLERANCES,
                      "mod_tolerances": {"real_absolute_per_s": mod.REAL_TOL_PER_S,
                                         "imag_relative": mod.IMAG_REL_TOL}})
    with comparator_options(variant, label):
        for trial in trials:
            if trial == "E39-MOD-02":
                summary = mod.run_comparison(pf_dir, output_dir)
                summary["auxiliary_avr_poles_per_s"] = ([] if variant.endswith("_frozen") else [-10000.]*9)
                summary["frozen_avr_rows"] = 9 if variant.endswith("_frozen") else 0
                summary["hypotheses"] = ["xf=Xf según proyección local E2",
                                          "Saturación exponencial solo si variante E2 explícita",
                                          "Bloques y límites sin confirmar por BlkDef"]
            else:
                summary = rms_replay.run_trial(pf_dir, output_dir, trial,
                                        progress=lambda s: print(s, flush=True))
            summary.update(variant_e2=variant, parameters_fitted=False,
                           normative_formula_verified=False,
                           e2_code_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in code})
            write_json(Path(output_dir)/trial/"regulator_experiment"/label/"summary.json", summary)
            print(trial, variant, "accepted", summary["accepted"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=rms_replay.OUTPUT_DIR)
    parser.add_argument("--variant", choices=("baseline", "exponential", "exponential_frozen"), required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--trial", choices=(*rms_replay.CASES, "E39-MOD-02"), action="append")
    args = parser.parse_args()
    run(args.pf_dir, args.output_dir, args.variant, args.label, args.trial or [*rms_replay.CASES, "E39-MOD-02"])


if __name__ == "__main__":
    main()
