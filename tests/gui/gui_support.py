"""Recursos de prueba públicos y fuente SC sintética explícita."""
from pathlib import Path
from power_machine.resources import resource_root, ieee39_root, output_root
from power_machine.shortcircuit.results import fingerprint
ROOT = resource_root()

def ieee_root():
    return ieee39_root()

def ieee_model():
    from power_machine.study import load_network
    return load_network(ieee_root() / 'model.json')

import pytest


def datum(value, unit="ohm"):
    if isinstance(value, complex):
        value = {"real": value.real, "imag": value.imag}
    return {"value": value, "unit": unit, "analytical_fixture": "critical clearing time"}

def fixture():
    model = {"meta": {"s_base_mva": 100.0, "f_nom_hz": 50.0}, "buses": [
        {"id": "B1", "object_id": "B1", "name": "Fuente", "scope": "ensayo", "path": "fixture/B1", "un_kv": 10.0},
        {"id": "B2", "object_id": "B2", "name": "Carga", "scope": "ensayo", "path": "fixture/B2", "un_kv": 10.0}],
        "branches": [{"id": "L1", "object_id": "L1", "class": "ElmLne", "path": "fixture/L1",
                      "bus_from": "B1", "bus_to": "B2"}],
        "gens": [{"id": "S1", "object_id": "S1", "class": "ElmXnet", "path": "fixture/S1", "bus": "B1"}]}
    supplemental = {"schema": "power_machine-sc-data-v1", "elements": {"L1": {"parameters": {
        "z1": datum(0.05 + 0.1j), "z2": datum(0.05 + 0.1j), "z0": datum(0.15 + 0.3j)}}},
        "sources": {"S1": {"kind": "external", "parameters": {
            "z1": datum(0.1 + 0.2j), "z2": datum(0.1 + 0.2j), "z0": datum(0.3 + 0.6j)}}}}
    model['schema'] = 'power_machine-model-v1'
    for group in ('buses', 'branches', 'gens'):
        for item in model[group]:
            item.setdefault('name', item['id'])
            item.setdefault('class', 'ElmTerm' if group == 'buses' else 'ElmLne')
            item.setdefault('relative_path', item['path'])
    return model, supplemental

def request_for(model=None, supplemental=None, *, selected=("B1", "B2")):
    model, supplemental = (model, supplemental) if model is not None else fixture()
    study = {"schema": "power_machine-study-v1", "scenario": {}}
    return {"schema": "power_machine-sc-sweep-request-v1", "module": "sc_sweep", "root": str(ROOT),
            "snapshot": {"model": model, "study": study, "fingerprints": {
                "model_sha256": fingerprint(model), "study_sha256": fingerprint(study), "variant_sha256": None}},
            "options": {"method": "iec60909", "case": "maximum", "frequency_hz": 50.0,
                        "fault_duration_s": 0.1, "profile": "iec_radial_v1", "fault_kind": "3ph"},
            "input": {"selected_bus_ids": list(selected), "include_contributions": True,
                      "supplemental": supplemental, "supplemental_sha256": fingerprint(supplemental)}}
from power_machine.netedit import new_variant, create_command, VariantHistory
from power_machine.gui.netedit.forms import creation_template, line_from_pu
def empty_model():
    return {"schema": "power_machine-model-v1", "meta": {"s_base_mva": 100.0, "f_nom_hz": 50.0},
            "buses": [], "branches": [], "branches3": [], "loads": [], "gens": [],
            "shunts": [], "switches": [], "controllers": [], "warnings": []}

def analytic_history():
    base = empty_model()
    variant = new_variant(base, variant_id="v_analytic")
    data = creation_template("buses")
    data.update(name="Barra 10 kV", un_kv=10.0)
    b1 = create_command(variant.variant_id, "buses", data)
    b2 = create_command(variant.variant_id, "buses", data)
    gen = creation_template("gens")
    gen.update(bus=b1.object_id, p_mw=0.0, q_mvar=0.0, v_set_pu=1.0, ref=True)
    g1 = create_command(variant.variant_id, "gens", gen)
    load = creation_template("loads")
    load.update(bus=b2.object_id, p_mw=50.0, q_mvar=0.0)
    l1 = create_command(variant.variant_id, "loads", load)
    line = creation_template("branches")
    line.update(bus_from=b1.object_id, bus_to=b2.object_id)
    line = line_from_pu(line, r_pu=0, x_pu=0.1, b_pu=0, g_pu=0, un_kv=10, s_base_mva=100)
    z1 = create_command(variant.variant_id, "branches", line)
    history = VariantHistory(base, variant)
    history.apply((b1, b2, g1, l1, z1))
    return base, history, {"b1": b1.object_id, "b2": b2.object_id, "g1": g1.object_id,
                           "l1": l1.object_id, "z1": z1.object_id}
