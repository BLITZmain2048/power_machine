"""Run the offline IEEE 39 validation campaign.

Run ``python -m validation`` from the repository root.  The command uses only
the included PowerFactory exports and writes a timestamped, immutable result
folder below ``validation/results``.  It never changes model parameters or
tolerances after measurements have started.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .analytical import run_cases


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_ROOT = Path(__file__).resolve().parent
TOLERANCE_PATH = VALIDATION_ROOT / "tolerances.json"


def _json_default(value):
    """Convert result scalars without relaxing JSON's finite-number contract."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Not JSON serialisable: {type(value).__name__}")


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default) + "\n", encoding="utf-8")


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _bool(value):
    return value is True or str(value).lower() == "true"


def _finite_max(rows, field):
    values = []
    for row in rows:
        try:
            value = float(row[field])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return max(values) if values else None


def _load_inputs():
    from power_machine.resources import required_readonly_path
    model = json.loads(required_readonly_path("cases", "ieee39", "model.json").read_text(encoding="utf-8"))
    dynamic = json.loads(required_readonly_path("cases", "ieee39", "dynamic_data.json").read_text(encoding="utf-8"))
    return model, dynamic


def _classical_signature(run):
    sample = run.samples[-1]
    return np.r_[sample.state.ravel(), sample.voltage_pu.real, sample.voltage_pu.imag,
                 sample.generator_power_pu.real, sample.generator_power_pu.imag, sample.bus_frequency_hz,
                 sample.coi_frequency_hz]


def _observed_order(coarse, medium, fine):
    numerator, denominator = np.linalg.norm(coarse - medium), np.linalg.norm(medium - fine)
    if numerator == 0.0 and denominator == 0.0:
        return None
    if denominator == 0.0:
        # Exact agreement to floating-point round-off has no finite fitted
        # order.  Returning None keeps the JSON report standards-compliant.
        return None
    return float(math.log(numerator / denominator, 2.0))


def _classical_convergence(tolerances):
    from power_machine.benchmarks.ieee39_dynamics import ClassicalSystem, critical_clearing_time, simulate
    model, dynamic = _load_inputs()
    h, h2, h4 = tolerances["steps_s"]
    end_s = tolerances["comparison_time_s"]
    vectors = []
    for step in (h, h2, h4):
        run = simulate(ClassicalSystem(model, dynamic), "BASE", end_s=end_s, step_s=step)
        vectors.append(_classical_signature(run))
    order = _observed_order(*vectors)
    # CCT deliberately has a separate resolution result because its bisection
    # and finite-horizon stability classification are not a smooth ODE norm.
    cct = []
    for step in (h, h2, h4):
        value = critical_clearing_time(model, dynamic, end_s=5.0, step_s=step, tolerance_s=0.004)
        cct.append({"step_s": step, "estimate_s": value.get("estimate_s"), "status": value["status"],
                    "trials": len(value["trials"])})
    return {"engine": "classical_rk4", "event": "BASE", "comparison_time_s": end_s, "steps_s": [h, h2, h4],
            "observed_order": order, "criterion": ">= " + str(tolerances["minimum_observed_order_classical_rk4"]),
            "pass": order is None or order >= tolerances["minimum_observed_order_classical_rk4"],
            "order_note": "Exact-to-roundoff equilibrium" if order is None else "Measured on a smooth interval",
            "cct_b16": cct,
            "cct_resolution_note": "CCT B16 is a resolution study, not a global temporal-order assertion."}


