"""Lectura y presentación de trayectorias nativas y referencias externas."""
import csv
from pathlib import Path
from .plots import SignalBuffer, TIME_KEY
from .i18n import tr


def native_comparison(native_directory, reference_directory, signals=('B4_U_pu','G32_speed_pu')):
    """Alinea identidades por metadatos, sin interpolar ni modificar muestras."""
    from power_machine.refcompare.rms_replay import load_reference
    native_directory,reference_directory=Path(native_directory),Path(reference_directory)
    with (native_directory/'channel_units.csv').open(encoding='utf-8-sig',newline='') as stream:
        channels=list(csv.DictReader(stream))
    by_signal={ch['signal']:ch for ch in channels}
    selected=[by_signal[signal] for signal in signals]
    keys=[f"native:{ch['signal']}|{ch['variable']}|{ch['unit']}" for ch in selected]
    own,reference=SignalBuffer(),SignalBuffer()
    own.reset([TIME_KEY]+keys)
    reference.reset([TIME_KEY]+keys)
    with (native_directory/'signals_native.csv').open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.reader(stream,delimiter=';')
        next(reader)
        next(reader)
        indices=[next(i+1 for i,ch in enumerate(channels) if ch['signal']==signal) for signal in signals]
        own.append([[row[0]]+[row[i] for i in indices] for row in reader])
    times,values,_=load_reference(reference_directory)
    end=own.rows[-1][0]
    reference.append([[t]+[values[signal][i] for signal in signals] for i,t in enumerate(times) if t<=end+1e-12])
    curves=[dict(key=key,axis=i,label=signal.split('_')[0],line_style='-',color_token='accent',reference_color_token='warn',line_width=1.8)
            for i,(key,signal) in enumerate(zip(keys,signals))]
    spec=dict(name=tr('plots.native_title'), name_key='plots.native_title',
              axes=len(curves),curves=curves,show_ref=True,source_legend=True,frequency_base_hz=60.,
              events=[dict(time_s=1.,label=tr('plots.fault_b16'), label_key='plots.fault_b16'),dict(time_s=1.1,label=tr('plots.clear_l16'), label_key='plots.clear_l16')],
              axis_scales={str(i):dict(xlim_s=[.9,1.2]) for i in range(len(curves))})
    return spec,own,reference
