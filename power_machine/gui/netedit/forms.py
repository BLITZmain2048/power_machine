"""Formularios de datos explícitos, en unidades del motor LF existente."""
from power_machine.gui.i18n import tr

from copy import deepcopy
import json
import math
import tkinter as tk
from tkinter import ttk


def number(text, unit):
    """Un campo vacío sigue ausente. No sustituye el vacío por cero."""
    if text is None or str(text).strip() == "":
        return None
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(tr('integration.nonfinite_value_in') + unit)
    return value


def creation_template(group, cls=None):
    """Valores iniciales visibles, deben revisarse antes de crear."""
    data = {"name": tr('integration.new_element'), "outserv": 0, "params": {}, "units": {},
            "type_params": {}, "type_units": {}}
    if group == "buses":
        data.update({"class": "ElmTerm", "un_kv": None, "units": {"un_kv": "kV"}})
    elif group == "loads":
        data.update({"class": "ElmLod", "bus": None, "p_mw": None, "q_mvar": None,
                     "scale": 1.0, "units": {"p_mw": "MW", "q_mvar": "Mvar", "scale": "1"}})
    elif group == "gens":
        data.update({"class": cls or "ElmGenstat", "bus": None, "p_mw": None, "q_mvar": None,
                     "mode": "constv", "ref": False, "v_set_pu": None,
                     "q_min_mvar": None, "q_max_mvar": None,
                     "params": {"ngnum": 1, "scale0": 1.0},
                     "units": {"p_mw": "MW", "q_mvar": "Mvar", "v_set_pu": "p.u."}})
    elif group == "branches" and cls == "ElmTr2":
        data.update({"class": "ElmTr2", "bus_from": None, "bus_to": None,
                     "params": {"nntap": 0, "ntnum": 1},
                     "type_params": {"strn": None, "utrn_h": None, "utrn_l": None,
                                     "uktr": None, "pcutr": None, "curmg": 0, "pfe": 0,
                                     "nt2ag": 0, "dutap": 0, "phitr": 0, "nntap0": 0,
                                     "tap_side": 0, "itrdr": 0.5, "itrdl": 0.5},
                     "type_units": {"strn": "MVA", "utrn_h": "kV", "utrn_l": "kV",
                                    "uktr": "%", "pcutr": "kW", "curmg": "%", "pfe": "kW",
                                    "nt2ag": "1", "dutap": "%", "phitr": "deg", "nntap0": "1",
                                    "tap_side": "1", "itrdr": "1", "itrdl": "1"}})
    elif group == "branches" and cls == "ElmZpu":
        params = {k: 0 for k in ("nphshift", "ag", "gi_pu", "bi_pu", "gj_pu", "bj_pu")}
        params.update({"Sn": None, "uratio": 1, "r_pu": None, "x_pu": None,
                       "r_pu_ji": None, "x_pu_ji": None})
        data.update({"class": cls, "bus_from": None, "bus_to": None, "params": params,
                     "units": {**{k: "p.u." for k in params}, "Sn": "MVA", "ag": "deg", "nphshift": "1", "uratio": "1"}})
    elif group == "branches":
        data.update({"class": "ElmLne", "bus_from": None, "bus_to": None,
                     "params": {"R1": None, "X1": None, "G1": 0, "B1": 0, "nlnum": 1},
                     "units": {"R1": "Ohm", "X1": "Ohm", "G1": "uS", "B1": "uS", "nlnum": "1"}})
    elif group == "shunts":
        data.update({"class": "ElmShnt", "bus": None,
                     "params": {"shtype": 2, "qcapn": None, "qrean": 0, "ushnm": None, "ncapa": 1, "ncapx": 1},
                     "units": {"shtype": "1", "qcapn": "Mvar", "qrean": "Mvar", "ushnm": "kV", "ncapa": "1", "ncapx": "1"}})
    elif group == "switches":
        data.update({"class": cls or "ElmCoup", "bus1": None, "bus2": None,
                     "closed": True, "element_id": None, "units": {"closed": "1"}})
        if cls == "StaSwitch":
            data["port"] = None
    else:
        raise ValueError(tr('integration.creation_outside_the_supported_domain'))
    return data


