"""Comparación modal offline IEEE39, tolerancias previas y cobertura explícita."""
from __future__ import annotations
from power_machine.resources import required_readonly_path

import argparse
import csv
import hashlib
import json
from pathlib import Path
import numpy as np

from ..benchmarks.ieee39_pf import DEFAULT_PF_DIR, LDF_OPTIONS
from ..loadflow import run_loadflow
from ..modal.classical_pf import build_classical_model
from power_machine.pfcompare.lf import number, read_csv

WORKSPACE = Path(__file__).resolve().parents[2]
REAL_TOL_PER_S = 1e-3
IMAG_REL_TOL = 1e-3
ZERO_TOL_PER_S = 1e-8
TYPE_KEYS = set("h sgn ugn frequ tag cosn xd xq xds xqs xdss xqss tds0 tqs0 tdss0 tqss0 xl rstr model_inp iturbo i_speedVar isat isatSalient iusesat sg10 sg12 sg1q sg2q satse satseq satv satvq".split())


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def native_inputs(pf_dir):
    """Proyecta TypSym y ElmDsl. Los vectores DSL no se truncan ni reinterpretan."""
    root = Path(pf_dir)
    types = {r["path"]: {k: v for k, v in r["attributes"].items() if k in TYPE_KEYS}
             for r in read_json(root / "modelo/types.json") if r["cls"] == "TypSym"}
    config = {r["path"]: r["fields"] for r in read_json(root / "E39-MOD-01/configuration_final.json")["generators"]}
    controllers = read_json(root / "modelo/controllers.json")
    return types, config, controllers


