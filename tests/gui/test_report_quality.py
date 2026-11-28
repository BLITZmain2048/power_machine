"""Regresión editorial del informe y de su evidencia gráfica."""
import json
import re
from pathlib import Path
import pytest

pytestmark = pytest.mark.gui
from power_machine.gui.report_templates.calculations import write_figure
from power_machine.gui.study_report import generate_study_report
ROOT=Path(__file__).resolve().parents[1]

def audit_body(text):
    body=text.split(r'\appendix')[0]
    assert not re.search(r'[A-Za-zÁÉÍÓÚáéíóúñ]\?[A-Za-zÁÉÍÓÚáéíóúñ]',body)
    for word in ('schema','validation status','elapsed s','model sha256','steps executed','energized\\_status','summary rows','Increase Mw','Total Load'):
        assert word not in body
    assert 'ref\\_' not in body
    for block in body.split(r'\begin{longtable}')[1:]:
        data=block.split(r'\endhead',1)[1].split(r'\bottomrule',1)[0]
        rows=[line[:-2].split(' & ') for line in data.splitlines() if ' & ' in line and line.endswith(r'\\')]
        assert len(rows)<=25
        for column in zip(*rows):
            assert not all(v.strip() in ('','ausente') for v in column)


@pytest.mark.parametrize('text',[
    'Tensi?n mínima', 'schema', 'elapsed s',
    r'\begin{longtable}\endhead'+'\na & ausente \\\\\nb & ausente \\\\\n'+r'\bottomrule',
    r'\begin{longtable}\endhead'+'\n'.join('a & 1 \\\\' for _ in range(26))+r'\bottomrule',
])
def test_editorial_guard_rejects_bad_outputs(text):
    with pytest.raises(AssertionError): audit_body(text)

def test_figure_requires_both_axis_labels(tmp_path):
    with pytest.raises(ValueError):
        write_figure(tmp_path,{'series':[{'x':[0,1],'y':[1,2],'label':'Prueba'}],'xlabel':'Tiempo (s)'},1)

def test_external_output_rejected_before_mkdir():
    with pytest.raises(ValueError,match='carpeta de resultados'):
        generate_study_report(ROOT.parent/'salida_prohibida',{},[],compile_pdf=False)