def _native_convergence(tolerances):
    from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, build_rms_case
    from power_machine.pfcompare.rms_network import simulate
    from power_machine.pfcompare.rms_replay import load_reference
    h, h2, h4 = tolerances["steps_s"]
    end_s = tolerances["comparison_time_s"]
    _, _, channels = load_reference(DEFAULT_PF_DIR / "E39-DIN-03")
    vectors = []
    diagnostics = []
    for step in (h, h2, h4):
        rms = build_rms_case(DEFAULT_PF_DIR, step_s=step)
        _, predicted, detail = simulate(rms, "E39-DIN-03", channels, stop_s=end_s, progress=None)
        vectors.append(np.asarray([predicted[channel["signal"]][-1] for channel in channels], dtype=float))
        diagnostics.append({"step_s": step, "state_count": rms.solver.nx, "max_kcl_pu": detail["max_kcl_pu"],
                            "max_scaled_step_residual": detail["max_scaled_step_residual"]})
    order = _observed_order(*vectors)
    return {"engine": "native_trapezoidal", "trial": "E39-DIN-03", "event": "none before 1.0 s",
            "comparison_time_s": end_s, "steps_s": [h, h2, h4], "observed_order": order,
            "criterion": ">= " + str(tolerances["minimum_observed_order_native_trapezoidal"]),
            "pass": order is None or order >= tolerances["minimum_observed_order_native_trapezoidal"],
            "order_note": "Exact-to-roundoff equilibrium" if order is None else "Measured on a smooth interval",
            "diagnostics": diagnostics}


def _native_trials(output, tolerances):
    from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR
    from power_machine.pfcompare.rms_network import run_trial
    trials = []
    for trial in ("E39-DIN-01", "E39-DIN-02", "E39-DIN-03"):
        summary = run_trial(DEFAULT_PF_DIR, output / "native", trial, stop_s=tolerances["horizon_s"], progress=None)
        metrics = _read_csv(output / "native" / trial / "rms_network" / "metrics.csv")
        failures = [row for row in metrics if not (_bool(row.get("max_pass")) and row.get("peak_pass", "") in ("", "True", "true"))]
        trials.append({"trial": trial, "summary": summary, "channels": metrics, "failed_or_incomplete_channels": failures,
                       "pass": len(failures) == 0})
    return trials


def _static_campaign(output):
    from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR
    from power_machine.pfcompare import cc, lf, net
    from power_machine.benchmarks.ieee39_pf import LDF_OPTIONS, build_model
    from power_machine.loadflow import run_loadflow
    # The distributed export contains E39-LF-01 but not the optional CT folders.
    # Call the public single-case comparator rather than the campaign helper,
    # which currently assumes those absent folders exist.
    lf_model = build_model(DEFAULT_PF_DIR)
    lf_comparison = lf.compare_results(lf_model, run_loadflow(lf_model, options=LDF_OPTIONS), DEFAULT_PF_DIR / "E39-LF-01")
    lf.write_comparison(lf_comparison, output / "load_flow" / "E39-LF-01")
    lf_result = {"E39-LF-01": lf_comparison}
    network_reference = DEFAULT_PF_DIR / "E39-NET-01"
    if network_reference.is_dir():
        net_result = net.run_comparisons(DEFAULT_PF_DIR, output / "network")
        network_summary = {"variants": {key: bool(value["pass"]) for key, value in net_result.items()},
                           "pass": all(value["pass"] for value in net_result.values()), "status": "compared"}
    else:
        # Do not substitute an internal round trip for an external reference.
        network_summary = {"variants": {}, "pass": False, "status": "reference_not_included",
                           "detail": "E39-NET-01 is not present under cases/ieee39_pf."}
    sc_result = cc.run_comparisons(DEFAULT_PF_DIR, output / "short_circuit")
    sc_summary = [{key: row[key] for key in ("metric", "maximum", "median", "measured", "passed", "total")}
                  for row in sc_result["summary"]]
    # RtoX and derived kappa are diagnostic rows without an acceptance count.
    # They remain in the report but do not masquerade as failed channels.
    sc_pass = all(row["passed"] is None or int(row["passed"]) == int(row["total"]) for row in sc_summary)
    return {"load_flow": {"cases": {key: bool(value["pass"]) for key, value in lf_result.items()},
                           "pass": all(value["pass"] for value in lf_result.values())},
            "network": network_summary,
            "short_circuit": {"fault_buses": len(sc_result["buses"]), "summary": sc_summary,
                              "worst": sc_result["worst"], "pass": sc_pass}}


