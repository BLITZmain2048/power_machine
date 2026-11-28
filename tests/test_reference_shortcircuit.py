"""Cotejo de referencia offline rápido y puertas de cobertura, sin ejecutar un simulador comercial de referencia."""
from copy import deepcopy
from dataclasses import replace
import json
import math

import pytest

from power_machine.refcompare import cc
from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions, run_shortcircuit
from power_machine.shortcircuit.iec60909 import peak_factor
from power_machine.shortcircuit.sequences import prepare_network
from test_shortcircuit_analytical import fixture, datum


@pytest.fixture(scope="module")
def comparison(tmp_path_factory):
    if not (cc.DEFAULT_REF_DIR / "E39-CC-01/fault_summary.csv").exists():
        pytest.skip("Carpeta de referencia externa no disponible")
    return cc.run_comparisons(output_dir=tmp_path_factory.mktemp("ref_cc"))


def test_c1_analytical_single_source():
    model, data = fixture(True)
    options = ShortCircuitOptions("iec60909", "maximum", frequency_hz=60,
                                  min_break_time_s=0.1, profile="iec_ref2016_c1_v1")
    r = run_shortcircuit(model, FaultSpec("B2", "3ph"), options, data)
    assert r.quantities["Ik_initial_sym_ka"] == pytest.approx(28.4018778722, rel=1e-9)
    assert r.quantities["kappa"] == pytest.approx(1.23866755695, rel=1e-9)
    assert r.quantities["ip_peak_ka"] == pytest.approx(49.7527185605, rel=1e-9)
    assert r.quantities["Ib_sym_ka"] == pytest.approx(r.quantities["Ik_initial_sym_ka"], rel=1e-12)
    assert r.quantities["equivalent_frequency_hz"] == 24
    assert r.quantities["Ith_ka"] is None
    for kind in ("lg", "ll", "llg"):
        bad = run_shortcircuit(model, FaultSpec("B2", kind), options, data)
        assert bad.status == "unsupported" and bad.quantities["Ik_initial_sym_ka"] is None


def test_c1_parallel_sources_use_virtual_network():
    model, data = fixture()
    model["gens"].append({"id": "S2", "class": "ElmXnet", "bus": "B1", "path": "fixture/S2"})
    z1, z2 = 0.1+0.2j, 0.02+0.3j
    data.sources["S2"] = {"kind": "external", "parameters": {"z1": datum(z2)}}
    options = ShortCircuitOptions("iec60909", "maximum", frequency_hz=60, min_break_time_s=0.1,
                                  profile="iec_ref2016_c1_v1")
    result = run_shortcircuit(model, FaultSpec("B1", "3ph"), options, data)
    ze = 1/(1/complex(z1.real, 0.4*z1.imag)+1/complex(z2.real, 0.4*z2.imag))
    expected_rx = 0.4*ze.real/ze.imag
    assert result.quantities["RtoX"] == pytest.approx(expected_rx, rel=1e-12)
    assert result.quantities["kappa"] == pytest.approx(peak_factor(expected_rx, 1), rel=1e-12)
    z_nominal = z1*z2/(z1+z2)
    assert abs(expected_rx-z_nominal.real/z_nominal.imag) > 0.001
    assert result.quantities["Ik_initial_sym_ka"] == pytest.approx(11/math.sqrt(3)/abs(z_nominal), rel=1e-12)
    assert cc.kcl_check(result, "B1")["pass"]


def test_39_fault_initial_and_peak_parity(comparison):
    assert len(comparison["buses"]) == 39
    assert all(r["pass_Ikss_kA"] and r["pass_angle"] and r["pass_ip_kA"] for r in comparison["buses"])
    assert all(r["relative_error_RtoX"] < 1e-6 for r in comparison["buses"])
    assert all(r["relative_error_kappa_effective_derived"] < 1e-6 for r in comparison["buses"])


def test_every_incident_and_kcl(comparison):
    assert len(comparison["contributions"]) == 102
    assert {r["fault_bus"] for r in comparison["contributions"]} == set(range(1, 40))
    assert all(r["pass_phasor"] and r["pass_Ikss_kA"] and r["pass_ip_kA"] for r in comparison["contributions"])
    assert len(comparison["kcl"]) == 39 and all(r["pass"] for r in comparison["kcl"])
    assert max(r["residual_relative"] for r in comparison["kcl"]) < 1e-10
    assert all(r["nodal_bus_count"] == 39 and r["max_nodal_relative"] < 1e-10 for r in comparison["kcl"])
    # La identidad interna no tolera omitir una máquina incidente.
    r = comparison["runs"][31]
    damaged = replace(r, contributions=tuple(c for c in r.contributions if c["kind"] != "source"))
    assert not cc.kcl_check(damaged, r.fault["bus_id"])["pass"]
    for c in r.contributions:
        assert c["relative_path"].startswith("Network Model.")


