"""DIN-01/02/03 closed-network offline prediction and no-fit comparison."""
from __future__ import annotations
from power_machine.resources import output_root
from power_machine.resources import required_readonly_path

import argparse
import csv
import hashlib
from pathlib import Path
import time
import uuid

import numpy as np

from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, WORKSPACE, build_rms_case
from power_machine.dynamics.measurements import PublishedFrequencyHistory
from power_machine.dynamics.avr_ieeet1 import AVR_PROFILES, PF_IDENTIFIED_STATUS
from power_machine.loadflow import _stamp
from power_machine.pfcompare.rms_replay import CASES, MAX_ERROR_LIMIT, ANGLE_PEAK_LIMIT, INITIAL_LIMITS, inside_workspace, load_reference, signal_metrics, values, write_csv, write_json

OUTPUT_DIR = output_root() / "comparison_results/ieee39_pf"
TOLERANCES = {"max_normalized": MAX_ERROR_LIMIT, "first_peak_delay_relative": ANGLE_PEAK_LIMIT,
              "equilibrium": 1e-8}
STATUS = "not validated against PF, standard IEEE regulators without exported BlkDef"


def fresh_output(output_dir, trial):
    """Preserve previous deliveries in history without deleting or writing outside it."""
    target = inside_workspace(Path(output_dir)/trial/"rms_network")
    if target.exists():
        archive = inside_workspace(Path(output_dir)/"_historico"/"native RMS"/
                                   (trial+"_"+uuid.uuid4().hex))
        archive.parent.mkdir(parents=True, exist_ok=True)
        target.rename(archive)
    target.mkdir(parents=True, exist_ok=True)
    write_json(target/"tolerances_before_measurement.json", TOLERANCES)
    return target


def profile_output(output_dir, trial, avr_profile):
    """An identified profile requires a new destination and preserves all prior evidence."""
    if avr_profile not in AVR_PROFILES:
        raise ValueError("Unknown AVR profile: " + str(avr_profile))
    if avr_profile == "dyn_a":
        return fresh_output(output_dir, trial)
    if trial not in (*CASES, "E39-MOD-02"):
        raise ValueError("Unknown identified-profile trial")
    target = inside_workspace(Path(output_dir)/trial/"identified_profile")
    target.mkdir(parents=True, exist_ok=False)
    write_json(target/"tolerances_before_measurement.json", TOLERANCES)
    write_json(target/"model_profile.json", {
        "avr_profile": avr_profile, "status": PF_IDENTIFIED_STATUS,
        "saturation": "exponential", "freeze_vm_when_tr_zero": True,
        "selection_evidence": "EN-MUESTRA, pendientes locales de Amat",
        "din_evidence": "FUERA DE MUESTRA, trayectorias no usadas para seleccionar",
        "parameters_fitted": False, "validated_against_blkdef": False})
    return target


def provenance(rms, trial):
    """Hashes de fuentes físicas y código usado, sin incorporar objetivos al DAE."""
    root = rms.case.pf_dir
    files = [root/"modelo"/(name+".json") for name in
             ("types", "controllers", "composites", "generators", "loads")]
    files += [required_readonly_path("cases", "ieee39_pf", "model.json")]
    files += [WORKSPACE/name for name in (
        "pfcompare/rms_network.py", "pfcompare/modal_network.py", "dynamics/native_registry.py",
        "dynamics/ieee39_native.py", "dynamics/avr_ieeet1.py",
        "dynamics/governors_ieeeg.py", "dynamics/load_general.py",
        "dynamics/synchronous.py", "dynamics/solver.py", "dynamics/network.py",
        "dynamics/measurements.py")]
    if trial in CASES:
        files += [root/trial/name for name in ("configuration.json", "signals_native.csv", "channel_units.json")]
    else:
        files += [root/trial/name for name in ("Amat.csv", "Amat.mtl", "EVals.csv", "states.csv")]
    if rms.avr_profile == "pf_identificado":
        files += [WORKSPACE/name for name in (
            "benchmarks/ieee39_pf.py", "pfcompare/identified_profile.py", "dynamics/machine_coordinates.py")]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def sample_channels(rms, state, channels, frequency):
    """Predice 29 canales con IDs, orientación y unidades de PFC-DIN."""
    entity_by_path = {e["path"]: e for group in ("buses", "gens", "loads", "branches")
                      for e in rms.model[group]}
    devices = {d.object_id: (d, state.x[xs]) for d, xs in
               zip(rms.devices, rms.solver.x_slices)}
    buses = {b["id"]: b for b in rms.model["buses"]}
    voltage = rms.network.bus_voltages(state.v)
    result = {}
    for ch in channels:
        e = entity_by_path[ch["object"]["path"]]
        key, var = e["id"], ch["variable"]
        if e["class"] == "ElmTerm":
            v = voltage[key]
            value = {"m:u": abs(v), "m:phiu": np.rad2deg(np.angle(v)),
                     "m:fehz": frequency[key]}[var]
            if key in rms.solver.bolted_bus_ids and var in ("m:phiu", "m:fehz"):
                # Arg(0) y su derivada no están definidos. PF conserva una
                # tensión residual numérica, no hay fase física cotejable.
                value = np.nan
        elif e["class"] == "ElmSym":
            dev, x = devices[key]
            out = dev.outputs(x, voltage[e["bus"]])
            value = {"s:speed": out["xspeed"], "s:phi": out["phi"], "s:ve": out["ve"],
                     "m:P:bus1": out["P1_mw"], "m:Q:bus1": out["Q1_mvar"]}[var]
        elif e["class"] == "ElmLod":
            dev, x = devices[key]
            power = dev.consumed_power_mva(voltage[e["bus"]], x)
            value = power.real if var == "m:P:bus1" else power.imag
        else:
            if key in rms.network.open_branches:
                value = 0.
            else:
                ids, _, stamp = _stamp(e, buses, 100.)
                v = np.array([voltage[b] for b in ids])
                power = v*np.conj(stamp@v)*100.
                value = power[0 if var.endswith("bus1") else 1].real
        result[ch["signal"]] = float(value)
    return result