def line_from_pu(data, *, r_pu, x_pu, b_pu, g_pu, un_kv, s_base_mva):
    """R/X p.u. a Ohm, G/B p.u. a uS con base explícita U_LL²/S_3F."""
    values = (r_pu, x_pu, b_pu, g_pu, un_kv, s_base_mva)
    if any(v is None or not math.isfinite(v) for v in values) or un_kv <= 0 or s_base_mva <= 0:
        raise ValueError(tr('integration.conversion_requires_explicit_values_and_bases'))
    result = deepcopy(data)
    z_base_ohm = un_kv ** 2 / s_base_mva
    result.setdefault("params", {}).update(R1=r_pu * z_base_ohm, X1=x_pu * z_base_ohm,
                                          G1=g_pu / z_base_ohm * 1e6, B1=b_pu / z_base_ohm * 1e6)
    result.setdefault("units", {}).update(R1="Ohm", X1="Ohm", G1="uS", B1="uS")
    result.setdefault("provenance", {})["conversions"] = [
        {"input_unit": "p.u.", "un_kv": un_kv, "s_base_mva": s_base_mva,
         "z_base_ohm": z_base_ohm, "r_pu": r_pu, "x_pu": x_pu, "g_pu": g_pu, "b_pu": b_pu,
         "formula": "Z_ohm=Z_pu*U_LL_kV^2/S_3F_MVA, Y_uS=Y_pu/Z_base_ohm*1e6"}]
    return result


class ElementForm(tk.Toplevel):
    """Formulario con unidades explícitas y desplazamiento."""

    def __init__(self, parent, data, on_accept, *, title=tr('integration.element_data')):
        super().__init__(parent)
        self.title(title)
        self.geometry('700x520')
        self.variables = {}
        self.original = deepcopy(data)
        self.on_accept = on_accept
        self.message = tk.StringVar(self)
        self.numeric = set()
        self.booleans = set()
        self.paths = {}
        from power_machine.gui.panels.panel_widgets import ScrollForm
        scroll_form = ScrollForm(self)
        scroll_form.pack(fill='both', expand=True)
        body = scroll_form.content
        ttk.Label(body, text=tr('integration.an_empty_field_preserves_missing_data_drafts'), wraplength=630).grid(row=0, columnspan=2, sticky='w')
        fields = []
        for key, value in data.items():
            if key in ('units', 'type_units'):
                continue
            if isinstance(value, dict):
                if key not in ('params', 'type_params'):
                    continue
                for nested, val in value.items():
                    fields.append((key+'.'+nested, val, data.get('type_units' if key == 'type_params' else 'units', {}).get(nested, '')))
            else:
                fields.append((key, value, data.get('units', {}).get(key, {'un_kv':'kV', 'p_mw':'MW', 'q_mvar':'Mvar', 'v_set_pu':'p.u.', 'q_min_mvar':'Mvar', 'q_max_mvar':'Mvar', 'scale':'1', 'outserv':'0/1'}.get(key, ''))))
        for row, (key, value, unit) in enumerate(fields, 1):
            label = key + (' ['+unit+']' if unit else '')
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=3)
            var = tk.StringVar(self, '' if value is None else str(value))
            self.variables[key] = var
            if isinstance(value, bool):
                self.booleans.add(key)
                widget = ttk.Combobox(body, textvariable=var, values=['True', 'False'], state='readonly')
            else:
                if isinstance(value, (int, float)) or unit or key.startswith(('params.', 'type_params.')):
                    self.numeric.add(key)
                widget = ttk.Entry(body, textvariable=var)
            widget.grid(row=row, column=1, sticky='ew', padx=6)
        body.columnconfigure(1, weight=1)
        scroll_form.wire()
        footer = ttk.Frame(self, padding=8)
        footer.pack(fill='x')
        ttk.Label(footer, textvariable=self.message, wraplength=520).pack(side='left')
        ttk.Button(footer, text=tr('integration.apply'), command=self.accept).pack(side='right')
        self.transient(parent.winfo_toplevel())

    def accept(self):
        try:
            data = deepcopy(self.original)
            for key, var in self.variables.items():
                text = var.get().strip()
                if key in self.booleans:
                    if text not in ('True', 'False'):
                        raise ValueError(tr('integration.select_true_or_false'))
                    value = text == 'True'
                elif key in self.numeric:
                    value = number(text, key)
                else:
                    value = text or None
                if '.' in key:
                    outer, inner = key.split('.', 1)
                    data[outer][inner] = value
                else:
                    data[key] = value
            if data.get('outserv') not in (0, 1):
                raise ValueError(tr('integration.out_of_service_accepts_0_or_1'))
            if data.get('un_kv') is not None and data['un_kv'] <= 0:
                raise ValueError(tr('integration.nominal_voltage_must_be_positive_in_kv'))
            json.dumps(data, allow_nan=False)
            self.on_accept(data)
            self.destroy()
        except (ValueError, TypeError) as exc:
            self.message.set(str(exc))
