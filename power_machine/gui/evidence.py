"""Alcance de cotejos incluidos, separado de la validación global del motor."""
import hashlib
import json
from dataclasses import asdict


def sc_reference_label(model, options, supplemental, fault):
    """Reconoce exclusivamente la entrada REF y el perfil trifásico publicados."""
    from power_machine.resources import readonly_path, ref_data_available
    if not ref_data_available(): return None
    from power_machine.shortcircuit.results import fingerprint
    from power_machine.refcompare.cc import build_supplement
    from power_machine.shortcircuit import ShortCircuitOptions
    if options.profile!='iec_ref2016_c1_v1' or fault.kind!='3ph' or fault.r_fault_ohm or fault.x_fault_ohm:
        return None
    root=readonly_path('cases','ieee39_ref')
    reference=json.loads((root/'model.json').read_text(encoding='utf-8-sig'))
    if fingerprint(model)!=fingerprint(reference):return None
    expected=asdict(ShortCircuitOptions('iec60909','maximum',frequency_hz=60,min_break_time_s=.1,profile='iec_ref2016_c1_v1'))
    actual=asdict(options)
    # La duración térmica no interviene en las tres magnitudes cotejadas.
    for key in expected:
        if key!='fault_duration_s' and actual[key]!=expected[key]:return None
    for relative,sha in reference['meta']['sha256_inputs'].items():
        if hashlib.sha256((root/relative).read_bytes()).hexdigest()!=sha:return None
    if not (root/'E39-CC-01/fault_summary.csv').is_file():return None
    if fingerprint(supplemental)!=fingerprint(build_supplement(reference,root)):return None
    return 'cotejado con la referencia externa (IEEE 39)'


def sc_result_matches_reference(result, model):
    """Comprueba las tres magnitudes actuales contra la referencia por barra."""
    from power_machine.resources import ref_data_available
    if not ref_data_available(): return False
    import csv,math
    from power_machine.resources import readonly_path
    payload=result.get('payload',{})
    fault=payload.get('fault',{})
    ident=fault.get('bus_id',fault.get('object_id'))
    bus=next((b for b in model.get('buses',[]) if b['id']==ident),None)
    if bus is None:return False
    number=str(int(bus['name'].removeprefix('Bus ')))
    with readonly_path('cases','ieee39_ref','E39-CC-01','fault_summary.csv').open(encoding='utf-8-sig',newline='') as stream:
        row=next((r for r in csv.DictReader(stream) if str(r['fault_bus'])==number),None)
    if row is None:return False
    for actual,key,tolerance in [('Ik_initial_sym_ka','Ikss_kA',.005),('ip_peak_ka','ip_kA',.01),('Ib_sym_ka','Ib_kA',.01)]:
        value=payload.get(actual)
        reference=float(row[key])
        if value is None or not math.isfinite(float(value)) or abs(float(value)-reference)>tolerance*abs(reference):return False
    return True
