"""Casos portátiles de estudio y fachada del motor power_machine, sin dependencias GUI.

Generation is positive injection and load is positive consumption.
Times are in seconds, voltages in p.u. and event impedances in ohms.
"""
from __future__ import annotations

import cmath
import copy
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
from pathlib import Path
import re

from .loadflow import _scenario, run_loadflow as _run_loadflow
from .overrides import apply_overrides, read_overrides
from .dynamics.events import Event
from .dynamics.runner import CASES, NEW_CASES, RMSRunner, load_case
from .dynamics.switching import resolve_element_switches
from .topology import build_topology
from .resources import dynamic_catalog_root, reference_tables_root, writable_root

STUDY_SCHEMA = "power_machine-study-v1"
REFERENCE_CASES = CASES
EVENT_KINDS = ("short_circuit", "clear_short_circuit", "switch_element",
               "switch", "load_step", "gen_trip")
_GROUPS = ("buses", "branches", "branches3", "switches", "gens", "loads", "shunts", "controllers")
_TRIP_DOMAIN = 'Desconexión de generador fuera del dominio validado contra la referencia externa.'


@dataclass
class SimEvent:
    time_s: float
    kind: str
    target_id: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return copy.deepcopy(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> SimEvent:
        return cls(float(d["time_s"]), str(d["kind"]), str(d["target_id"]),
                   copy.deepcopy(d.get("params", {})))


@dataclass
class SimOptions:
    t_start_s: float = -0.1
    t_end_s: float = 10.0
    dt_s: float = 0.001
    output_every: int = 1


@dataclass
class StudyCase:
    name: str = "Caso nuevo"
    scenario: dict = field(default_factory=dict)
    events: list[SimEvent] = field(default_factory=list)
    options: SimOptions = field(default_factory=SimOptions)
    monitored: list[str] = field(default_factory=list)
    ldf_options: dict = field(default_factory=lambda: {"q_limits": True})
    reference_case: str | None = None
    plots: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"schema": STUDY_SCHEMA, **copy.deepcopy(asdict(self))}

    @classmethod
    def from_json(cls, d: dict) -> StudyCase:
        if d.get("schema") != STUDY_SCHEMA:
            raise ValueError("Esquema de estudio no compatible")
        return cls(name=str(d["name"]), scenario=copy.deepcopy(d.get("scenario", {})),
                   events=[SimEvent.from_dict(e) for e in d.get("events", [])],
                   options=SimOptions(**d.get("options", {})),
                   monitored=list(d.get("monitored", [])),
                   ldf_options=copy.deepcopy(d.get("ldf_options", {"q_limits": True})),
                   reference_case=d.get("reference_case"), plots=copy.deepcopy(d.get("plots", [])))

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2,
                                        allow_nan=False)+"\n", encoding="utf-8")

    @classmethod
    def load(cls, path) -> StudyCase:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def load_network(path, overrides=None, dynamic_dir=None) -> dict:
    """Carga la red y descubre dynamic/manifest.json o el catálogo explícito."""
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / "model.json"
    model = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(model, dict) or not isinstance(model.get("buses"), list):
        raise ValueError("Modelo de red no compatible")
    if overrides is not None:
        model, _ = apply_overrides(model, read_overrides(overrides))
    model.pop("_dynamic_dir", None)
    candidate = Path(dynamic_dir).expanduser() if dynamic_dir is not None else path.parent / "dynamic"
    if not candidate.is_absolute():
        candidate = path.parent / candidate
    if candidate.is_dir() and (candidate / "manifest.json").is_file():
        model["_dynamic_dir"] = str(candidate.resolve())
    return model


