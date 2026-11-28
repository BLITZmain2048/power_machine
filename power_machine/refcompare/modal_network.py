"""MOD-02 de 166 estados, reconstrucción pm independiente del objetivo REF."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import numpy as np

from power_machine.benchmarks.ieee39_ref import DEFAULT_REF_DIR, build_rms_case
from power_machine.refcompare.rms_replay import inside_workspace, read_json, write_json, write_csv
from power_machine.refcompare.rms_network import OUTPUT_DIR, STATUS, fresh_output, provenance, profile_output
from power_machine.dynamics.avr_ieeet1 import AVR_PROFILES, REF_IDENTIFIED_STATUS
from power_machine.refcompare.modal import compare_spectra, read_eigenvalues, frozen_rows, REAL_TOL_PER_S, IMAG_REL_TOL


def native_matrix(ref_dir):
    """Lee coordenadas Amat cotejadas con MTL, vacíos nunca equivalen a cero."""
    root = Path(ref_dir)/"E39-MOD-02"
    with (root/"states.csv").open(encoding="utf-8-sig", newline="") as f:
        states = list(csv.DictReader(f))
    frozen = frozen_rows(root/"Amat.csv", states, native_sparse_path=root/"Amat.mtl")
    a = np.zeros((len(states), len(states)), complex)
    with (root/"Amat.csv").open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            a[int(r["row"])-1, int(r["column"])-1] = complex(float(r["real"]), float(r["imag"]))
    if np.max(abs(a.imag)) != 0:
        raise ValueError("Amat REF no es real")
    return a.real, states, frozen


from power_machine.dynamics.state_coordinates import state_transform


def run_comparison(ref_dir=DEFAULT_REF_DIR, output_dir=OUTPUT_DIR, *, avr_profile="dyn_a"):
    target = (fresh_output(output_dir, "E39-MOD-02") if avr_profile == "dyn_a" else
              profile_output(output_dir, "E39-MOD-02", avr_profile))
    write_json(target/"tolerances_before_measurement.json", {
        "real_absolute_per_s": REAL_TOL_PER_S, "imag_relative": IMAG_REL_TOL,
        "zero_imag_absolute_per_s": 1e-8, "no_mode_excluded": True})
    rms = (build_rms_case(ref_dir) if avr_profile == "dyn_a" else
           build_rms_case(ref_dir, avr_profile=avr_profile))
    a, diagnostics = rms.linearize()
    half, _ = rms.linearize(rel_step=.5e-6)
    reference = read_eigenvalues(Path(ref_dir)/"E39-MOD-02/EVals.csv")
    ref_a, ref_states, frozen = native_matrix(ref_dir)
    t, mapping = state_transform(rms, ref_states)
    # T es permutación diagonal escalada, resolver evita construir inversa.
    mapped = np.linalg.solve(t.T, (t@a).T).T
    calculated = np.linalg.eigvals(a)
    pairs = compare_spectra(reference, calculated)
    audit_pairs = compare_spectra(reference, np.linalg.eigvals(ref_a))
    rows = [{"ref_index": i+1, "model": r["model"], "state": r["state"],
             "max_row_abs_delta": float(max(abs(mapped[i]-ref_a[i]))),
             "ref_row_max_abs": float(max(abs(ref_a[i]))),
             "pm_row_max_abs": float(max(abs(mapped[i])))} for i, r in enumerate(ref_states)]
    write_csv(target/"matrix_row_errors.csv", rows)
    write_csv(target/"state_mapping.csv", mapping)
    write_csv(target/"state_manifest.csv", rms.manifest)
    write_csv(target/"eigenvalue_pairs.csv", pairs)
    write_csv(target/"native_matrix_eigenvalue_audit.csv", audit_pairs)
    write_csv(target/"frozen_ref_rows.csv", frozen)
    write_json(target/"equilibrium.json", rms.equilibrium)
    write_csv(target/"Amat_pm.csv", [{"row": i+1, "column": j+1, "real": float(a[i, j]), "imag": 0.}
                                      for i in range(len(a)) for j in range(len(a))])
    write_csv(target/"Amat_pm_ref_coordinates.csv", [{"row": i+1, "column": j+1,
                                                       "real": float(mapped[i, j]), "imag": 0.}
                                                      for i in range(len(a)) for j in range(len(a))])
    summary = {"status": STATUS, "states": len(a), "compared_modes": len(pairs),
               "accepted_modes": sum(r["pass"] for r in pairs), "accepted": all(r["pass"] for r in pairs),
               "validated_against_ref": False, "parameters_fitted": False,
               "state_counts": {k: sum(r["kind"] == k for r in rms.manifest)
                                for k in ("machine", "avr", "governor", "load")},
               "diagnostics": diagnostics, "jacobian_h_half_max_difference": float(np.max(abs(a-half))),
               "matrix_max_difference_in_assumed_ref_coordinates": float(np.max(abs(mapped-ref_a))),
               "native_matrix_export_max_eigenvalue_distance_per_s": max(r["distance_per_s"] for r in audit_pairs),
               "auxiliary_avr_poles_per_s": [-10000.]*9,
               "hypotheses": ["Tr=0 conserva polos auxiliares machine model frente a nueve xr REF congelados",
                               "Coordenadas y saturación excitador son supuestos IEEE sin BlkDef",
                               "Bases PN=0, cableado y limitadores governor son supuestos controller model"],
               "input_sha256": provenance(rms, "E39-MOD-02")}
    if avr_profile == "ref_identificado":
        summary.update(status=REF_IDENTIFIED_STATUS, avr_profile=avr_profile,
                       evidence_scope="EN-MUESTRA", validated_against_blkdef=False,
                       auxiliary_avr_poles_per_s=[], frozen_avr_rows=9,
                       hypotheses=["Curva exponencial seleccionada con pendientes de Amat",
                                   "Tr=0 conserva cuatro estados con xr congelado",
                                   "Designación E1 psi1q/psi2q, E2 xf=Xf, sin cambio físico de máquina",
                                   "BlkDef, límites y ramas no activas permanecen sin confirmar"])
    write_json(target/"summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref-dir", type=Path, default=DEFAULT_REF_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--avr-profile", choices=AVR_PROFILES, default="dyn_a")
    args = parser.parse_args()
    inside_workspace(args.output_dir)
    summary = run_comparison(args.ref_dir, args.output_dir, avr_profile=args.avr_profile)
    print("MOD-02 modos conformes", summary["accepted_modes"], "/", summary["states"], flush=True)


if __name__ == "__main__":
    main()
