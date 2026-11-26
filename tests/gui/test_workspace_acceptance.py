from gui_support import ieee_model, ieee_root
"""Comparación por islas y persistencia real de controles del documento."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import csv
import json
import pytest
from power_machine.gui.app_compare import compare_flows,export_comparison

from gui_support import ROOT


def test_delta_loadflow_and_unresolved_are_not_zero(tmp_path):
    from gui_support import ieee_model as two_bus_model
    from power_machine.loadflow import run_loadflow
    base=two_bus_model()
    ids={'l1':'LD04','b2':'B04'}
    other=deepcopy(base)
    next(x for x in other['loads'] if x['id']==ids['l1'])['params']['plini']*=1.1
    a,b=run_loadflow(base),run_loadflow(other)
    rows=compare_flows(base,a,other,b)
    bus=next(r for r in rows['buses'] if r['object_id']==ids['b2'])
    assert bus['delta_u_pu']<0 and bus['status']=='Comparable'
    assert any(r['delta_p_from_mw'] and r['delta_p_from_mw']>0 for r in rows['branches'])
    broken=deepcopy(b)
    for island in broken['islands']:island['status']='no_converge'
    absent=compare_flows(base,a,other,broken)
    assert all(r['delta_u_pu'] is None for r in absent['buses'])
    path=tmp_path/'differences.csv'
    export_comparison(path,'buses',rows['buses'])
    with path.open(encoding='utf-8-sig',newline='') as stream:table=list(csv.reader(stream))
    assert 'ΔU [p.u.]' in table[0]
    assert any(float(r[4])<0 for r in table[1:] if r[4])


@pytest.fixture
def gui(tmp_path, monkeypatch):
    from power_machine.gui.app import Application
    import tkinter as tk
    import time
    for attempt in range(3):
        try:
            window=Application(ROOT)
            break
        except tk.TclError as exc:
            if attempt == 2:
                pytest.skip(f"Entorno Tk no disponible tras tres intentos: {exc}")
            time.sleep(.2)
    window.open_ieee39()
    errors=[]
    window.report_callback_exception=lambda *exc:errors.append(exc)
    window.show_error=lambda exc:pytest.fail(str(exc))
    yield window
    window._closed = True
    window.manager.shutdown()
    window.close_parity_panels()
    window.destroy()
    assert not errors


def test_presentation_and_workspaces_roundtrip(gui,tmp_path,monkeypatch):
    from power_machine.gui.document import Document
    from power_machine.dsl_editor import template_draft
    app=gui
    page=app.diagrams[None]
    page.set_color_mode('loading')
    page.set_result_format({'u_pu':5})
    page.set_detail_zoom(.91)
    app.open_parity_panel('dsl_editor')
    panel=app.parity_panels['dsl_editor'][1].panel
    draft=template_draft('first_order')
    panel.controller.set_draft(draft)
    texts=app.dsl_texts(panel)
    texts['sAddEquat']='y = u'
    app.dsl_texts(panel,texts)
    app.open_data_table('loads')
    manager=app.data_windows[-1][1]
    manager.column_visible['p_mw'].set(False)
    manager.populate_table()
    plots=[page.get_presentation() for page in app.plot_pages]
    app.run_loadflow()
    from gui_support import run_until
    run_until(app,lambda:not app.busy,timeout=45)
    branches=next(t for t in app.table_pages if t.kind=='branches')
    branches.column_visible['p_from_mw'].set(False)
    branches.populate()
    path=tmp_path/'presentation.power_machine'
    monkeypatch.setattr('power_machine.gui.app.filedialog.asksaveasfilename',lambda **kwargs:str(path))
    assert app.save_case(True)
    stored=app.document.to_data()['presentation']
    reopened=Document.open(path,app.api.StudyCase)
    assert reopened.to_data()['presentation']==stored
    app.install_document(reopened)
    assert app.diagrams[None].color_mode=='loading'
    assert app.diagrams[None].result_digits['u_pu']==5
    assert app.diagrams[None].detail_zoom==.91
    assert app.document.study.plots==plots
    assert not app.data_windows[-1][1].column_visible['p_mw'].get()
    app.open_parity_panel('dsl_editor')
    restored=app.parity_panels['dsl_editor'][1].panel
    assert asdict(restored.controller.draft)==asdict(draft)
    assert app.dsl_texts(restored)['sAddEquat']=='y = u'
    app.run_loadflow()
    run_until(app,lambda:not app.busy,timeout=45)
    assert not next(t for t in app.table_pages if t.kind=='branches').column_visible['p_from_mw'].get()


def test_window_preferences_and_capability_details(gui):
    app=gui
    app.state('normal')
    app.geometry('1100x700+0+0')
    app.update()
    app.horizontal.sashpos(0,280)
    app.save_window_preferences()
    value=json.loads(app.workspace_preferences_path().read_text(encoding='utf-8'))
    assert value['main_geometry'].startswith('1100x700')
    assert value['panel_options']['workspace_v3']['sidebar']==280
    app.geometry('1000x600')
    app.restore_window()
    app.update()
    app.restore_dividers()
    assert app.winfo_width()==1100 and app.horizontal.sashpos(0)==280
    assert app.calculation_capabilities['qualification_revision'] == 'capabilities-v1'
    assert app.calculation_details['calc_cct']['restrictions']
    assert app.validation_records() == []


def test_protection_workspace_roundtrip(gui,tmp_path):
    from power_machine.gui.document import Document
    from power_machine.protection import RelayStage,standard_curve
    from power_machine.protection.settings import new_workspace
    app=gui
    app.open_parity_panel('protection')
    controller=app.parity_panels['protection'][1].panel.controller
    stage=RelayStage('relay_fixture',None,'fixture',None,None,100.,.2,standard_curve('iec','very_inverse'),True,'phase')
    controller.workspace=new_workspace([stage])
    controls=app.protection_controls(app.parity_panels['protection'][1].panel)
    dial=next(key for key,value in controls.items() if value=='0.1')
    controls[dial]='0.234'
    app.protection_controls(app.parity_panels['protection'][1].panel,controls)
    app.capture_presentation()
    path=tmp_path/'protection.power_machine'
    app.document.save(path)
    app.install_document(Document.open(path,app.api.StudyCase))
    app.open_parity_panel('protection')
    assert 'relay_fixture' in app.parity_panels['protection'][1].panel.controller.workspace['stages']
    assert app.protection_controls(app.parity_panels['protection'][1].panel)[dial]=='0.234'


def test_integrator_preserves_plot_axis_reference_and_log_scale():
    from types import SimpleNamespace
    import numpy as np
    from power_machine.gui.app_plots import EngineeringPlotPage
    from power_machine.gui.plots import SignalBuffer
    data=SignalBuffer()
    data.reset(['time_s','g|frequency|Hz'])
    data.append([[0,60.],[1,59.8]])
    page=SimpleNamespace(spec={'curves':[{'key':'g|frequency|Hz','axis':0}],
        'axis_specs':[{'unit':'Hz','reference':'deviation','base':60.,'scale':'linear'}]},
        _frequency_options=lambda:('absolute',50.))
    x,y,unit=EngineeringPlotPage._series(page,data,'g|frequency|Hz')
    assert np.allclose(y,[0.,-.2]) and unit=='Hz de desviación'
    assert data.rows==[[0.,60.],[1.,59.8]]


def test_comparison_reuses_current_flows_and_presentation_undo(gui):
    from gui_support import run_until
    app=gui
    app.close_parity_panels()
    doc=app.document
    base=doc.active_case_id
    other=doc.duplicate_case(base,'Comparación +3 %',doc.revision)
    load=next(l for l in app.model['loads'] if not l.get('outserv'))
    scenario=doc.create_scenario('Demanda +3 %',{'load_scale':{load['id']:1.03}},doc.revision)
    doc.configure_case(other,doc.revision,scenario_id=scenario)
    app.compare_cases(base,other)
    run_until(app,lambda:not app.busy and hasattr(app,'last_comparison'),timeout=45)
    assert any(row['delta_u_pu'] not in (None,0) for row in app.last_comparison['tables']['buses'])
    app.comparison_window.destroy()
    app.compare_cases(base,other)
    run_until(app,lambda:not app.busy,timeout=45)
    assert app.last_comparison['reused']==[True,True]
    app.comparison_window.destroy()
    app.capture_presentation()
    before=doc.materialize_case(base)[2]
    original=app.diagrams[None].color_mode
    app.diagrams[None].set_color_mode('state')
    app.capture_presentation()
    assert doc.history.step('presentation')
    assert app.diagrams[None].color_mode==original
    assert doc.materialize_case(base)[2]==before
