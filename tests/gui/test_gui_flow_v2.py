"""Contratos del flujo v2 de casos, papelera, corridas y eventos."""
from copy import deepcopy

from gui_support import analytic_history
from power_machine.gui.document import Document
from power_machine.gui.events_editor import validate_events
from power_machine.study import StudyCase


def test_case_trash_restore_and_undo_keep_document_data():
    _, history, _ = analytic_history()
    model = history.model
    document = Document(StudyCase(name='Base'))
    document.set_base(model)
    copied = document.duplicate_case(document.active_case_id, 'Alternativa', document.revision)
    document.move_to_trash('case', copied, document.revision)
    assert copied not in {row['case_id'] for row in document.to_data()['cases']}
    assert document.to_data()['trash'][0]['item']['name'] == 'Alternativa'
    document.restore_from_trash(0, document.revision)
    assert copied in {row['case_id'] for row in document.to_data()['cases']}
    assert document.undo()
    assert copied not in {row['case_id'] for row in document.to_data()['cases']}


def test_unused_scenario_and_variant_move_to_trash():
    _, history, _ = analytic_history()
    model = history.model
    document = Document(StudyCase())
    document.set_base(model)
    scenario = document.create_scenario('Escenario descartable', {}, document.revision)
    variant = document.create_variant('Alternativa descartable', document.revision)
    document.move_to_trash('scenario', scenario, document.revision)
    document.move_to_trash('variant', variant, document.revision)
    trash = document.to_data()['trash']
    assert [entry['kind'] for entry in trash] == ['scenario', 'variant']


def test_event_validation_accepts_diagram_fault_and_clear_pair():
    _, history, ids = analytic_history()
    model = history.model
    index = {row['id']: dict(deepcopy(row), **{'class': 'ElmTerm', 'group': 'buses'}) for row in model['buses']}
    options = StudyCase().options
    fault = dict(time_s=1.0, kind='short_circuit', target_id=ids['b1'], params=dict(r_ohm=0.1, x_ohm=1.0))
    clear = dict(time_s=1.1, kind='clear_short_circuit', target_id=ids['b1'], params={})
    assert validate_events([fault, clear], options, index) == []