def test_ib_parity_and_missing_off_are_explicit(comparison):
    b20 = comparison["cc03"]
    assert b20["pass_Ikss_kA"] and b20["pass_ip_kA"]
    assert b20["pass_Ib_kA"] and b20["pass"]
    assert b20["ref_correction_off"] is None and b20["ratio_on_off"] is None
    assert all(r["pass_Ib_kA"] for r in comparison["buses"])
    assert all(r["pass_Ib_kA"] for r in comparison["contributions"])
    metric = next(r for r in comparison["summary"] if r["metric"] == "Ib_kA_relative")
    assert metric["passed"] == metric["total"] == 39
    assert metric["maximum"] <= cc.TOLERANCES["Ib_relative"]


def test_station_factors_and_internal_fault(comparison):
    model, data = comparison["model"], comparison["data"]
    before = deepcopy((model, data))
    options = ShortCircuitOptions("iec60909", "maximum", profile="iec_ref2016_c1_v1")
    b20 = next(b["id"] for b in model["buses"] if b["name"] == "Bus 20")
    net = prepare_network(model, data, options, "3ph", b20)
    ref_entry = cc.read_json(cc.DEFAULT_REF_DIR / "E39-CC-03/correction_audit.json")
    factors = {r["name"]: r["factor"] for r in ref_entry["B20_transformer_factors"]}
    for b in net["branches"]:
        if b["item"]["class"] == "ElmTr2":
            assert b["factor"] == pytest.approx(factors[b["item"]["name"]], abs=1e-8)
    b31 = next(b["id"] for b in model["buses"] if b["name"] == "Bus 31")
    internal = prepare_network(model, data, options, "3ph", b31)
    branch = next(b for b in internal["branches"] if b["item"]["name"] == "Trf 06 - 31")
    assert branch["factor"] == 1
    assert (model, data) == before


def test_native_sc_data_and_base_invariance(comparison):
    model, data = deepcopy((comparison["model"], comparison["data"]))
    assert len(data.sources) == 10 and len(data.elements) == 46
    for record in data.sources.values():
        p = record["parameters"]
        assert p["x_d_subtransient_pu"]["value"] == pytest.approx(0.2, abs=1e-8)
        assert "xdsss" in p["x_d_subtransient_pu"]["source_attribute"]
        assert p["r_generator"]["value"] == pytest.approx(0.01*p["ur_kv"]["value"]**2/p["sn_mva"]["value"])
    options = ShortCircuitOptions("iec60909", "maximum", frequency_hz=60, min_break_time_s=0.1, profile="iec_ref2016_c1_v1")
    bus = comparison["runs"][20].fault["bus_id"]
    model["meta"]["s_base_mva"] = 250
    changed = run_shortcircuit(model, FaultSpec(bus, "3ph"), options, data)
    for field in ("Ik_initial_sym_ka", "ip_peak_ka", "Ib_sym_ka"):
        assert changed.quantities[field] == pytest.approx(comparison["runs"][20].quantities[field], rel=1e-12)


def test_nulls_signs_and_incomplete_coverage(comparison):
    ref_entry = cc.read_csv(cc.DEFAULT_REF_DIR / "E39-CC-01/fault_summary.csv")[0]
    bus = next(b for b in comparison["model"]["buses"] if b["name"] == "Bus 01")
    empty = dict(ref_entry, Ikss_kA="")
    assert not cc.compare_bus(empty, comparison["runs"][1], bus)["pass_Ikss_kA"]
    altered = dict(ref_entry, angle_deg=float(ref_entry["angle_deg"])+180)
    assert not cc.compare_bus(altered, comparison["runs"][1], bus)["pass_angle"]
    refs = list(cc.read_incident_reference(cc.DEFAULT_REF_DIR / "E39-CC-01/all_element_end_currents.csv"))
    with pytest.raises(ValueError, match="Cobertura incompleta"):
        cc.compare_contributions(comparison["model"], comparison["runs"], refs[:-1], comparison["buses"])
    refs[0]["into_fault_re_kA"] = -float(refs[0]["into_fault_re_kA"])
    refs[0]["into_fault_im_kA"] = -float(refs[0]["into_fault_im_kA"])
    rows = cc.compare_contributions(comparison["model"], comparison["runs"], refs, comparison["buses"])
    assert not rows[0]["pass_phasor"]


def test_export_confinement_and_completeness(comparison, tmp_path):
    with pytest.raises(ValueError, match="working directory"):
        cc.write_table(cc.WORKSPACE.parent / "ref_cc_forbidden.csv", [{"value": 1}])
    cc.write_table(tmp_path / "check.csv", comparison["buses"], output_dir=tmp_path)
    assert len(cc.read_csv(tmp_path / "check.csv")) == 39
    assert cc.number("") is None


import pytest
pytestmark = [pytest.mark.refdata]