from copy import deepcopy
from power_machine.netedit import execute_request, variant_to_dict, model_sha256, value_sha256
from power_machine.contingency import run_contingencies, ContingencyOptions
from power_machine.protection import CurveSpec, RelayStage, standard_curve
import math
class FakeContext:
    def __init__(self, model):
        self.root = ROOT
        self.model = deepcopy(model)
        self.jobs, self.published, self.logs, self.selected = [], [], [], []

    def get_snapshot(self):
        return {"model": deepcopy(self.model), "study": {}, "fingerprints": {}}

    def submit_job(self, module_id, request, on_message):
        self.jobs.append((module_id, deepcopy(request)))
        on_message(("result", execute_request(request)))
        return "fake-job"

    def publish_result(self, result):
        self.published.append(deepcopy(result))

    def log(self, level, message):
        self.logs.append((level, message))

    def select_element(self, object_id):
        self.selected.append(object_id)

@pytest.fixture
def parallel_model():
    """100 MVA, 10 kV, X por rama 0.1 p.u., carga 50 MW, rating 0.3 Ibase."""
    model = {"schema": "power_machine-model-v1", "meta": {"s_base_mva": 100.0, "f_nom_hz": 50.0},
             "buses": [{"id": key, "name": key, "class": "ElmTerm", "un_kv": 10.,
                        "path": f"fixture/{key}", "outserv": 0} for key in ("B1", "B2")],
             "branches": [{"id": key, "name": key, "path": f"fixture/{key}",
                           "class": "ElmLne", "bus_from": "B1", "bus_to": "B2", "outserv": 0,
                           "params": {"R1": 0., "X1": .1, "B1": 0., "G1": 0., "nlnum": 1},
                           "i_rated_ka": .3 * 100 / (math.sqrt(3) * 10)} for key in ("L1", "L2")],
             "gens": [{"id": "G1", "path": "fixture/G1", "name": "G1", "class": "ElmSym",
                       "bus": "B1", "ref": True, "mode": "constv", "v_set_pu": 1.,
                       "outserv": 0, "params": {"pgini": 0., "qgini": 0., "ngnum": 1}}],
             "loads": [{"id": "D2", "path": "fixture/D2", "name": "D2", "class": "ElmLod",
                        "bus": "B2", "outserv": 0, "params": {"plini": 50., "qlini": 0., "scale0": 1.}}],
             "branches3": [], "shunts": [], "switches": [], "warnings": []}
    return model

def lines_only(model, *, options=None, scenario=None, **kwargs):
    return run_contingencies(model, scenario or {}, options or ContingencyOptions(),
                             selected_ids=["L1", "L2"], filter_reason="Ensayo de ramas paralelas",
                             **kwargs)

def net_request(base, variant):
    study = {"schema": "power_machine-study-v1", "scenario": {}, "ldf_options": {"q_limits": False}}
    return {"schema": "power_machine-parity-request-v1", "module": "netedit", "root": str(ROOT),
            "snapshot": {"model": base, "study": study,
                         "fingerprints": {"model_sha256": model_sha256(base), "study_sha256": value_sha256(study)}},
            "options": {"run_loadflow": True}, "input": {"variant": variant_to_dict(variant)}}

def stage(family="iec", name="very_inverse", dial=0.1, *, object_id="P", pickup=100.0, **kwargs):
    curve = standard_curve(family, name) if family in ("iec", "ieee") else CurveSpec(family, name, {}, {})
    return RelayStage(object_id, "fixture/"+object_id, "R_"+object_id, None, None, pickup, dial,
                      curve, True, "phase", **kwargs)


def run_until(app, done, timeout=45):
    import time
    deadline=time.monotonic()+timeout
    import _tkinter
    def pump():
        # update() drains until quiescence and can starve this deadline on busy Tk.
        turn=time.monotonic()+.05
        for _ in range(200):
            if time.monotonic()>=turn or not app.tk.dooneevent(_tkinter.ALL_EVENTS|_tkinter.DONT_WAIT):
                break
    while not done() and time.monotonic()<deadline:
        pump()
        time.sleep(.01)
    pump()
    assert done()