def _markdown(result):
    lines = ["# IEEE 39 validation report", "", "This report is generated by `python -m validation`. Tolerances were read from `validation/tolerances.json` before execution.",
             "", "## Outcome", "", f"Overall result: **{'PASS' if result['pass'] else 'FAIL'}**.", "",
             "## Static PowerFactory comparisons", "", "| Study | Result | Coverage |", "| --- | --- | --- |"]
    static = result["static"]
    lines.extend([f"| Load flow | {'PASS' if static['load_flow']['pass'] else 'FAIL'} | {len(static['load_flow']['cases'])} cases |",
                  f"| Network variants | {'PASS' if static['network']['pass'] else 'FAIL'} | {len(static['network']['variants'])} variants |",
                  f"| 3-phase short circuit | {'PASS' if static['short_circuit']['pass'] else 'FAIL'} | {static['short_circuit']['fault_buses']} buses |", "",
                  "## Native RMS PowerFactory comparison", "", "All included channels are listed. A blank criterion is not silently counted as a pass.",
                  "", "| Trial | Channel | Unit | Maximum normalized error | Max pass | Peak pass | Complete samples |", "| --- | --- | --- | ---: | --- | --- | --- |"])
    for trial in result["native_rms"]:
        for channel in trial["channels"]:
            lines.append("| {trial} | {signal} | {unit} | {error} | {maximum} | {peak} | {complete} |".format(
                trial=trial["trial"], signal=channel["signal"], unit=channel["unit"], error=channel.get("max_normalized", ""),
                maximum=channel.get("max_pass", ""), peak=channel.get("peak_pass", ""), complete=channel.get("complete_samples", "")))
    lines.extend(["", "## Analytical cases", "", "| Case | Result | Equation |", "| --- | --- | --- |"])
    for case in result["analytical"]:
        lines.append(f"| {case['case']} | {'PASS' if case['pass'] else 'FAIL'} | {case['equation']} |")
    lines.extend(["", "## Temporal convergence", "", "| Engine | Smooth interval | Observed order | Result |", "| --- | ---: | ---: | --- |"])
    for entry in result["convergence"]:
        order = "n/a" if entry["observed_order"] is None else f"{entry['observed_order']:.3f}"
        lines.append(f"| {entry['engine']} | {entry['comparison_time_s']:.3f} s | {order} | {'PASS' if entry['pass'] else 'FAIL'} |")
    lines.extend(["", "The classical CCT B16 resolution results are in `summary.json`. They are not presented as a global order estimate because bisection, events and finite-horizon classification are discontinuous.",
                  "", "## Limits", "", "The native RMS result is limited to the included IEEE 39 export, the stated settings and the 1.2 s horizon. Classical RMS is reported separately and has no PowerFactory comparison label. See `CAPABILITIES.md` and `DYNAMIC_DATA_PROVENANCE.md`."])
    return "\n".join(lines) + "\n"


def run(output=None, *, quick=False):
    """Execute the campaign and return its JSON summary."""
    tolerances = json.loads(TOLERANCE_PATH.read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(output) if output else VALIDATION_ROOT / "results" / stamp
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Some existing comparison helpers intentionally confine their output to this root.
    os.environ["POWER_MACHINE_OUTPUT_DIR"] = str(output)
    _write_json(output / "tolerances_before_run.json", tolerances)
    analytical = run_cases(tolerances["analytical"])
    if quick:
        result = {"schema": "power-machine-validation-summary-v1", "mode": "quick", "tolerances": tolerances,
                  "analytical": analytical, "pass": all(item["pass"] for item in analytical)}
    else:
        static = _static_campaign(output)
        native = _native_trials(output, tolerances["native_rms"])
        convergence = [_classical_convergence(tolerances["temporal_convergence"]), _native_convergence(tolerances["temporal_convergence"])]
        result = {"schema": "power-machine-validation-summary-v1", "mode": "full", "tolerances": tolerances,
                  "analytical": analytical, "static": static, "native_rms": native, "convergence": convergence,
                  "pass": all(item["pass"] for item in analytical) and static["load_flow"]["pass"] and static["network"]["pass"]
                  and static["short_circuit"]["pass"] and all(item["pass"] for item in native) and all(item["pass"] for item in convergence)}
        (output / "report.md").write_text(_markdown(result), encoding="utf-8")
    _write_json(output / "summary.json", result)
    return result, output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New destination below the current repository")
    parser.add_argument("--quick", action="store_true", help="Run only closed-form analytical validation")
    args = parser.parse_args(argv)
    result, output = run(args.output, quick=args.quick)
    print(output)
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