def frequency_voltages(rms, state):
    """La falla sólida no alimenta el estimador con fase de ruido redondeado."""
    return {bid: (0j if bid in rms.solver.bolted_bus_ids else v)
            for bid, v in rms.network.bus_voltages(state.v).items()}


def simulate(rms, trial, channels, *, stop_s=10., progress=None):
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
    history = PublishedFrequencyHistory(f_nom_hz=60.)
    freq = history.accept(0., frequency_voltages(rms, state))
    rows = [sample_channels(rms, state, channels, freq)]
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
                    rows.append(sample_channels(rms, state, channels, freq))
                state, applied = rms.apply_events(trial, event_t, state)
                events.extend(applied)
                freq = history.accept(state.t, frequency_voltages(rms, state), restart=True,
                                      f_ref_pu=float(state.x[rms.solver.reference_speed_index]))
                next_event += 1
        if len(rows) < round(t_out/output_s)+1:
            rows.append(sample_channels(rms, state, channels, freq))
        if progress and abs(t_out-round(t_out)) < 1e-12:
            progress(f"{trial} t={t_out:.0f} s, {time.monotonic()-started:.1f} s de cálculo")
    predicted = {ch["signal"]: np.array([row[ch["signal"]] for row in rows]) for ch in channels}
    return times, predicted, {"events": events, "steps": metrics,
                              "max_kcl_pu": max_kcl, "max_scaled_step_residual": max_step_residual,
                              "projections": rms.solver.projections,
                              "limit_events": rms.solver.limit_events,
                              "elapsed_s": time.monotonic()-started}


def compare_channel(time_s, reference, predicted, channel):
    """Sin ocultar saltos, ausencias ni canales casi constantes."""
    row = {"signal": channel["signal"], "unit": channel["unit"],
           "object_id": channel["object_id"], "relative_path": channel["relative_path"],
           **signal_metrics(time_s, reference, predicted)}
    peak = row.get("first_peak_difference_s")
    pf_peak = row.get("pf_first_peak_s")
    row["first_peak_delay_relative"] = abs(peak)/(pf_peak-1.) if peak is not None and pf_peak > 1. else None
    maximum = row.get("max_normalized")
    row["max_pass"] = bool(maximum <= MAX_ERROR_LIMIT) if maximum is not None else None
    relative = row["first_peak_delay_relative"]
    row["peak_pass"] = bool(relative <= ANGLE_PEAK_LIMIT) if relative is not None else None
    row["initial_absolute_error"] = float(abs(predicted[0]-reference[0]))
    row["initial_pass"] = row["initial_absolute_error"] <= INITIAL_LIMITS[channel["unit"]]
    row["max_tolerance"], row["peak_tolerance"] = MAX_ERROR_LIMIT, ANGLE_PEAK_LIMIT
    row["complete_samples"] = bool(np.isfinite(reference).all() and np.isfinite(predicted).all())
    if not row["complete_samples"]:
        row["max_pass"] = row["peak_pass"] = None
    row["criterion_status"] = ("complete coverage" if row["complete_samples"] else
                               "no complete criterion because samples are missing or phase is undefined at V=0")
    row["status"] = STATUS
    return row


def run_trial(pf_dir, output_dir, trial, *, stop_s=10., progress=print, avr_profile="dyn_a"):
    target = (fresh_output(output_dir, trial) if avr_profile == "dyn_a" else
              profile_output(output_dir, trial, avr_profile))
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
    time_s, predicted, diagnostics = simulate(rms, trial, channels, stop_s=stop_s, progress=progress)
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
        "zero_voltage_phase_policy": "angle and frequency unavailable at solid V=0, never accepted from partial samples",
        "integrator": "implicit trapezoidal, first post-event Euler step, limits projected after acceptance",
               "input_sha256": provenance(rms, trial)}
    if avr_profile == "pf_identificado":
        summary.update(status=PF_IDENTIFIED_STATUS, avr_profile=avr_profile,
                       validated_against_blkdef=False, evidence_scope="FUERA DE MUESTRA")
    write_json(target/"summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--trial", choices=CASES, action="append")
    parser.add_argument("--stop-s", type=float, default=10.)
    parser.add_argument("--avr-profile", choices=AVR_PROFILES, default="dyn_a")
    args = parser.parse_args()
    inside_workspace(args.output_dir)
    for trial in args.trial or CASES:
        summary = run_trial(args.pf_dir, args.output_dir, trial, stop_s=args.stop_s,
                            progress=lambda s: print(s, flush=True), avr_profile=args.avr_profile)
        print(trial, "max conformes", summary["max_pass_count"], "/29", flush=True)


if __name__ == "__main__":
    main()
