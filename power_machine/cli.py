"""English command-line interface for the public Power Machine API."""
from __future__ import annotations

import argparse
import importlib
from importlib.metadata import PackageNotFoundError, version as installed_version
import json
from pathlib import Path
import sys
from typing import Any

from . import __version__
from .api import (CctOptions, ContingencyOptions, PowerMachineError, QuasiDynamicOptions, cct,
                  compare_reference, contingency, default_output_dir, import_csv_export,
                  list_cases, load_case, loadflow, quasi_dynamic, rms, shortcircuit)

EXIT_SUCCESS, EXIT_INVALID, EXIT_FAILED, EXIT_PARTIAL = 0, 2, 3, 4
JSON_SCHEMA = "power-machine-cli-v1"


def _payload(value: Any) -> Any:
    """Return the public JSON payload without engine objects."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "to_records") and hasattr(value, "study"):
        records = value.to_records()
        complete = all(row.get("status") in {"completed", "limit_violation"} for row in records)
        return {"status": "completed" if complete else "partial", "coverage": {"complete": complete},
                "study": value.study, "cases": {"name": "cases", "units": {}, "records": records},
                "output": value.output, "model_sha256": value.model_sha256, "study_sha256": value.study_sha256}
    return value


def _emit(value: Any, as_json: bool) -> None:
    """Print a versioned JSON payload or concise human study summary."""
    payload = _payload(value)
    if as_json:
        print(json.dumps({"schema": JSON_SCHEMA, "result": payload}, ensure_ascii=False, indent=2, allow_nan=False))
        return
    if isinstance(payload, list):
        print("\n".join(str(item) for item in payload))
        return
    if not isinstance(payload, dict):
        print(payload)
        return
    if "environment" in payload and "resources" in payload:
        print("Power Machine doctor")
        print(f"status: {payload.get('status', 'unknown')}")
        print(f"version: {payload.get('version', 'unknown')}")
        print(f"Python: {payload['environment'].get('python', 'unknown')}")
        for name in ("numpy", "scipy", "tkinter", "matplotlib"):
            item = payload["environment"].get(name, {})
            version = item.get("version") if isinstance(item, dict) else None
            print(f"{name}: {version or 'not installed (optional)'}")
        ieee39 = payload["resources"].get("ieee39", {})
        if ieee39.get("loaded"):
            print(f"IEEE 39: loaded, {ieee39.get('buses', 'unknown')} buses")
            print(f"load flow: {ieee39.get('loadflow_status', 'unknown')}")
        else:
            print("IEEE 39: unavailable")
        diagnostics = payload.get("diagnostics", [])
        if diagnostics:
            print("diagnostics: " + ", ".join(str(item.get("code", "warning")) for item in diagnostics))
        return
    print(f"status: {payload.get('status', 'completed')}")
    coverage = payload.get("coverage", {})
    if coverage:
        print("coverage: " + ("complete" if coverage.get("complete") else "partial"))
    summary = payload.get("summary", {})
    if isinstance(summary, dict):
        if "converged" in summary:
            print(f"converged: {summary['converged']}")
        if "max_mismatch_mva" in summary:
            print(f"max mismatch: {summary['max_mismatch_mva']} MVA")
    buses = payload.get("buses", {})
    if isinstance(buses, dict):
        values = [row.get("u_pu") for row in buses.get("records", []) if row.get("u_pu") is not None]
        if values:
            print(f"voltage range: {min(values):.4f} to {max(values):.4f} p.u.")
    cases = payload.get("cases", {})
    if isinstance(cases, dict):
        rows = cases.get("records", [])
        print(f"evaluated cases: {len(rows)}")
        if rows:
            worst = max(rows, key=lambda row: row.get("max_loading_pct") or row.get("unserved_mw") or 0)
            print(f"worst case: {worst.get('id', worst.get('case_id', 'n/a'))}")
            violations = sum(len(row.get("violations", [])) for row in rows if isinstance(row.get("violations"), list))
            print(f"violations: {violations}")
    if payload.get("output"):
        print(f"results: {payload['output']}")
    diagnostics = payload.get("diagnostics", [])
    if diagnostics:
        print("diagnostics: " + ", ".join(str(item.get("code", "warning")) for item in diagnostics[:3]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="power-machine", description="Power-system studies in Python.",
        epilog="Exit codes: 0 success, 2 invalid input, 3 failed study, 4 partial coverage.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help=f"Emit {JSON_SCHEMA} JSON.")
    sub.add_parser("cases", parents=[common], help="List included cases. Example: power-machine cases --json")
    lf = sub.add_parser("lf", parents=[common], help="Run load flow. Example: power-machine lf ieee39")
    lf.add_argument("case")
    sc = sub.add_parser("sc", parents=[common], help="Run short circuit. Example: power-machine sc ieee39 --sc-data source.json --bus B01")
    sc.add_argument("case")
    sc.add_argument("--bus")
    sc.add_argument("--fault", default="3ph")
    sc.add_argument("--sc-data", required=True, help="Short-circuit source JSON.")
    n1 = sub.add_parser("n1", parents=[common], help="Run contingencies. Example: power-machine n1 ieee39 --max-cases 20")
    n1.add_argument("case")
    n1.add_argument("--elements", nargs="*")
    n1.add_argument("--max-cases", type=int, default=10000)
    dynamic = sub.add_parser("rms", parents=[common], help="Run RMS. Example: power-machine rms ieee39 --events events.json --t-end-s 2")
    dynamic.add_argument("case")
    dynamic.add_argument("--events", required=True, help="RMS event list JSON.")
    dynamic.add_argument("--t-end-s", type=float, required=True)
    dynamic.add_argument("--step-s", type=float, default=0.001)
    dynamic.add_argument("--output", help="Result folder under POWER_MACHINE_OUTPUT_DIR by default.")
    cct_parser = sub.add_parser("cct", parents=[common], help="Run CCT. Example: power-machine cct ieee39 --fault fault.json")
    cct_parser.add_argument("case")
    cct_parser.add_argument("--fault", required=True, help="CCT fault request JSON.")
    cct_parser.add_argument("--lower-clearing-s", type=float, default=0.05)
    cct_parser.add_argument("--upper-clearing-s", type=float, default=0.35)
    cct_parser.add_argument("--resolution-ms", type=float, default=5.0)
    cct_parser.add_argument("--end-s", type=float, default=5.0)
    cct_parser.add_argument("--step-ms", type=float, default=2.0)
    qds = sub.add_parser("qds", parents=[common], help="Run QSS. Example: power-machine qds ieee39 --series series.json")
    qds.add_argument("case")
    qds.add_argument("--series", required=True, help="QSS Series JSON with name, steps, and provenance.")
    qds.add_argument("--output", help="New result folder under POWER_MACHINE_OUTPUT_DIR by default.")
    imp = sub.add_parser("import-csv", parents=[common], help="Import element-table CSV. Example: power-machine import-csv export -o network.json")
    imp.add_argument("directory")
    imp.add_argument("-o", "--output", required=True)
    comparison = sub.add_parser("compare-reference", parents=[common], help="Compare REF export. Example: power-machine compare-reference ieee39 cases/ieee39_ref")
    comparison.add_argument("case")
    comparison.add_argument("directory")
    comparison.add_argument("--study", choices=("loadflow", "shortcircuit", "network"), default="loadflow")
    sub.add_parser("doctor", parents=[common], help="Inspect IEEE 39 resources and optional dependencies.")
    batch = sub.add_parser("batch", parents=[common], help="Run a batch manifest. Example: power-machine batch scenarios.json --workers 1")
    batch.add_argument("scenario_file", help="JSON with case, study, and scenarios.")
    batch.add_argument("--workers", type=int)
    batch.add_argument("--output", help="Batch output folder under POWER_MACHINE_OUTPUT_DIR by default.")
    gui = sub.add_parser("gui", help="Open the desktop application. Example: power-machine gui ieee39")
    gui.add_argument("case", nargs="?")
    sub.add_parser("mcp", help="Start the MCP stdio server. Example: power-machine mcp")
    return parser


def _dependency(name: str) -> dict[str, Any]:
    """Describe an optional runtime dependency without making it required."""
    if importlib.util.find_spec(name) is None:
        return {"available": False, "version": None}
    if name == "tkinter":
        return {"available": True, "version": "available"}
    try:
        version = installed_version(name)
    except PackageNotFoundError:
        version = "available"
    return {"available": True, "version": version}


def _doctor() -> dict[str, Any]:
    """Run the minimum import, resource, and load-flow health check."""
    environment = {"python": sys.version.split()[0], **{name: _dependency(name)
                   for name in ("numpy", "scipy", "tkinter", "matplotlib")}}
    report: dict[str, Any] = {"status": "completed", "version": __version__, "environment": environment,
                              "resources": {"ieee39": {"loaded": False}}, "results": str(default_output_dir())}
    try:
        package = importlib.import_module("power_machine")
        network = package.load_case("ieee39")
        flow = package.loadflow(network)
        converged = bool(flow.summary.get("converged"))
        report["resources"]["ieee39"] = {"loaded": True, "buses": len(network.to_model()["buses"]),
                                            "loadflow_status": flow.status, "converged": converged}
        if flow.status != "completed" or not converged:
            raise RuntimeError("IEEE 39 load flow did not converge")
    except Exception as exc:
        report["status"] = "failed"
        report["diagnostics"] = [{"code": "doctor_package_or_case_failed", "level": "error", "message": str(exc)}]
    return report


def _exit_code(value: Any) -> int:
    payload = _payload(value)
    if not isinstance(payload, dict):
        return EXIT_SUCCESS
    status = payload.get("status", "completed")
    if status in {"partial", "cancelled", "unsupported"} or payload.get("coverage", {}).get("complete") is False:
        return EXIT_PARTIAL
    return EXIT_FAILED if status in {"error", "failed", "invalid_input"} else EXIT_SUCCESS


def _run_batch(path: str, workers: int | None, output: str | None):
    """Run a documented batch manifest through the batch package."""
    from .batch import BatchOptions, Scenario, run_batch
    manifest = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("Batch manifest must be a JSON object")
    scenarios = [Scenario(**item) for item in manifest["scenarios"]]
    destination = Path(output) if output else default_output_dir() / "batch"
    if not destination.is_absolute():
        destination = default_output_dir() / destination
    configured = dict(manifest.get("options", {}))
    if workers is not None:
        configured["workers"] = workers
    return run_batch(load_case(manifest.get("case", "ieee39")), manifest.get("study", "loadflow"), scenarios,
                     study_options=manifest.get("study_options"), options=BatchOptions(**configured), output=destination)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its documented exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "cases":
            value = list_cases()
        elif args.command == "lf":
            value = loadflow(load_case(args.case))
        elif args.command == "sc":
            value = shortcircuit(load_case(args.case), bus=args.bus, fault=args.fault, sc_data=Path(args.sc_data))
        elif args.command == "n1":
            value = contingency(load_case(args.case), elements=args.elements, options=ContingencyOptions(max_cases=args.max_cases))
        elif args.command == "rms":
            from .api import RmsOptions
            value = rms(load_case(args.case), json.loads(Path(args.events).read_text(encoding="utf-8-sig")),
                        t_end_s=args.t_end_s, step_s=args.step_s, options=RmsOptions(output=args.output))
        elif args.command == "cct":
            settings = CctOptions(lower_clearing_s=args.lower_clearing_s, upper_clearing_s=args.upper_clearing_s,
                                  resolution_ms=args.resolution_ms, end_s=args.end_s, step_ms=args.step_ms)
            value = cct(load_case(args.case), json.loads(Path(args.fault).read_text(encoding="utf-8-sig")), options=settings)
        elif args.command == "qds":
            from .qss.series import Series, SeriesStep
            source = json.loads(Path(args.series).read_text(encoding="utf-8-sig"))
            series = Series(source["name"], tuple(SeriesStep(**row) for row in source["steps"]), source.get("provenance", {}))
            value = quasi_dynamic(load_case(args.case), series, options=QuasiDynamicOptions(output=args.output))
        elif args.command == "import-csv":
            network = import_csv_export(args.directory)
            value = {"status": "completed", "saved": str(network.save(args.output)), "network": network.summary()}
        elif args.command == "compare-reference":
            value = compare_reference(load_case(args.case), args.directory, args.study)
        elif args.command == "doctor":
            value = _doctor()
        elif args.command == "batch":
            value = _run_batch(args.scenario_file, args.workers, args.output)
        elif args.command == "gui":
            from .gui import main as gui_main
            gui_main(case=args.case)
            return EXIT_SUCCESS
        else:
            from .mcp import main as mcp_main
            return int(mcp_main() or EXIT_SUCCESS)
        _emit(value, args.json)
        return _exit_code(value)
    except (PowerMachineError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        payload = exc.to_dict() if isinstance(exc, PowerMachineError) else {"code": "invalid_input", "message": str(exc)}
        _emit({"status": "invalid_input", "diagnostics": [payload]}, getattr(args, "json", False))
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