def load_model(root, model_path=None) -> dict:
    """Support network directories with model.json and optional overrides."""
    root = Path(root)
    if root.is_file() and model_path is None:
        return load_network(root)
    if model_path is None and not (root / "model.json").is_file() and (root / "data" / "model.json").is_file():
        root = root / "data"
    path = Path(model_path) if model_path is not None else root / "model.json"
    if model_path is not None and not path.is_absolute():
        path = root / path
    overrides = root / "overrides.json"
    return load_network(path, overrides if model_path is None and overrides.is_file() else None)


def element_index(model) -> dict[str, dict]:
    return {item["id"]: {**copy.deepcopy(item), "group": group}
            for group in _GROUPS for item in model.get(group, [])}


def substation_of(item) -> str:
    parts = str(item.get("path", item.get("relative_path", ""))).replace("/", "\\").split("\\")
    for suffix in (".ElmSubstat", ".ElmNet"):
        groups = [p[:-len(suffix)] for p in parts if p.endswith(suffix)]
        if groups:
            return groups[-1]
    return "Sin subestación"


def new_study(root, name="Caso nuevo") -> StudyCase:
    return StudyCase(name=name, monitored=default_monitored(load_model(root)))


def reference_study(root, case: str) -> StudyCase:
    root = Path(root)
    model = load_model(root)
    raw, report = load_case(root, case)
    by_path = {item["path"]: item for item in element_index(model).values()}
    events = []
    for row in raw:
        attrs, target = row["attributes"], row["target"]
        if row["class"] == "EvtShc":
            target = target.split("\\Cub_", 1)[0]
            code = int(attrs["i_shc"])
            if code not in (0, 4):
                raise ValueError(f"Falla REF no soportada: {code}")
            kind = "clear_short_circuit" if code == 4 else "short_circuit"
            params = {} if code == 4 else {"r_ohm": float(attrs["R_f"]), "x_ohm": float(attrs["X_f"])}
        elif row["class"] == "EvtLod":
            kind, params = "load_step", {"dp_pct": float(attrs["dP"]), "dq_pct": float(attrs["dQ"])}
        elif row["class"] == "EvtSwitch":
            cls = by_path[target]['class']
            if cls in ('ElmSym', 'ElmGenstat', 'ElmPvsys'):
                if int(attrs['i_switch']) != 0:
                    raise ValueError('Reconexión de generador RMS no soportada')
                kind, params = 'gen_trip', {}
            else:
                kind = "switch" if cls in ("StaSwitch", "ElmCoup") else "switch_element"
                params = {"action": "open" if int(attrs["i_switch"]) == 0 else "close"}
        else:
            raise ValueError(f"Evento REF desconocido: {row['class']}")
        events.append(SimEvent(float(row["time_s"]), kind, by_path[target]["id"], params))
    with (reference_tables_root(root) / "rms" / case / "signals.csv").open(encoding="utf-8-sig", newline="") as f:
        monitored = next(csv.reader(f))
    supported = {row['key'] for row in signal_catalog(root, model)}
    return StudyCase(name=case, events=events, reference_case=case,
                     options=SimOptions(t_start_s=float(report["rms_options"]["tstart"]),
                                        t_end_s=float(report["tstop_s"])),
                     monitored=[key for key in monitored if key in supported])


def signal_catalog(root, model=None) -> list[dict]:
    model = load_model(root) if model is None else model
    result = []
    for item in element_index(model).values():
        cls = item.get("class")
        variables = []
        if cls == "ElmTerm":
            variables = [("m:u", "p.u.", "Tensión"), ("m:phiu", "deg", "Ángulo de tensión"),
                         ("m:fehz", "Hz", "Frecuencia")]
        elif cls in ("ElmLne", "ElmTr2", "ElmTr3", "ElmZpu"):
            ports = ("bushv", "busmv", "buslv") if cls == "ElmTr3" else ("bus1", "bus2")
            variables = [(f"m:{var}:{port}", unit, f"Potencia saliente {var}, {port}")
                         for port in ports for var, unit in (("P", "MW"), ("Q", "Mvar"))]
        elif cls in ("ElmSym", "ElmGenstat", "ElmPvsys", "ElmLod"):
            direction = "consumida" if cls == "ElmLod" else "inyectada"
            variables = [("m:P:bus1", "MW", f"Potencia activa {direction}"),
                         ("m:Q:bus1", "Mvar", f"Potencia reactiva {direction}")]
            if cls == "ElmSym":
                variables += [("s:xspeed", "p.u.", "Velocidad del rotor"),
                              ("s:phi", "deg", "Ángulo del rotor en marco nominal"),
                              ("s:ve", "p.u.", "Excitación en base máquina"),
                              ("s:pt", "p.u.", "Potencia mecánica en base máquina")]
        for variable, unit, description in variables:
            result.append({"key": f"{item['path']}|{variable}|{unit}", "element_id": item["id"],
                           "element_name": item["name"], "element_class": cls,
                           "variable": variable, "unit": unit, "description": description})
    return result


