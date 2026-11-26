"""Transporte del ensayo nativo incluido. No completa redes del usuario."""
import json


def is_native_case(model, study):
    from power_machine.resources import readonly_path
    from power_machine.modal.results import fingerprint
    if model.get('meta', {}).get('benchmark') != 'ieee39_pf' or any(study.get('scenario', {}).values()):
        return False
    from power_machine.resources import pf_data_available, PFReferenceDataError
    if not pf_data_available(): raise PFReferenceDataError()
    reference = json.loads(readonly_path('cases', 'ieee39_pf', 'model.json').read_text(encoding='utf-8-sig'))
    try:
        return fingerprint(model) == fingerprint(reference)
    except (ValueError, TypeError):
        return False


def simulate_native(root, study, model, queue, cancel):
    """Ejecuta eventos del formulario, sin sustituirlos por un ensayo prefijado."""
    import csv
    import math
    import time
    from power_machine.resources import readonly_path
    from power_machine.benchmarks.ieee39_pf import build_rms_case
    from power_machine.dynamics.events import EventTimeline
    from power_machine.study import SimEvent, _translate_event, element_index
    from .paths import run_directory
    from .i18n import tr
    started = time.monotonic()
    options = study['options']
    rms = build_rms_case(readonly_path('cases', 'ieee39_pf'), step_s=options['dt_s'])
    index = element_index(model)
    events = [_translate_event(SimEvent.from_dict(row), index, model['meta']['s_base_mva'])
              for row in study.get('events', [])]
    # El estado inicial publicado es de equilibrio. El reloj respeta el inicio del estudio.
    initial = rms.initial.copy()
    initial.t = float(options.get('t_start_s', 0))
    step = rms.solver.step
    def advance(state, step_s=None):
        if cancel.is_set():
            raise InterruptedError(tr('integration.cancellation_requested'))
        before = time.monotonic()
        value = step(state, step_s)
        queue.put(('progress', value[0].t, options['t_end_s'], (time.monotonic() - before) * 1000))
        return value
    rms.solver.step = advance
    run = EventTimeline(events).run(rms.solver, initial, options['t_end_s'])
    buses = model['buses']
    states = [s for s in rms.manifest if s['kind'] == 'machine' and s['variable'] in ('speed', 'phi')]
    keys = [b['path'] + '|m:u|p.u.' for b in buses]
    keys += [s['relative_path'] + ('|s:xspeed|p.u.' if s['variable'] == 'speed' else '|s:phi|deg') for s in states]
    # El catálogo de la red conserva sus rutas completas como IDs de señal.
    gen_paths = {g['id']: g['path'] for g in model['gens']}
    keys[len(buses):] = [gen_paths[s['object_id']] + ('|s:xspeed|p.u.' if s['variable'] == 'speed' else '|s:phi|deg') for s in states]
    rows = []
    for sample in run.samples:
        row = [float(sample.t)] + [float(abs(sample.v[rms.network.index_of_bus(b['id'])])) for b in buses]
        row += [float(sample.x[s['index']]) * (180 / math.pi if s['variable'] == 'phi' else 1.) for s in states]
        rows.append(row)
    directory = run_directory(root, 'rms_native')
    with (directory / 'signals.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['time_s'] + keys)
        writer.writerows(rows)
    (directory / 'study_snapshot.json').write_text(json.dumps(study, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'model_snapshot.json').write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding='utf-8')
    report = dict(completed=True, cancelled=False, steps=len(rows), wall_s=time.monotonic() - started,
                  states=len(rms.initial.x), profile='ieee39_native', avr_profile=rms.avr_profile,
                  events=study.get('events', []), f_nom_hz=model['meta']['f_nom_hz'],
                  pf_validation=False, signals_csv=str(directory / 'signals.csv'))
    (directory / 'run_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    queue.put(('header', ['time|b:tnow|s'] + keys))
    queue.put(('samples', rows))
    queue.put(('done', report))
