"""Pruebas propias del informe de estudio study report."""

from __future__ import annotations
from gui_support import ieee_model, ieee_root

import csv
import json
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def spanish_report_contract():
    """Estas comprobaciones editoriales ejercitan la versión española del informe."""
    from power_machine.gui.i18n import language, set_language
    previous = language()
    set_language('es')
    yield
    set_language(previous)

from power_machine.gui.study_report import (generate_study_report, latex_escape,
                                    verify_tex_table_values, verify_tex_utf8,
                                    verify_tex_envelope_values, _summary_rows)
from power_machine.loadflow import run_loadflow


def _assert_body_tables_short_and_informative(tex: str) -> None:
    for table in tex.split(r"\appendix", 1)[0].split(r"\begin{longtable}")[1:]:
        body = table.split(r"\bottomrule", 1)[0]
        rows = [line for line in body.splitlines() if " & " in line and line.rstrip().endswith(r"\\")]
        assert len(rows) <= 25
        columns = list(zip(*[line[:-2].split(" & ") for line in rows])) if rows else []
        assert not any(column and all(cell.strip() == "ausente" for cell in column) for column in columns)


def _loadflow_result():
    return {
        "converged": False,
        "islands": [
            {"status": "resuelto", "max_mismatch_mva": 1.0e-10},
            {"status": "sin_referencia", "elements": {"loads": ["LD_1"]}},
        ],
        "buses": [{"id": "B_1", "u_pu": 1.012345678901, "u_kv": 220.0, "angle_deg": -3.25}],
        "branches": [{"id": "L#1", "p_from_mw": 10.25, "q_from_mvar": -2.5,
                      "p_to_mw": -10.1, "q_to_mvar": 2.75}],
        "gens": [{"id": "G&1", "p_mw": 12.5, "q_mvar": 3.75, "q_limit": False}],
        "totals": {"generation_p_mw": 12.5, "load_p_mw": 10.0, "branch_loss_p_mw": 2.5},
    }


def test_generates_traceable_tex_csv_and_manifest(tmp_path):
    result = _loadflow_result()
    snapshot = {
        "study_id": "caso_#1",
        "title": "Impacto A&B",
        "objective": "Revisar 10% de la ruta datos\\red_1",
        "model": {"base_mva": 100.0, "source": "modelo_#1.json"},
        "scenario": {"alternative": "A_1", "stage": "2026"},
        "assumptions": ["Carga constante"],
        "missing_data": ["TC no informado"],
        "diagnostics": ["Sin comparación PF y operación según condición válida"],
        "conclusions": ["Resultado limitado a {islas}~A^B, conclusión técnica"],
        "network_model": {"buses": [{"id": "B_1", "name": "Barra ágil", "un_kv": 220.0,
                                      "path": "Red\\SE Prueba.ElmNet\\Barra ágil.ElmTerm"}],
                          "branches": [{"id": "L#1", "name": "Línea ñ", "class": "ElmLne",
                                        "params": {}, "units": {}, "type_params": {}, "type_units": {}}],
                          "gens": [{"id": "G&1", "name": "Generación ó", "bus": "B_1"}]},
    }
    report = generate_study_report(tmp_path, snapshot, [{
        "module": "loadflow",
        "run_id": "lf_#1",
        "freshness": "vigente",
        "coverage": "isla energizada, una isla sin referencia",
        "model": snapshot["network_model"],
        "result": result,
    }], compile_pdf=False)
    tex = Path(report["tex"])
    manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
    assert tex.is_file()
    assert manifest["schema"] == "power_machine-study-report-manifest-v1"
    assert manifest["latex"]["status"] == "not_requested"
    assert manifest["inputs"]["snapshot_sha256"]
    assert "informe.tex" in manifest["outputs"]
    text = tex.read_text(encoding="utf-8")
    assert ";" not in text
    assert "Impacto A\\&B" in text
    assert "Barra ágil" in text
    assert "Línea ñ" in text
    assert "B\\_1" not in text
    assert "10\\%" in text
    assert r"\{islas\}\textasciitilde{}A\textasciicircum{}B" in text
    assert r"\textbackslash{}" in text
    assert "sin validar contra PF" in text
    assert "partial" not in text
    assert "0.13\\textwidth" not in text
    assert "1.0123" in text
    assert "1.012345678901" not in text
    verify_tex_table_values(tex, result)
    verify_tex_utf8(tex, snapshot["diagnostics"] + snapshot["conclusions"] + ["áéíóúñ"])
    bus_csv = next(path for path in report["csv"] if path.endswith("_barras.csv"))
    with open(bus_csv, encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["u_pu"] == repr(result["buses"][0]["u_pu"])


def test_latex_escape_covers_special_characters():
    assert latex_escape(r"r_1#2%3&4{5}~6^7\8") == (
        r"r\_1\#2\%3\&4\{5\}\textasciitilde{}6\textasciicircum{}7\textbackslash{}8")




def test_calculation_envelope_adds_restrictions_vector_pdf_and_values(tmp_path):
    envelope = {
        "module": "voltage_stability", "status": "completed",
        "payload": {"curve_type": "PV", "points": [
            {"increase_mw": 0.0, "minimum_voltage": {"u_pu": 1.0, "name": "Barra prueba"}, "status": "completed"},
            {"increase_mw": 10.0, "minimum_voltage": {"u_pu": 0.975, "name": "Barra prueba"}, "status": "completed"},
        ]},
    }
    report = generate_study_report(tmp_path, {"study_id": "sobres", "network_model": {"buses": []},
                                               "capabilities": {"calculations": {"voltage_stability": {
                                                   "restrictions": ["Margen numérico, no límite físico"]}}}}, [{
        "module": "voltage_stability", "run_id": "pv_prueba", "result": envelope,
        "restrictions": ["Fuente ficticia sin capacidad real"],
    }], compile_pdf=False)
    tex = Path(report["tex"])
    text = tex.read_text(encoding="utf-8")
    assert "Curva PV" in text
    assert "Margen numérico, no límite físico" in text
    assert "Fuente ficticia sin capacidad real" in text
    assert "10.000000" not in text
    assert "10" in text and "0.975" in text
    assert (tmp_path / "figuras" / "figura010.pdf").read_bytes().startswith(b"%PDF-")
    verify_tex_envelope_values(tex, envelope)


def test_engineering_body_omits_massive_and_unavailable_columns(tmp_path):
    buses = [{"name": "Barra %d" % index, "un_kv": 220.0,
              "du_dp_pu_per_mw": index / 10000.0, "du_dq_pu_per_mvar": index / 9000.0}
             for index in range(40)]
    branches = [{"name": "Línea %d" % index, "ptdf_mw_per_mw": index / 100.0,
                 "lodf_mw_per_mw": None, "lodf_error_mw": None} for index in range(40)]
    report = generate_study_report(tmp_path, {"study_id": "sens", "network_model": {"buses": []}}, [{
        "module": "sensitivities", "run_id": "sens_prueba",
        "result": {"payload": {"buses": buses, "branches": branches}}}], compile_pdf=False)
    text = Path(report["tex"]).read_text(encoding="utf-8")
    assert "No informado" not in text
    assert "LODF y error no se imprimen" in text
    assert (tmp_path / "sensibilidades_barras_completas.csv").is_file()
    assert (tmp_path / "sensibilidades_ramas_completas.csv").is_file()
    _assert_body_tables_short_and_informative(text)