def default_monitored(model) -> list[str]:
    keys = [f"{b['path']}|{var}|{unit}" for b in model.get("buses", [])
            if not b.get("outserv")
            for var, unit in (("m:u", "p.u."), ("m:fehz", "Hz"))]
    keys += [f"{g['path']}|s:xspeed|p.u." for g in model.get("gens", [])
             if g.get("class") == "ElmSym" and not g.get("outserv")]
    return keys


def _translate_event(event, index, s_base_mva):
    """Translate without rounding percentages or local R+jX impedance."""
    item = index.get(event.target_id)
    if event.kind not in EVENT_KINDS:
        raise ValueError(f"Tipo de evento no soportado: {event.kind}")
    if item is None:
        raise ValueError(f"Objetivo desconocido: {event.target_id}")
    cls, params = item["class"], event.params
    if not isinstance(params, dict):
        raise ValueError('Los parámetros del evento deben ser un diccionario')
    if event.kind == 'gen_trip':
        if cls not in ('ElmSym', 'ElmGenstat', 'ElmPvsys'):
            raise ValueError('gen_trip requiere un generador')
        if item.get('ref') or int(item.get('params', {}).get('ip_ctrl', 0)) == 1:
            raise ValueError('No se puede desconectar la máquina de referencia angular')
        if params:
            raise ValueError('gen_trip no admite parámetros ni reconexión')
        return Event(event.time_s, 'gen_trip', event.target_id)
    if event.kind in ("short_circuit", "clear_short_circuit"):
        if cls != "ElmTerm":
            raise ValueError("La falla y su despeje requieren una barra")
        value = None
        if event.kind == "short_circuit":
            r, x = float(params["r_ohm"]), float(params["x_ohm"])
            if not all(math.isfinite(v) for v in (r, x)) or r < 0:
                raise ValueError("Rf+jXf must be finite, with nonnegative Rf")
            value = complex(r, x)/(float(item["un_kv"])**2/float(s_base_mva))
        return Event(event.time_s, "fault_apply" if value is not None else "fault_clear", event.target_id, value)
    if event.kind in ("switch", "switch_element"):
        classes = ("StaSwitch", "ElmCoup") if event.kind == "switch" else ("ElmLne", "ElmTr2", "ElmTr3", "ElmZpu")
        if cls not in classes or params.get("action") not in ("open", "close"):
            raise ValueError("Objetivo o acción de maniobra inválidos")
        prefix = "switch_" if event.kind == "switch" else "element_switch_"
        return Event(event.time_s, prefix+params["action"], event.target_id)
    if cls != "ElmLod":
        raise ValueError("El escalón requiere una carga ElmLod")
    dp, dq = float(params["dp_pct"]), float(params["dq_pct"])
    if not all(math.isfinite(v) for v in (dp, dq)):
        raise ValueError("Los porcentajes de carga deben ser finitos")
    return Event(event.time_s, "load_step", event.target_id, (dp, dq))


