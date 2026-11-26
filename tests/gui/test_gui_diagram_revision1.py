"""Regression coverage for the first diagram review."""
from copy import deepcopy
from types import SimpleNamespace
import math
import pytest

from gui_support import ROOT, ieee_model, ieee_root
from power_machine.study import new_study, run_loadflow
from power_machine.gui.diagram import DiagramWidget
from power_machine.gui.diagram.loading import diagram_loading
from power_machine.gui.bus import EventBus
from power_machine.gui.i18n import language, set_language, tr
from power_machine.gui.theme import theme


@pytest.fixture(scope='module')
def inputs():
    model=ieee_model()
    result=run_loadflow(ROOT,new_study(ieee_root()),model)
    return model,result


def test_ieee39_rate_a_is_shown_for_both_terminals_without_mutating_inputs(inputs):
    model,result=inputs
    before=deepcopy((model,result))
    branch=next(b for b in model['branches'] if b['id']=='L05-08')
    row=next(b for b in result['branches'] if b['id']==branch['id'])
    assert branch['rate_a_mva']==900
    summary=diagram_loading(model,result)[branch['id']]
    for end in ('from','to'):
        expected=100*math.hypot(row[f'p_{end}_mw'],row[f'q_{end}_mvar'])/900
        assert summary['ends'][end]['loading_pct']==pytest.approx(expected)
    assert summary['rating']['source']=='rate_a_mva'
    assert summary['loading_pct']==max(v['loading_pct'] for v in summary['ends'].values())
    assert (model,result)==before


@pytest.mark.parametrize('status', ['no_convergente','sin_referencia','sin_energia_pasiva'])
def test_rate_a_never_unmasks_unsolved_islands(inputs,status):
    model,result=deepcopy(inputs)
    row=next(b for b in result['branches'] if b['id']=='L05-08')
    row['status']=status
    assert diagram_loading(model,result)['L05-08']['loading_pct'] is None


def test_detail_panel_icons_status_and_ordered_ranking(tk_root,tmp_path,inputs):
    old_code=language()
    set_language('en')
    model,result=inputs
    bus=EventBus(tk_root)
    view=DiagramWidget(tk_root,SimpleNamespace(model=model,layout_path=tmp_path/'layout.json'),bus)
    view.pack(fill='both',expand=True)
    try:
        tk_root.update()
        view.set_result_boxes_visible(True)
        view.set_loadflow_results(result)
        view.select_ids(['L05-08'])
        tk_root.update()
        assert not view.canvas.find_withtag('selected_detail')
        assert view._detail_panel.winfo_manager()=='pack'
        assert 'rateA = 900 MVA' in view._detail_text.get()
        assert tr('diagram.loading')+' —' not in view._detail_text.get()
        # The panel occupies its own space to the right, never the diagram surface.
        assert view.canvas.winfo_rootx()+view.canvas.winfo_width() <= view._detail_panel.winfo_rootx()
        view.toggle_detail()
        tk_root.update()
        assert not view._detail_panel.winfo_manager()
        for name in ('select','pan','move','fit','save','detail','legend','minimap','search','layers'):
            button=view._icon_buttons[name]
            assert button.cget('image')
            assert button._diagram_tooltip.text==tr('diagram.'+name)
        view.retranslate()
        assert 'disposición' not in view.status.get()
        assert 'Network diagram' in view.status.get()
        ids=['L05-08','L01-02','L03-04']
        bus.publish('contingency.ranking.changed',ids=ids)
        assert view.ranking_ids==tuple(ids)
        assert [view.canvas.itemcget(i,'text') for i in view.canvas.find_withtag('ranking')]==['1','2','3']
        bus.publish('contingency.ranking.changed',ids=ids[::-1])
        assert view.ranking_ids==tuple(ids[::-1])
        legend=' '.join(view._footer.itemcget(i,'text') for i in view._footer.find_all() if view._footer.type(i)=='text')
        assert 'Switch' in legend and 'Busbar' in legend
        tooltip=view._icon_buttons['fit']._diagram_tooltip
        tooltip.show()
        assert tooltip.window.winfo_exists()
        tooltip.hide()
    finally:
        view.destroy()
        bus.close()
        set_language(old_code)
