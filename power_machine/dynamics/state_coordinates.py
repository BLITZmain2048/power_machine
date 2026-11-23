'State coordinates support for power-system RMS dynamics.'
from power_machine.dynamics.errors import coded_error as _coded_error
import numpy as np

def state_transform(rms, pf_states):
    'Map engine coordinates to PF. G1 uses x0=-K*q and valve/steam states. G3 uses gate, lag, rate and reset states. AVR state conventions are explicit. Load coordinates may differ by a constant U0 shift.'
    index = {}
    for r in rms.manifest:
        path = r["relative_path"]
        short = "Grid\\"+path.split("Grid.ElmNet\\", 1)[1].replace(".ElmComp", "")
        index[(short, r["variable"])] = r["index"]
    controls = {c.object_id: c for d in rms.devices if hasattr(d, "machine")
                for c in (d.avr, d.governor) if c is not None}
    by_path = {r["relative_path"]: controls.get(r["object_id"]) for r in rms.manifest}
    transform = np.zeros((len(pf_states), len(rms.manifest)))
    mapping = []
    for i, r in enumerate(pf_states):
        name, scale = r["state"], 1.
        path = r["model"]
        control = next((v for k, v in by_path.items() if v is not None and
                       "Grid\\"+k.split("Grid.ElmNet\\", 1)[1].replace(".ElmComp", "") == path), None)
        if "\\AVR " in path:
            name = {"xa": "Vr_pu", "xe": "Efd_pu", "xf": "Efd_lag_pu", "xr": "Vmeas_pu"}[name]
            if r["state"] == "xf":
                scale = -control.parameters["Kf"]/control.parameters["Tf"]
        elif "\\GOV " in path:
            if control.n_states == 6:
                name = control.state_names[int(name[1:])]
                if r["state"] == "x0":
                    scale = -control.parameters["K"]
            else:
                name = dict(zip(("x", "x1", "xp", "xr"), control.state_names))[name]
        j = index[(path, name)]
        if scale == 0:
            raise _coded_error(ValueError, 'invalid_state_coordinates_data', 'Singular control state transform')
        transform[i, j] = scale
        mapping.append({"pf_index": i+1, "pm_index": j, "pf_model": path,
                        "pf_state": r["state"], "pm_state": name, "scale_pf_per_pm": scale,
                        "coordinate_status": 'Unvalidated IEEE assumption' if control else 'Native state'})
    if np.linalg.matrix_rank(transform) != len(pf_states):
        raise _coded_error(ValueError, 'invalid_state_coordinates_data', 'State map is not bijective')
    if rms.avr_profile == "pf_identificado":
        from power_machine.dynamics.machine_coordinates import machine_transform
        for i, r in enumerate(pf_states):
            if "\\AVR " in r["model"] and r["state"] == "xf":
                j = mapping[i]["pm_index"]
                transform[i, j] = 1.0
                mapping[i].update(scale_pf_per_pm=1.0,
                                  coordinate_status='Local regulator coordinate, xf=Xf')
        transform, mapping = machine_transform(transform, mapping, rms.manifest,
                                               pf_states, swap_q=True)
    return transform, mapping