def validate_study(root, study, model=None) -> list[dict]:
    messages = []
    def add(level, message):
        messages.append({"level": level, "message": str(message)})
    model = load_model(root) if model is None else model
    try:
        opts = study.options
        if not all(math.isfinite(v) for v in (opts.t_start_s, opts.t_end_s, opts.dt_s)):
            raise ValueError("Los tiempos y el paso deben ser finitos")
        if opts.t_end_s <= opts.t_start_s or opts.dt_s <= 0:
            raise ValueError("Intervalo o paso de simulación inválido")
        if type(opts.output_every) is not int or opts.output_every < 1:
            raise ValueError("El submuestreo debe ser un entero positivo")
        if not isinstance(study.scenario, dict) or not isinstance(study.ldf_options, dict):
            raise ValueError('Escenario y opciones LF deben ser diccionarios')
        for name, changes in study.scenario.items():
            if not isinstance(changes, dict):
                raise ValueError(f'{name} requiere un mapa por ID de elemento')
            for value in changes.values():
                if name in ('closed', 'outserv'):
                    if value not in (0, 1, False, True):
                        raise ValueError(f'{name} requiere 0 o 1')
                elif not math.isfinite(float(value)):
                    raise ValueError(f'{name} requiere un valor finito')
                elif name == 'load_scale' and float(value) < 0:
                    raise ValueError('La escala de carga debe ser no negativa')
                elif name == 'v_set_pu' and float(value) <= 0:
                    raise ValueError('La tensión de consigna debe ser positiva')
        tolerance = study.ldf_options.get('tolerance_pu', 1e-8)
        iterations = study.ldf_options.get('max_iter', 30)
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError('Tolerancia LF inválida')
        if type(iterations) is not int or iterations < 1:
            raise ValueError('El máximo de iteraciones LF debe ser un entero positivo')
        operational = _scenario(model, study.scenario)
        topology = build_topology(operational)
    except (ValueError, TypeError, KeyError) as exc:
        add("error", exc)
        return messages
    index = element_index(operational)
    if study.scenario:
        add("aviso", "El escenario difiere del caso base validado. Se inicializará desde el LF del modelo.")
    if opts.dt_s != 0.001:
        add("aviso", "Paso distinto de 1 ms, fuera del dominio de validación temporal.")
    if study.reference_case is not None and study.reference_case not in (*REFERENCE_CASES, *NEW_CASES):
        add("error", "Caso de referencia desconocido")
    known = {r['key'] for r in signal_catalog(root, model)} | {'time|b:tnow|s'}
    for key in study.monitored:
        if key not in known:
            add("error", f"Señal no soportada: {key}")
    faults = set()
    reference_faults = set()
    switches = {s['id']: s for s in operational['switches']}
    shared_generators = set()
    if any(e.kind == 'gen_trip' for e in study.events):
        from .dynamics.converters import converter_bindings
        from .dynamics.schema import read_dynamic_export
        shared_generators = set(converter_bindings(
            read_dynamic_export(dynamic_catalog_root(root)))['controlled_genstat'])
    load_factors = {}
    for event in sorted(study.events, key=lambda e: e.time_s):
        try:
            active_gens = set(topology['active']['gens'])
            powered = {bid for island in topology['islands']
                       if set(island['elements']['gens']) & active_gens for bid in island['buses']}
            if not math.isfinite(event.time_s) or not opts.t_start_s <= event.time_s <= opts.t_end_s:
                raise ValueError("Evento fuera del intervalo de simulación")
            translated = _translate_event(event, index, model['meta']['s_base_mva'])
            item = index[event.target_id]
            buses = ([item['id']] if item['class'] == 'ElmTerm' else
                     [item[k] for k in ('bus', 'bus1', 'bus2', 'bus_from', 'bus_to', 'bus_hv', 'bus_mv', 'bus_lv') if item.get(k)])
            if item.get('outserv') or not any(b in powered for b in buses):
                raise ValueError("Objetivo fuera de servicio o sin barra energizada")
            switch_ids = []
            if event.kind == 'switch_element':
                switch_ids = resolve_element_switches(operational, event.target_id)
            elif event.kind == 'switch':
                switch_ids = [event.target_id]
            elif event.kind == 'load_step':
                if event.target_id not in topology['active']['loads']:
                    raise ValueError('La carga está desconectada')
                p, q = load_factors.get(event.target_id, (100.0, 100.0))
                dp, dq = translated.value
                if p+dp < 0 or q+dq < 0:
                    raise ValueError('Los escalones acumulados no pueden producir consumo negativo')
                load_factors[event.target_id] = (p+dp, q+dq)
            elif event.kind == 'gen_trip':
                if event.target_id in shared_generators:
                    raise ValueError('Desconexión de unidades que comparten dispositivo y PPC no soportada')
                if event.target_id not in active_gens:
                    raise ValueError('El generador ya está desconectado')
                add('aviso', _TRIP_DOMAIN)
                next(g for g in operational['gens'] if g['id'] == event.target_id)['outserv'] = 1
                topology = build_topology(operational)
            if translated.kind == 'fault_apply':
                if event.target_id in faults:
                    raise ValueError("La barra ya tiene una falla activa")
                faults.add(event.target_id)
                if (event.target_id, float(event.params['r_ohm']), float(event.params['x_ohm'])) not in reference_faults:
                    add("aviso", "Ubicación o Rf+jXf de falla distinta de caso de referencia suministrado, fuera del caso validado.")
            elif translated.kind == 'fault_clear':
                if event.target_id not in faults:
                    raise ValueError("Despeje sin falla previa en la barra")
                faults.remove(event.target_id)
            if event.kind == 'switch' and item.get('element_id') in {g['id'] for g in model['gens']}:
                raise ValueError('Maniobra de cubículo de generador no soportada, use gen_trip para desconectar')
            for switch_id in switch_ids:
                switches[switch_id]['closed'] = event.params['action'] == 'close'
            if switch_ids:
                topology = build_topology(operational)
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            add("error", f"Evento {event.time_s} s, {event.target_id}: {exc}")
    if any(e.kind == 'short_circuit' for e in study.events) and any('|m:fehz|' in k for k in study.monitored):
        add("aviso", "Frecuencia en los primeros 10 ms tras una falla no apta para tasa de cambio de frecuencia ni protecciones rápidas. ")
    return messages