def minimum_distance_pairs(reference, calculated):
    """Asignación biyectiva de distancia compleja total mínima, algoritmo húngaro.

    Índices base cero, empate determinista. No depende del orden de eig.
    Requiere igual número de modos, no completa subconjuntos con ceros.
    """
    ref, calc = np.asarray(reference, complex), np.asarray(calculated, complex)
    if ref.ndim != 1 or calc.shape != ref.shape or not np.isfinite(ref).all() or not np.isfinite(calc).all():
        raise ValueError("Espectros finitos de igual dimensión requeridos")
    cost = abs(ref[:, None] - calc[None, :])
    n = len(ref)
    u, v = np.zeros(n+1), np.zeros(n+1)
    p, way = np.zeros(n+1, int), np.zeros(n+1, int)
    for i in range(1, n+1):
        p[0] = i
        j0 = 0
        minimum = np.full(n+1, np.inf)
        used = np.zeros(n+1, bool)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], np.inf, 0
            for j in range(1, n+1):
                if not used[j]:
                    current = cost[i0-1, j-1] - u[i0] - v[j]
                    if current < minimum[j]:
                        minimum[j], way[j] = current, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(n+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return sorted((int(p[j]-1), j-1) for j in range(1, n+1))


def damping(value):
    return None if abs(value) <= ZERO_TOL_PER_S else float(-value.real / abs(value))


def compare_spectra(reference, calculated, *, exclude_structural_zero=False):
    """Deltas pm menos referencia, real [1/s], imag [1/s], f [Hz], zeta [1].

    Imag de referencia nula usa error absoluto <=1e-8 1/s como control
    numérico del carácter aperiódico. Zeta se informa sin umbral añadido.
    Solo el cero estructural explícitamente autorizado se excluye del criterio.
    """
    rows = []
    for i, j in minimum_distance_pairs(reference, calculated):
        pf, pm = complex(reference[i]), complex(calculated[j])
        real_error = abs(pm.real - pf.real)
        imag_error = abs(pm.imag - pf.imag)
        relative = imag_error / abs(pf.imag) if abs(pf.imag) > ZERO_TOL_PER_S else None
        zpf, zr = damping(pf), damping(pm)
        excluded = exclude_structural_zero and abs(pf) <= ZERO_TOL_PER_S and abs(pm) <= ZERO_TOL_PER_S
        rows.append({"reference_index": i+1, "pm_index": j+1,
                     "reference_real_per_s": pf.real, "reference_imag_per_s": pf.imag,
                     "pm_real_per_s": pm.real, "pm_imag_per_s": pm.imag,
                     "real_abs_error_per_s": real_error, "imag_abs_error_per_s": imag_error,
                     "imag_relative_error": relative, "distance_per_s": abs(pm-pf),
                     "reference_frequency_hz": abs(pf.imag)/(2*np.pi),
                     "pm_frequency_hz": abs(pm.imag)/(2*np.pi),
                     "frequency_delta_hz": (abs(pm.imag)-abs(pf.imag))/(2*np.pi),
                     "reference_damping_ratio": zpf, "pm_damping_ratio": zr,
                     "damping_abs_error": abs(zr-zpf) if zr is not None and zpf is not None else None,
                     "real_tolerance_per_s": REAL_TOL_PER_S, "imag_relative_tolerance": IMAG_REL_TOL,
                     "excluded": excluded,
                     "exclusion_reason": "Referencia angular congelada, simetría de rotación común" if excluded else "",
                     "pass": None if excluded else bool(real_error <= REAL_TOL_PER_S and
                         (relative <= IMAG_REL_TOL if relative is not None else imag_error <= ZERO_TOL_PER_S))})
    return rows


def read_eigenvalues(path):
    rows = read_csv(path)
    if [int(r["row"]) for r in rows] != list(range(1, len(rows)+1)):
        raise ValueError("Índices modales PF no consecutivos")
    values = []
    for r in rows:
        real, imag = number(r["real"]), number(r["imag"])
        if real is None or imag is None:
            raise ValueError("Autovalor ausente, no es cero")
        values.append(complex(real, imag))
    return np.array(values)


def frozen_rows(matrix_path, states, *, native_sparse_path=None):
    """Escanea Amat CSV y conserva solo máximos por fila, nunca la matriz entera."""
    maxima = np.zeros(len(states))
    entries = np.zeros(len(states), int)
    seen = {}
    with Path(matrix_path).open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            i, j = int(r["row"])-1, int(r["column"])-1
            if not (0 <= i < len(states) and 0 <= j < len(states)) or (i, j) in seen:
                raise ValueError("Índices Amat repetidos o fuera de rango")
            real, imag = number(r["real"]), number(r["imag"])
            if real is None or imag is None:
                raise ValueError("Entrada Amat ausente")
            seen[(i, j)] = complex(real, imag)
            maxima[i] = max(maxima[i], abs(complex(real, imag)))
            entries[i] += 1
    if native_sparse_path is not None:
        # MTL nativo es lista de coordenadas no nulas. Exige la misma lista
        # completa en CSV antes de admitir sus ceros implícitos.
        native_seen = set()
        with Path(native_sparse_path).open(encoding="utf-8-sig") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) != 3:
                    raise ValueError("Formato de coordenadas MTL no reconocido")
                key = (int(parts[0])-1, int(parts[1])-1)
                value = number(parts[2])
                if key in native_seen or key not in seen or value is None or value == 0:
                    raise ValueError("Coordenada MTL ausente, repetida o nula")
                if not np.isclose(seen[key], value, rtol=1e-14, atol=0):
                    raise ValueError("CSV distinto del MTL nativo")
                native_seen.add(key)
        if native_seen != set(seen) or not native_seen:
            raise ValueError("CSV incompleto respecto de coordenadas MTL")
    elif np.any(entries != len(states)):
        raise ValueError("Amat incompleta, no se presupone cero para celdas ausentes")
    return [dict(r, row_max_abs_per_s=float(maxima[i]), native_row_entries=int(entries[i]),
                 matrix_format="native_sparse_coordinate" if native_sparse_path is not None else "dense")
            for i, r in enumerate(states) if maxima[i] == 0]


def identity(path):
    relative = path.split(".IntPrj\\", 1)[1]
    return {"object_id": "pf_" + hashlib.sha256(relative.encode()).hexdigest()[:20],
            "relative_path": relative}


