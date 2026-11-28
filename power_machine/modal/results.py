'Results support for power-system modal analysis.'
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import copy
import hashlib
import json
from pathlib import Path
from power_machine.resources import confined_output, dynamic_catalog_root, output_root
import uuid
import numpy as np

from .adapter import (WORKSPACE, LinearDAEAdapter, file_input, inside_root,
                      prepare_machine, prepare_rms)
from .eigen import solve_modes
from .linearize import linearize, reduce_angle_reference
from .schema import (ModalError, ModalOptions, OperatingPoint, StateRef,
                     check_cancel, diagnostic, fail, real_vector)


def fingerprint(value):
    'SHA-256 of sorted compact UTF-8 JSON without nonfinite numbers.'
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def dumps_result(result):
    return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)


def write_result(result, directory, root=None):
    'Write a new result directory without replacing previous evidence.'
    root = output_root() if root is None else Path(root).expanduser().resolve()
    directory = confined_output(Path(directory) if Path(directory).is_absolute() else root / directory, root)
    allowed = [root/"results/archive/modal",
               root/"results/parity/modal", root/"results/parity/modal"]
    if not any(directory == base or directory.is_relative_to(base) for base in allowed):
        fail('invalid_input', 'output_directory', 'Output must belong to the modal results directory')
    run_id = result["run_id"]
    if not run_id or Path(run_id).name != run_id or run_id in (".", "..") or any(c in run_id for c in "/\\:"):
        fail('invalid_input', 'run_id', 'run_id is not a valid run name')
    text = dumps_result(result)
    target = inside_root(directory/run_id, root)
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        fail('invalid_input', 'run_exists', 'Run already exists. Overwriting is disabled')
    path = target/"result.json"
    temporary = target/"result.pending.json"
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text+"\n")
    temporary.rename(path)
    return path


def _analytical_input(data):
    x, z = real_vector(data["x"], "x"), real_vector(data.get("z", []), "z")
    n, m = len(x), len(z)
    matrices = []
    for key, shape in (("fx", (n, n)), ("fz", (n, m)), ("gx", (m, n)), ("gz", (m, m))):
        raw = np.asarray(data[key]) if key in data else np.zeros(shape)
        if key not in data and shape[0]*shape[1]:
            fail('invalid_input', 'missing_matrix', 'Missing matrix ')
        empty_json = raw.shape == (0,) and not shape[0]*shape[1]
        if (np.iscomplexobj(raw) or (raw.shape != shape and not empty_json)
                or raw.dtype.kind not in "fiu"):
            fail('invalid_input', 'real_matrix_required', ' must be a real matrix of shape ')
        value = raw.astype(float).reshape(shape)
        if not np.isfinite(value).all():
            fail('invalid_input', 'nonfinite_matrix', ' contains nonfinite values')
        matrices.append(value)
    states = tuple(StateRef(**row) for row in data["states"])
    point = OperatingPoint(x, z, float(data.get("time_s", 0.)), states,
        real_vector(data.get("algebraic_scales", []), 'Algebraic scales'),
        {"profile": "analytical_linear_dae", "angular_frame": data.get("angular_frame", 'Fixture-declared frame'),
         "coverage": [{"object_id": s.object_id, "relative_path": s.relative_path, "status": "analytical_fixture"}
                      for s in states]})
    return LinearDAEAdapter(*matrices), point