def run_loadflow(root, study, model=None) -> dict:
    model = load_model(root) if model is None else model
    result = _run_loadflow(model, study.scenario, {"q_limits": True, **study.ldf_options})
    index = element_index(model)
    for bus in result['buses']:
        bus['un_kv'] = float(index[bus['id']]['un_kv'])
        bus['energized'] = bus['status'] == 'resuelto' and bool(bus['u_pu'])
    present = {b['id'] for b in result['buses']}
    for bus in model['buses']:
        if bus['id'] not in present:
            result['buses'].append({"id": bus['id'], "name": bus['name'], "un_kv": float(bus['un_kv']),
                                    "u_pu": None, "u_kv": None, "angle_deg": None,
                                    "energized": False, "status": "fuera_de_servicio"})
    for gen in result['gens']:
        gen['at_q_limit'] = bool(gen.get('q_limit'))
    for branch in result['branches']:
        if branch['class'] == 'ElmTr3':
            for name, source in (('bus_from', 'bus_hv'), ('bus_to', 'bus_mv'),
                                 ('p_from_mw', 'p_hv_mw'), ('q_from_mvar', 'q_hv_mvar'),
                                 ('p_to_mw', 'p_mv_mw'), ('q_to_mvar', 'q_mv_mvar'), ('i_from_ka', 'i_hv_ka')):
                branch[name] = branch.get(source)
    result['max_mismatch_pu'] = float(result['max_mismatch_mva'])/float(model['meta']['s_base_mva'])
    return result


