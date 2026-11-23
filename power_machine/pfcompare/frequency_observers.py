"""frequency observers offline, perfil oficial G1 y estimador nuevo desactivado por defecto.

El calendario físico es copia literal del simulador native RMS, con observadores
de salida en paralelo. No modifica el solver, controles ni condiciones físicas.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np

from power_machine.pfcompare import rms_network as rms_replay
from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, WORKSPACE, build_rms_case
from power_machine.dynamics.published_frequency_grid import FrequencyObservers
from power_machine.dynamics.avr_ieeet1 import PF_IDENTIFIED_STATUS
from power_machine.pfcompare.rms_replay import inside_workspace, load_reference, signal_metrics, values, write_csv, write_json

CASES, OUTPUT_DIR, TOLERANCES, STATUS = rms_replay.CASES, rms_replay.OUTPUT_DIR, rms_replay.TOLERANCES, rms_replay.STATUS
compare_channel, provenance = rms_replay.compare_channel, rms_replay.provenance
frequency_voltages = rms_replay.frequency_voltages


def simulate(rms, trial, channels, *, stop_s=10., progress=None, freq_estimator='accepted_step', partial=None):
    """Paso nominal 1 ms, muestra 10 ms, límite izquierdo en eventos exactos.

    No consume trayectorias PF. El calendario de salida se genera aquí.
    Euler implícito en el primer paso después de salto, luego trapezoidal.
    Las proyecciones de límites solo se aplican a pasos aceptados.
    """
    h = rms.solver.step_s
    output_s = .01
    n = round(stop_s/output_s)
    if abs(n*output_s-stop_s) > 1e-12 or abs(round(output_s/h)*h-output_s) > 1e-12:
        raise ValueError("Calendario requiere stop múltiplo de 10 ms y paso divisor de salida")
    times = np.arange(n+1)*output_s
    events_at = sorted({values(e["fields"])["time"] for e in
                       rms.case.inventory["trials"][trial]["events"]
                       if not values(e["fields"])["outserv"]})
    state = rms.initial.copy()
    history = FrequencyObservers(freq_estimator)
    freq = history.accept(0., frequency_voltages(rms, state))
    variant_rows = {name: [] for name in history.histories}

    def sample(state, frequency):
        row = rms_replay.sample_channels(rms, state, channels, frequency)
        for name, output in history.outputs.items():
            other = dict(row)
            for ch in channels:
                if ch["variable"] == "m:fehz":
                    bid = ch["object_id"]
                    other[ch["signal"]] = (np.nan if bid in rms.solver.bolted_bus_ids
                                           else output[bid])
            variant_rows[name].append(other)
        return row

    rows = [sample(state, freq)]
    events, metrics = [], []
    max_kcl, max_step_residual = 0., 0.
    next_event = 0
    started = time.monotonic()
    for t_out in times[1:]:
        while state.t < t_out-1e-12:
            event_t = events_at[next_event] if next_event < len(events_at) else np.inf
            target = min(t_out, event_t, state.t+h)
            if target-state.t > 1e-12:
                state, metric = rms.solver.step(state, target-state.t)
                if abs(state.t-target) < 1e-12:
                    state.t = float(target)
                voltage = frequency_voltages(rms, state)
                freq = history.accept(state.t, voltage,
                                      f_ref_pu=float(state.x[rms.solver.reference_speed_index]))
                _, _, net = rms.solver._evaluate(state.t, state.x, state.z, state.v)
                max_kcl = max(max_kcl, float(max(abs(net))))
                max_step_residual = max(max_step_residual, metric.scaled_residual)
                metrics.append({"time_s": state.t, "iterations": metric.iterations,
                                "scaled_residual": metric.scaled_residual, "method": metric.method})
            if abs(state.t-event_t) <= 1e-12:
                # Publicar t- antes de resolver t+, también en el ensayo con trip.
                if abs(state.t-t_out) <= 1e-12:
                    rows.append(sample(state, freq))
                state, applied = rms.apply_events(trial, event_t, state)
                events.extend(applied)
                freq = history.accept(state.t, frequency_voltages(rms, state), restart=True,
                                      f_ref_pu=float(state.x[rms.solver.reference_speed_index]))
                next_event += 1
        if len(rows) < round(t_out/output_s)+1:
            rows.append(sample(state, freq))
        if partial and abs(t_out-round(t_out)) < 1e-12:
            partial(times[:len(rows)], rows, metrics, state)
        if progress and abs(t_out-round(t_out)) < 1e-12:
            progress(f"{trial} t={t_out:.0f} s, {time.monotonic()-started:.1f} s de cálculo")
    predicted = {ch["signal"]: np.array([row[ch["signal"]] for row in rows]) for ch in channels}
    variants = {name: {ch["signal"]: np.array([row[ch["signal"]] for row in samples])
                       for ch in channels} for name, samples in variant_rows.items()}
    return times, predicted, {"variants": variants, "events": events, "steps": metrics,
                              "max_kcl_pu": max_kcl, "max_scaled_step_residual": max_step_residual,
                              "projections": rms.solver.projections,
                              "limit_events": rms.solver.limit_events,
                              "elapsed_s": time.monotonic()-started}


def run_trial(pf_dir, output_dir, trial, *, stop_s=10., progress=print, avr_profile="pf_identificado", freq_estimator="accepted_step", decision_path=None):
    if avr_profile != "pf_identificado":
        raise ValueError("frequency observers requiere el perfil oficial pf_identificado")
    if freq_estimator not in ("accepted_step", "nominal_grid"):
        raise ValueError("Estimador desconocido")
    if trial not in CASES:
        raise ValueError("Ensayo DIN desconocido")
    if Path(output_dir).resolve() in (WORKSPACE.resolve(), WORKSPACE.parent.resolve()):
        raise ValueError("La salida debe permanecer en la carpeta de trabajo de resultados")
    inside_workspace(Path(output_dir))
    target = inside_workspace(Path(output_dir)/trial/"frequency_observers")
    target.mkdir(parents=True, exist_ok=False)
    write_json(target/"tolerances_before_measurement.json", TOLERANCES)
    write_json(target/"manifest_before_measurement.json", campaign_manifest(trial, freq_estimator, decision_path=decision_path))
    rms = (build_rms_case(pf_dir) if avr_profile == "dyn_a" else
           build_rms_case(pf_dir, avr_profile=avr_profile))
    write_json(target/"equilibrium.json", rms.equilibrium)
    write_csv(target/"state_manifest.csv", rms.manifest)
    write_csv(target/"controller_coverage.csv", rms.catalog.coverage)
    refs = [{"object_id": d.object_id, "relative_path": d.relative_path,
             "Vref_pu": d.avr_inputs.get("Vref"), "Pref_pu": d.gov_inputs.get("Pref"),
             "ve_initial_pu": d.machine.ve, "pt_initial_pu": d.machine.pt}
            for d in rms.devices if hasattr(d, "machine")]
    write_csv(target/"references_from_lf.csv", refs)
    time_pf, reference, channels = load_reference(Path(pf_dir)/trial)
    time_s, predicted, diagnostics = simulate(rms, trial, channels, stop_s=stop_s, progress=progress,
        freq_estimator=freq_estimator, partial=partial_writer(target, channels))
    variants = diagnostics.pop("variants")
    np.savez(target/"report_10ms.npz", time_s=time_s,
             **{name: np.column_stack([p[ch["signal"]] for ch in channels])
                for name, p in variants.items()})
    if not np.allclose(time_s, time_pf[:len(time_s)], rtol=0., atol=1e-12):
        raise ValueError("Fechas pm/PF diferentes")
    metrics = [compare_channel(time_s, reference[ch["signal"]][:len(time_s)],
                               predicted[ch["signal"]], ch) for ch in channels]
    if avr_profile == "pf_identificado":
        for row in metrics:
            row.update(status=PF_IDENTIFIED_STATUS, avr_profile=avr_profile)
    write_csv(target/"metrics.csv", metrics)
    windows = []
    for label, lower, upper in (("pre", 0., 1.), ("transient", 1., 3.), ("post", 3., 10.00001)):
        mask = (time_s >= lower)&(time_s < upper)
        for ch in channels:
            key = ch["signal"]
            windows.append({"window": label, "signal": key,
                            **signal_metrics(time_s[mask], reference[key][:len(time_s)][mask], predicted[key][mask])})
    write_csv(target/"window_metrics.csv", windows)
    write_csv(target/"integration.csv", diagnostics.pop("steps"))
    write_csv(target/"events.csv", diagnostics["events"])
    with (target/"signals_native.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["time"]+[ch["object"]["name"] for ch in channels])
        writer.writerow(["b:tnow in s"]+[ch["variable"]+" in "+ch["unit"] for ch in channels])
        for i, t in enumerate(time_s):
            writer.writerow([format(t, ".12g")]+[
                format(predicted[ch["signal"]][i], ".15g") if np.isfinite(predicted[ch["signal"]][i]) else ""
                for ch in channels])
    write_csv(target/"channel_units.csv", [{k: ch[k] for k in
                                           ("signal", "column", "unit", "variable", "object_id", "relative_path")}
                                          for ch in channels])
    summary = {"trial": trial, "status": STATUS, "samples": len(time_s), "channels": len(channels),
               "state_count": rms.solver.nx, "tolerances": TOLERANCES, "diagnostics": diagnostics,
               "max_pass_count": sum(r["max_pass"] is True for r in metrics),
               "peak_pass_count": sum(r["peak_pass"] is True for r in metrics),
               "accepted": bool(stop_s == 10. and all(r["complete_samples"] and r["initial_pass"]
                                 and r["max_pass"] is True and r["peak_pass"] is not False for r in metrics)),
               "validated_against_pf": False, "parameters_fitted": False,
               "frequency_method": "PublishedFrequencyHistory PF-04, paso aceptado en marco G02",
               "event_output_side": "left", "step_s": .001, "output_s": .01,
               "undefined_channels": [r["signal"] for r in metrics if not r["complete_samples"]],
               "zero_voltage_phase_policy": "ángulo y frecuencia ausentes a V=0 sólido, nunca aceptados por muestras parciales",
               "integrator": "trapezoidal implícito, primer paso posevento Euler, límites proyectados tras aceptación",
               "input_sha256": provenance(rms, trial)}
    if avr_profile == "pf_identificado":
        summary.update(status=PF_IDENTIFIED_STATUS, avr_profile=avr_profile,
                       validated_against_blkdef=False, evidence_scope="FUERA DE MUESTRA")
    summary.update(freq_estimator=freq_estimator,
                   frequency_method=("PublishedFrequencyHistory PF-04, paso aceptado en marco G02"
                                     if freq_estimator == "accepted_step" else
                                     "PublishedFrequencyHistory en malla nominal 1 ms, fasor complejo lineal"),
                   interpolation_decision="linear_complex_interpolation",
                   frequency_estimator_validated_against_pf=False)
    audit = audit_variants(target, trial, time_s, reference, channels, variants)
    summary["identity_audit"] = audit
    write_json(target/"summary.json", summary)
    return summary


def campaign_manifest(trial, freq_estimator, *, decision_path=None):
    """Registra opciones, fuentes y decisión antes del cálculo físico."""
    record = Path(decision_path).expanduser().resolve() if decision_path is not None else None
    decision = (json.loads(record.read_text(encoding="utf-8")) if record is not None else
                {"timestamp": None, "interpolation": "complex_linear", "sensitivity_only": "polar_linear"})
    decision_hash = (sha256(record) if record is not None else hashlib.sha256(
        json.dumps(decision, sort_keys=True).encode("utf-8")).hexdigest())
    sources = [WORKSPACE/"pfcompare/frequency_observers.py",
               WORKSPACE/"dynamics/published_frequency_grid.py"]
    return {"timestamp": datetime.datetime.now().astimezone().isoformat(),
            "trial": trial, "freq_estimator": freq_estimator, "avr_profile": "pf_identificado",
            "decision_timestamp": decision["timestamp"], "decision_sha256": decision_hash, "decision_source": "caller_file" if record is not None else "built_in_policy",
            "tolerances": TOLERANCES, "nominal_step_s": .001, "output_s": .01,
            "interpolation": "complex_linear", "sensitivity_only": "polar_linear",
            "source_sha256": {str(p.relative_to(WORKSPACE)): sha256(p) for p in sources},
            "environment": {k: os.environ.get(k) for k in
                            ("OPENBLAS_NUM_THREADS", "PYTHONDONTWRITEBYTECODE")},
            "status": "sin validar contra PF"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_signals(path, time_s, predicted, channels):
    """Mismo formato y precisión que identified profile para comparación por hash."""
    with Path(path).open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["time"]+[ch["object"]["name"] for ch in channels])
        writer.writerow(["b:tnow in s"]+[ch["variable"]+" in "+ch["unit"] for ch in channels])
        for i, t in enumerate(time_s):
            writer.writerow([format(t, ".12g")]+[
                format(predicted[ch["signal"]][i], ".15g")
                if np.isfinite(predicted[ch["signal"]][i]) else "" for ch in channels])


def partial_writer(target, channels):
    """Checkpoints por segundo nuevos, sin borrar ni reemplazar evidencia."""
    folder = target/"partial"
    folder.mkdir()

    def save(time_s, rows, metrics, state):
        checkpoint = folder/f"t_{round(state.t):02d}s"
        checkpoint.mkdir()
        predicted = {ch["signal"]: np.array([row[ch["signal"]] for row in rows]) for ch in channels}
        write_signals(checkpoint/"signals_native.csv", time_s, predicted, channels)
        write_csv(checkpoint/"integration.csv", metrics)
        np.savez(checkpoint/"physical_state.npz", time_s=state.t, x=state.x, z=state.z, v=state.v)
    return save


def variant_metrics(time_s, reference, channels, predicted):
    metrics = [compare_channel(time_s, reference[ch["signal"]][:len(time_s)],
                               predicted[ch["signal"]], ch) for ch in channels]
    for row in metrics:
        row.update(status=PF_IDENTIFIED_STATUS, avr_profile="pf_identificado")
    return metrics


def audit_variants(target, trial, time_s, reference, channels, variants):
    """Audita todos los canales, ausencias y reproducción heredada completa."""
    baseline = variants["accepted_step"]
    frequency_metrics = []
    physical_hashes = []
    for name, predicted in variants.items():
        folder = target/name
        folder.mkdir()
        write_signals(folder/"signals_native.csv", time_s, predicted, channels)
        metrics = variant_metrics(time_s, reference, channels, predicted)
        write_csv(folder/"metrics.csv", metrics)
        windows = []
        for label, lower, upper in (("pre", 0., 1.), ("transient", 1., 3.), ("post", 3., 10.00001)):
            mask = (time_s >= lower)&(time_s < upper)
            for ch in channels:
                key = ch["signal"]
                windows.append({"window": label, "signal": key,
                                **signal_metrics(time_s[mask], reference[key][:len(time_s)][mask], predicted[key][mask])})
        write_csv(folder/"window_metrics.csv", windows)
        for row in metrics:
            if row["signal"] in ("B4_frequency_Hz", "B16_frequency_Hz"):
                frequency_metrics.append({"variant": name, **row})
        for ch in channels:
            key = ch["signal"]
            if ch["variable"] != "m:fehz":
                old_hash = hashlib.sha256(baseline[key].tobytes()).hexdigest()
                new_hash = hashlib.sha256(predicted[key].tobytes()).hexdigest()
                physical_hashes.append({"variant": name, "signal": key,
                                        "object_id": ch["object_id"], "relative_path": ch["relative_path"],
                                        "baseline_sha256_float64": old_hash,
                                        "variant_sha256_float64": new_hash, "identical": old_hash == new_hash})
    write_csv(target/"frequency_effect.csv", frequency_metrics)
    write_csv(target/"physical_channel_hashes.csv", physical_hashes)
    # identified profile contiene dos artefactos administrativos adicionales y un resumen
    # con duración y hashes de código. Solo los artefactos deterministas deben
    # tener hash idéntico, la duración no es una salida del modelo.
    original = OUTPUT_DIR/trial/"identified_profile"
    hashes = []
    baseline_files = {name: target/"accepted_step"/name for name in
                      ("signals_native.csv", "metrics.csv", "window_metrics.csv")}
    baseline_files.update({name: target/name for name in (
        "integration.csv", "events.csv", "equilibrium.json", "state_manifest.csv",
        "controller_coverage.csv", "references_from_lf.csv", "channel_units.csv",
        "tolerances_before_measurement.json")})
    for name, path in baseline_files.items():
        before, after = sha256(original/name), sha256(path)
        hashes.append({"file": name, "identified_profile_sha256": before,
                       "disabled_sha256": after, "identical": before == after})
    write_csv(target/"disabled_identified_profile_hashes.csv", hashes)
    sensitivity = []
    for ch in channels:
        if ch["variable"] != "m:fehz":
            continue
        key = ch["signal"]
        complex_hz = variants["nominal_grid"][key]
        polar_hz = variants["polar_sensitivity"][key]
        finite = np.isfinite(complex_hz)&np.isfinite(polar_hz)
        sensitivity.append({"signal": key, "object_id": ch["object_id"],
                            "relative_path": ch["relative_path"],
                            "max_complex_minus_polar_abs_hz": float(np.max(abs(complex_hz[finite]-polar_hz[finite]))),
                            "same_missing_mask": bool(np.array_equal(np.isnan(complex_hz), np.isnan(polar_hz))),
                            "same_float64_bytes": complex_hz.tobytes() == polar_hz.tobytes()})
    write_csv(target/"interpolation_sensitivity.csv", sensitivity)
    audit = {"disabled_deterministic_artifacts_identical": all(r["identical"] for r in hashes),
             "disabled_artifacts_compared": len(hashes),
             "physical_channels_per_variant": len([ch for ch in channels if ch["variable"] != "m:fehz"]),
             "physical_channels_float64_identical": all(r["identical"] for r in physical_hashes),
             "full_horizon_s": float(time_s[-1]), "samples": len(time_s),
             "frequency_variants_same_physical_solve": True,
             "status": "sin validar contra PF"}
    write_json(target/"identity_audit.json", audit)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--trial", required=True, choices=CASES)
    parser.add_argument("--freq-estimator", choices=("accepted_step", "nominal_grid"), default="accepted_step")
    parser.add_argument("--stop-s", type=float, default=10.)
    args = parser.parse_args()
    summary = run_trial(args.pf_dir, args.output_dir, args.trial, stop_s=args.stop_s,
                        progress=lambda s: print(s, flush=True), freq_estimator=args.freq_estimator)
    print(args.trial, summary["identity_audit"], flush=True)


if __name__ == "__main__":
    main()