def regulated_coverage(root, model, controllers):
    """Report coverage for the models present in the supplied IEEE 39 case."""
    states = read_csv(root / "E39-MOD-02/states.csv")
    rows = []
    for r in states:
        cls = r["model"].rsplit(".", 1)[-1]
        if cls == "ElmSym":
            reason = "Máquina RMS 5/6 estados disponible, sin importador e inicialización del conjunto IEEE39 ni marco completo auditado"
        elif cls == "ElmLod":
            reason = "Estado xu de medición de tensión PF ausente en RMSLoad, que tiene cero estados"
        elif "AVR" in r["model"]:
            reason = "Sin acoplamiento IEEE39 avr_IEEET1, saturación de excitador y limitadores no reproducidos en este perfil"
        else:
            reason = "Sin acoplamiento IEEE39 gov_IEEEG1/gov_IEEEG3 al par mecánico"
        # Vincula nombre modal abreviado a la ruta real, no lo usa como ID.
        candidates = [g for g in model["gens"] if cls == "ElmSym" and g["path"].endswith("\\"+r["model"].split("\\")[-1])]
        candidates += [c for c in controllers if cls == "ElmDsl" and c["path"].endswith("\\"+r["model"].split("\\")[-1])]
        candidates += [l for l in model["loads"] if cls == "ElmLod" and l["path"].endswith("\\"+r["model"].split("\\")[-1])]
        ident = identity(candidates[0]["path"]) if len(candidates) == 1 else {"object_id": None, "relative_path": None}
        rows.append({**ident, "native_state_index": int(r["index"]), "native_model": r["model"],
                     "variable": r["state"], "status": "unsupported", "reason": reason})
    frozen = frozen_rows(root / "E39-MOD-02/Amat.csv", states,
                         native_sparse_path=root / "E39-MOD-02/Amat.mtl")
    expected = [r for r in states if (r["state"] == "xr" and "AVR" in r["model"] and "AVR 01" not in r["model"])
                or (r["state"] == "phi" and r["model"].endswith("G 02.ElmSym"))]
    if {r["index"] for r in frozen} != {r["index"] for r in expected} or len(frozen) != 10:
        raise ValueError("El subconjunto constante difiere de la evidencia entregada")
    for row in rows:
        if str(row["native_state_index"]) in {r["index"] for r in frozen}:
            row.update(status="constant_quotient_only", reason="Ecuación nativa congelada dx/dt=0, no reproduce su acoplamiento al resto")
    return rows, frozen


