from gui_support import ieee_model, ieee_root
"""Contratos de integración v2 y enlaces por contenido, sin sustituir motores."""
from types import SimpleNamespace
from pathlib import Path
from power_machine.gui.app_study import StudyActions
from power_machine.gui.document import Document
from power_machine.study import new_study
from power_machine.gui.workspace_links import file_sha256
from power_machine.gui.i18n import tr

from gui_support import ROOT


def test_archive_reopen_and_changed_content(tmp_path,monkeypatch):
    # Este contrato varía los archivos de entrada, no el código del motor.
    # Fijar su versión evita que ediciones paralelas invaliden otro supuesto.
    from power_machine.gui import workspace_links
    engine_version = workspace_links.engine_sha256()
    monkeypatch.setattr(workspace_links, 'engine_sha256', lambda: engine_version)
    monkeypatch.setenv('POWER_MACHINE_OUTPUT_DIR',str(tmp_path/'runs'))
    document=Document(new_study(ieee_root()))
    from power_machine.study import load_model
    document.set_base(ieee_model())
    engine=tmp_path/'power_machine'
    engine.mkdir()
    (engine/'engine.py').write_text('# Motor de prueba del archivo de enlaces',encoding='utf-8')
    app=SimpleNamespace(document=document,root_path=tmp_path)
    source=tmp_path/'supplement.json'
    source.write_text('{"input":1}',encoding='utf-8')
    from power_machine.gui.workspace_links import relative_path
    result=dict(status='partial',coverage={'reason':'sin fuente completa'},provenance={'input_files':[
        dict(path=relative_path(source),sha256=file_sha256(source),role='supplement')]})
    link=StudyActions.archive_result(app,'shortcircuit',result,'shortcircuit')
    assert document.result_freshness(link['run_id'])=='current'
    path=tmp_path/'document.power_machine'
    document.save(path)
    reopened=Document.open(path,type(document.study))
    assert reopened.result_freshness(link['run_id'])=='current'
    source.write_text('{"input":2}',encoding='utf-8')
    assert reopened.result_freshness(link['run_id'])=='stale'
    source.unlink()
    assert reopened.result_freshness(link['run_id'])=='missing'


def test_active_identity_includes_scenario_variant_stage():
    document=Document(new_study(ieee_root()))
    from power_machine.study import load_model
    document.set_base(ieee_model())
    variant=document.create_variant('Alternativa',document.revision)
    stage=document.add_stage(variant,'Etapa 1','2027-01-01T00:00:00-03:00',[],document.revision)
    document.configure_case(document.active_case_id,document.revision,variant_id=variant,stage_id=stage)
    title=StudyActions.study_identity(SimpleNamespace(document=document))
    assert 'Alternativa' in title and 'Etapa 1' in title and tr('study.default_operation') in title