def reference_signals(root, case) -> tuple[list[str], list[list[float]]]:
    if case not in (*REFERENCE_CASES, *NEW_CASES):
        raise ValueError(f"Caso desconocido: {case}")
    with (reference_tables_root(root) / "rms"/case/'signals.csv').open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        return next(reader), [[float(v) for v in row] for row in reader]


class Simulation:
    def __init__(self, root, study: StudyCase, *, log=None, model=None):
        self.root = Path(root)
        self.study = StudyCase.from_json(study.to_json())
        self.log = log or (lambda level, text: None)
        self._runner = None
        self._used = False
        self._model = copy.deepcopy(model)

    def initial_conditions(self) -> dict:
        if self._runner is None:
            model = load_model(self.root) if self._model is None else copy.deepcopy(self._model)
            self._messages = validate_study(self.root, self.study, model)
            for message in self._messages:
                self.log(message['level'].upper(), message['message'])
            errors = [m['message'] for m in self._messages if m['level'] == 'error']
            if errors:
                raise ValueError('\n'.join(errors))
            point = None
            if self.study.scenario or self._model is not None:
                lf = run_loadflow(self.root, self.study, model)
                if any(i['status'] == 'no_convergente' for i in lf['islands']):
                    raise ValueError('El LF del escenario no converge')
                point = {'bus_voltages': {b['id']: b['u_pu']*cmath.exp(1j*math.radians(b['angle_deg']))
                                          for b in lf['buses'] if b['energized']},
                         'generator_powers': {g['id']: complex(g['p_mw'], g['q_mvar'])
                                              for g in lf['gens'] if g['p_mw'] is not None}}
                model = _scenario(model, self.study.scenario)
            index = element_index(model)
            events = [_translate_event(e, index, model['meta']['s_base_mva']) for e in self.study.events]
            self._runner = RMSRunner(self.root, events=events, monitored=self.study.monitored,
                                     tstart=self.study.options.t_start_s, dt_s=self.study.options.dt_s,
                                     model=model, operating_point=point)
        runner = self._runner
        warnings = [m['message'] for m in self._messages if m['level'] == 'aviso'] + list(runner.override_warnings)
        return {'n_states': int(runner.state.x.size), 'n_nodes': len(runner.network.node_ids),
                'network_residual_pu': float(max(abs(runner.initial_network_residual), default=0.0)),
                'max_derivative': float(max(abs(runner.initial_derivative), default=0.0)),
                'dsl_coupled': sum(r['status'] == 'coupled' for r in runner.dsl_coverage),
                'dsl_total': len(runner.dsl_coverage), 'frozen': copy.deepcopy(runner.coverage),
                'validated_operating_point': not bool(self.study.scenario) and self._model is None,
                'warnings': warnings}

    def run(self, *, on_header=None, on_samples=None, on_progress=None,
            cancel=None, output_dir=None, batch=50) -> dict:
        if self._used:
            raise RuntimeError('Cree otra Simulation para repetir el estudio')
        self.initial_conditions()
        self._used = True
        name = re.sub(r'[^\w .-]', '_', self.study.name).strip(' .') or 'caso'
        out = Path(output_dir) if output_dir is not None else writable_root() / (
            name+'_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        out.mkdir(parents=True, exist_ok=True)
        self.study.save(out/'study.power_machine')
        report = self._runner.run(end_s=self.study.options.t_end_s, output=out,
                                  output_every=self.study.options.output_every,
                                  on_header=on_header, on_samples=on_samples, on_progress=on_progress,
                                  cancel=cancel, batch=batch)
        summary = {**report, 'signals_csv': str(out/'signals.csv'), 'summary_json': str(out/'summary.json')}
        (out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        self.log('ERROR' if summary['failure'] else 'INFO',
                 summary['failure'] or ('Simulación cancelada' if summary['cancelled'] else 'Simulación terminada'))
        return summary