def write_csv(path, rows):
    from power_machine.pfcompare.rms_replay import inside_workspace
    target = inside_workspace(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("Tabla vacía sin contrato de columnas")
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run_comparisons(pf_dir=DEFAULT_PF_DIR, output_dir=None):
    """Construye MOD-01 y audita MOD-02, sin ejecutar ni importar PowerFactory."""
    root = Path(pf_dir)
    from power_machine.resources import output_root
    from power_machine.pfcompare.rms_replay import inside_workspace
    target = inside_workspace(output_dir if output_dir is not None else output_root() / "comparison_results/ieee39_pf")
    model_path = Path(pf_dir) / "model.json"
    model = read_json(model_path)
    types, config, controllers = native_inputs(root)
    lf = run_loadflow(model, options=LDF_OPTIONS)
    classical = build_classical_model(model, lf, types, config)
    eigenvalues, vectors = np.linalg.eig(classical.a)
    pf = read_eigenvalues(root / "E39-MOD-01/EVals.csv")
    if len(pf) != 20 or len(eigenvalues) != 20:
        raise ValueError("MOD-01 exige exactamente 20 estados")
    # Prueba explícita de simetría angular antes de excluir un solo modo.
    common = np.tile([0.0, 1.0], 10)
    if np.linalg.norm(classical.a @ common) > 1e-10 or sum(abs(eigenvalues) <= ZERO_TOL_PER_S) != 1:
        raise ValueError("Cero estructural clásico no identificado")
    pairs = compare_spectra(pf, eigenvalues, exclude_structural_zero=True)
    independent = read_csv(root / "E39-MOD-01/independent_classical/eigenvalue_comparison.csv")
    independent_values = [complex(number(r["numpy_real_1_s"]), number(r["numpy_imag_1_s"])) for r in independent]
    independent_pairs = compare_spectra(independent_values, eigenvalues, exclude_structural_zero=True)
    for row in independent_pairs:
        row["independent_index"] = int(independent[row["reference_index"]-1]["index"])
    a_independent = np.loadtxt(root / "E39-MOD-01/independent_classical/A_independent_PF_state_order.csv", delimiter=",")
    ideal = build_classical_model(model, lf, types, config, mechanical_convention="ideal_power")
    ideal_pairs = compare_spectra(pf, np.linalg.eigvals(ideal.a))
    residuals = [float(np.linalg.norm(classical.a @ vectors[:, i] - eigenvalues[i]*vectors[:, i]) /
                       (np.linalg.norm(classical.a)*np.linalg.norm(vectors[:, i]))) for i in range(20)]
    summary1 = {"status": "comparado contra PF", "pass": all(r["pass"] for r in pairs if not r["excluded"]),
                "states": 20, "compared_modes": 19, "excluded_structural_modes": 1,
                "real_tolerance_per_s": REAL_TOL_PER_S, "imag_relative_tolerance": IMAG_REL_TOL,
                "max_real_abs_error_per_s": max(r["real_abs_error_per_s"] for r in pairs),
                "max_imag_relative_error": max(r["imag_relative_error"] for r in pairs if r["imag_relative_error"] is not None),
                "max_distance_per_s": max(r["distance_per_s"] for r in pairs),
                "max_damping_abs_error": max(r["damping_abs_error"] for r in pairs if r["damping_abs_error"] is not None),
                "max_independent_distance_per_s": max(r["distance_per_s"] for r in independent_pairs),
                "independent_pass": all(r["pass"] for r in independent_pairs if not r["excluded"]),
                "max_independent_matrix_abs_error": float(np.max(abs(classical.a-a_independent))),
                "max_eigenvector_residual": max(residuals),
                "max_equilibrium_residual": float(np.max(abs(classical.derivative(classical.x0)))),
                "ideal_power_max_distance_per_s": max(r["distance_per_s"] for r in ideal_pairs),
                "ideal_power_pass": all(r["pass"] for r in ideal_pairs),
                "frequency_hz": model["meta"]["f_nom_hz"], "s_base_mva": model["meta"]["s_base_mva"],
                "matching": "Asignación biyectiva de distancia compleja total mínima, sin usar orden de eig",
                "mechanical_convention": "Tm=Pm/(1+nu), Te=Pe nominal, D explícito=0",
                "reference": "G02, delta_dot=omega_nom*(nu_i-nu_G02), delta_G02 constante"}
    mod1 = target / "E39-MOD-01"
    for name, rows in (("eigenvalue_pairs", pairs), ("independent_pairs", independent_pairs),
                       ("ideal_power_diagnostic", ideal_pairs), ("machines", classical.machines), ("states", classical.states)):
        write_csv(mod1 / (name+".csv"), rows)
    write_csv(mod1 / "A_pm.csv", [{"row": i+1, "column": j+1, "value": float(classical.a[i,j])}
                                      for i in range(20) for j in range(20)])
    write_csv(mod1 / "summary.csv", [{"metric": k, "value": json.dumps(v, ensure_ascii=False)} for k,v in summary1.items()])
    pf2 = read_eigenvalues(root / "E39-MOD-02/EVals.csv")
    coverage, frozen = regulated_coverage(root, model, controllers)
    if len(pf2) != 166 or len(coverage) != 166:
        raise ValueError("MOD-02 exige 166 estados nativos")
    zero_indices = np.flatnonzero(abs(pf2) <= ZERO_TOL_PER_S)
    if len(zero_indices) != len(frozen):
        raise ValueError("Multiplicidad de ceros distinta del cociente constante")
    # Único cociente autónomo exactamente representable, q_dot=0.
    # No se reconstruye ni se diagonaliza Amat como si fuese un modelo pm.
    zero_pairs = compare_spectra(pf2[zero_indices], np.linalg.eigvals(np.zeros((len(frozen), len(frozen)))))
    for row in zero_pairs:
        row["native_eigenvalue_index"] = int(zero_indices[row["reference_index"]-1]+1)
        row["scope"] = "comparación parcial, cociente de coordenadas constantes"
        row["individual_identity"] = "No identificable, autovalor cero repetido"
    band = read_csv(root / "E39-MOD-02/modes_0_2_to_2_5_Hz.csv")
    for row in band:
        index = int(row["native_mode_index"])-1
        if abs(pf2[index]-complex(float(row["real_1_s"]),float(row["imag_1_s"]))) > ZERO_TOL_PER_S:
            raise ValueError("Tabla de banda inconsistente con EVals")
        row.update(scope="comparación parcial", status="no medido", pm_real_per_s=None,
                   pm_imag_per_s=None, pass_comparison=None,
                   reason="Modo acoplado con máquina, reguladores o carga, sin subsistema modal pm equivalente")
    summary2 = {"status": "comparación parcial", "full_model_status": "unsupported", "native_states": 166,
                "full_eigenvalues_compared": 0, "constant_quotient_modes_compared": len(zero_pairs),
                "constant_quotient_pass": all(r["pass"] for r in zero_pairs),
                "oscillatory_modes_compared": 0, "band_positive_imag_modes_unmeasured": len(band),
                "caveat": "Diez ceros del cociente q_dot=0, no prueban AVR, máquinas, cargas ni red regulada",
                "pss_active_exported": sum(c["name"].startswith("PSS") and c["fields"]["outserv"]["value"] == 0 for c in controllers),
                "local_machine_partial_meaning": "Máquina-admitancia local, 6 estados y 2 algebraicas, no red IEEE39",
                "full_dae_unsupported_meaning": "DAE completo sin autovalores por híbridos, unidades y procedencia, no se hereda al IEEE39"}
    mod2 = target / "E39-MOD-02"
    for name, rows in (("coverage", coverage), ("frozen_states", frozen), ("constant_quotient_pairs", zero_pairs),
                       ("modes_0_2_to_2_5_Hz_unmeasured", band)):
        write_csv(mod2 / (name+".csv"), rows)
    write_csv(mod2 / "summary.csv", [{"metric": k, "value": json.dumps(v, ensure_ascii=False)} for k,v in summary2.items()])
    audit = [{**identity(p), "type_path": p, "parameter": k, "value_json": json.dumps(v["value"]), "unit": v["unit"]}
             for p, data in types.items() for k,v in data.items()]
    write_csv(mod2 / "native_machine_parameters.csv", audit)
    control_audit = [{**identity(c["path"]), "name": c["name"], "type_path": c["fields"]["typ_id"]["value"],
                      "outserv": c["fields"]["outserv"]["value"],
                      "params_json": json.dumps(c["fields"]["params"]["value"]),
                      "sParams_json": json.dumps(c["type_fields"]["sParams"]["value"]),
                      "status": "fuera de servicio" if c["fields"]["outserv"]["value"] else "acoplamiento no soportado"}
                     for c in controllers]
    write_csv(mod2 / "native_controller_parameters.csv", control_audit)
    inputs = [model_path] + [root / f for f in ("modelo/types.json", "modelo/generators.json", "modelo/controllers.json",
        "E39-MOD-01/configuration_final.json", "E39-MOD-01/EVals.csv", "E39-MOD-01/independent_classical/eigenvalue_comparison.csv",
        "E39-MOD-01/independent_classical/A_independent_PF_state_order.csv", "E39-MOD-02/EVals.csv",
        "E39-MOD-02/states.csv", "E39-MOD-02/Amat.csv", "E39-MOD-02/Amat.mtl", "E39-MOD-02/modes_0_2_to_2_5_Hz.csv")]
    write_csv(mod2 / "input_provenance.csv", [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in inputs])
    return {"E39-MOD-01": summary1, "E39-MOD-02": summary2}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=output_root() / "comparison_results/ieee39_pf")
    args = parser.parse_args()
    print(json.dumps(run_comparisons(args.pf_dir, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