def _envelope(request):
    return {"schema": "pm-parity-result-v1", "module": "modal",
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid.uuid4().hex[:12],
        "model_sha256": None, "variant_sha256": None, "study_sha256": None,
        "options": {}, "status": "invalid_input", "units": {"lambda": "1/s", "frequency_hz": "Hz",
            "damping_ratio": "1", "decay_rate_per_s": "1/s", "participation": "1",
            "s_base_mva": "MVA", "f_nom_hz": "Hz", "bus_voltage": 'p.u. line-to-line'},
        "provenance": {"inputs": [], "formulas": [{"standard": 'Index-1 DAE algebra',
            "edition": 'Own modal derivation', "profile": "continuous_index1_real",
            "definition": 'A=fx-fz solve(gz,gx), W=solve(V,I), p_ki=V_ki W_ik'}],
            "analytical_checks": [], "normative_evidence": [], "ref_comparison": 'not validated against the external reference'},
        "diagnostics": [], "payload": {}}


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    'Execute a serializable module-level modal request without tkinter. Profiles full_rms, machine_admittance and analytical_linear_dae have explicit coverage.'
    result = _envelope(request)
    try:
        if not isinstance(request, dict):
            fail('invalid_input', 'request_type', 'Request must be a serializable dictionary')
        json.dumps(request, allow_nan=False)
        request = copy.deepcopy(request)
        if request.get("schema") != "pm-parity-request-v1" or request.get("module") != "modal":
            fail('invalid_input', 'request_schema', 'Incorrect modal schema or study identifier')
        root = Path(request["root"]).resolve()
        snapshot = request["snapshot"]
        if not isinstance(snapshot, dict):
            fail('invalid_input', 'request_section', 'snapshot must be a dictionary')
        model, study, hashes = snapshot["model"], snapshot["study"], snapshot["fingerprints"]
        sections = {"model": model, "study": study, "fingerprints": hashes,
                    "options": request.get("options", {}), "input": request.get("input", {})}
        for name, value in sections.items():
            if not isinstance(value, dict):
                fail('invalid_input', 'request_section', ' must be a dictionary')
        result["model_sha256"], result["study_sha256"] = fingerprint(model), fingerprint(study)
        for key in ("model_sha256", "study_sha256"):
            if hashes.get(key) != result[key]:
                fail('invalid_input', 'fingerprint_mismatch', ' is missing or differs from the snapshot')
        variant_hash = hashes.get("variant_sha256")
        if variant_hash is not None and (not isinstance(variant_hash, str) or len(variant_hash) != 64
                                        or any(c not in "0123456789abcdef" for c in variant_hash)):
            fail('invalid_input', 'variant_fingerprint', 'Invalid variant fingerprint')
        result["variant_sha256"] = variant_hash
        # La variante ya debe venir materializada. Se conserva su huella como
        # procedencia, no se lee ni materializa un workspace desde este módulo.
        result["provenance"]["inputs"] = [{"path": "snapshot/model", "sha256": result["model_sha256"]},
                                           {"path": "snapshot/study", "sha256": result["study_sha256"]},
                                           {"path": "request/input", "sha256": fingerprint(request.get("input", {}))}]
        options = ModalOptions(**request.get("options", {}))
        result["options"] = asdict(options)
        check_cancel(cancel)
        data = request.get("input", {})
        profile = data.get("profile", "full_rms")
        if profile != "analytical_linear_dae":
            frequency = model.get("meta", {}).get("f_nom_hz")
            if (model["meta"].get("s_base_mva") != 100. or
                    not isinstance(frequency, (int, float)) or
                    not np.isfinite(frequency) or frequency <= 0):
                fail('invalid_input', 'electrical_base',
                     'RMS profile requires 100 MVA and an explicit positive finite meta.f_nom_hz')
        if progress:
            progress({"phase": "initialization", "profile": profile})
        if profile == "analytical_linear_dae":
            adapter, point = _analytical_input(data)
            result["provenance"]["analytical_checks"] = ['Explicit matrices, residual and step sensitivity']
        elif profile == "machine_admittance":
            adapter, point = prepare_machine(root, model, data)
        elif profile == "full_rms":
            directory = dynamic_catalog_root(root)
            for name in ("manifest.json", "objects.csv", "attributes.csv", "references.csv"):
                result["provenance"]["inputs"].append(file_input(directory/name, root))
            adapter, point = prepare_rms(root, model, study, data)
        else:
            fail('unsupported', 'unknown_profile', 'Unsupported modal profile')
        result["provenance"]["inputs"] += point.metadata.get("source_inputs", [])
        result["payload"] = {"states": [asdict(s) for s in point.states],
            "operating_point": {"x": point.x.tolist(), "z": point.z.tolist(), "time_s": point.time_s,
                                "algebraic_scales": point.algebraic_scales.tolist(),
                                "states": [asdict(s) for s in point.states]},
            "metadata": point.metadata, "coverage": point.metadata.get("coverage", []),
            "missing_by_id": point.metadata.get("unsupported_features", [])}
        if point.metadata.get("unsupported_features"):
            # Reportar también residuos propios aunque la cobertura rechace el modal.
            f, g = adapter.clone().evaluate(point.x, point.z, point.time_s)
            result["payload"]["residuals"] = {"f": f.tolist(), "g": g.tolist(),
                "max_f_per_s": float(np.max(abs(f)/np.array([s.scale for s in point.states]), initial=0)),
                "max_g": float(np.max(abs(g)/point.algebraic_scales, initial=0))}
        lin = linearize(adapter, point, options, cancel=cancel, progress=progress)
        states = point.states
        frame = data.get("angle_reference")
        if data.get("reduce_rotor_frame"):
            angles = [s.index for s in states if s.variable == "rotor_angle_rad"]
            reference = point.metadata.get("reference_angle_index")
            if reference is None:
                reference = angles[0] if len(angles) == 1 else None
            if reference is None:
                fail('invalid_input', 'ambiguous_angle_reference', 'Select the rotor reference explicitly')
            frame = {"angle_indexes": angles, "reference_index": reference}
        if frame is not None:
            lin, states = reduce_angle_reference(lin, states, frame["angle_indexes"], frame["reference_index"])
        check_cancel(cancel)
        modes = solve_modes(lin, states, options)
        result["status"] = "partial" if point.metadata.get("partial_model") else modes.status
        result["diagnostics"] = list(modes.diagnostics)
        result["payload"].update({"states": [asdict(s) for s in states],
            "linearization": {key: getattr(lin, key).tolist() for key in ("fx", "fz", "gx", "gz", "a")},
            "modes": list(modes.modes), "participation": None if modes.participation is None else list(modes.participation)})
        if point.metadata.get("partial_model"):
            result["diagnostics"].append(diagnostic('partial_submodel',
                'Modes of the explicit machine-admittance submodel. External network and controls are excluded', level="warning"))
        check_cancel(cancel)
        dumps_result(result)
        if data.get("output_directory"):
            directory = inside_root(data["output_directory"], root)
            result["payload"]["result_path"] = (directory/result["run_id"]/"result.json").relative_to(root).as_posix()
            path = write_result(result, data["output_directory"], root)
    except ModalError as exc:
        result["status"] = exc.status
        result["diagnostics"] = list(exc.diagnostics)
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        result["status"] = "invalid_input"
        result["diagnostics"] = [diagnostic('invalid_input', str(exc), level="error")]
    except (OSError, RuntimeError, np.linalg.LinAlgError) as exc:
        result["status"] = "numerical_failure"
        result["diagnostics"] = [diagnostic('runtime_failure', f'{type(exc).__name__}: {exc}', level="error")]
    try:
        dumps_result(result)
    except (ValueError, TypeError):
        result["status"] = "numerical_failure"
        result["payload"] = {}
        result["diagnostics"] = [diagnostic('nonserializable_result',
            'Result contains nonfinite or unserializable data. Payload omitted', level="error")]
        dumps_result(result)
    return result
