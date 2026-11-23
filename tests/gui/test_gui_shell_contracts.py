import json,threading,time
from pathlib import Path
import pytest
from power_machine.gui.bus import EventBus
from power_machine.gui.theme import Theme
from power_machine.gui.settings import Settings
from power_machine.gui.i18n import tr,set_language,language


def test_event_delivery_and_unsubscribe():
    bus=EventBus()
    seen=[]
    token=bus.subscribe('selection.changed',lambda **p:seen.append(p))
    bus.publish('selection.changed',ids=['B1'],source='test')
    bus.unsubscribe(token)
    bus.publish('selection.changed',ids=['B2'],source='test')
    assert seen==[dict(ids=['B1'],source='test')]


def test_worker_delivery_occurs_on_tk_thread(tk_root):
    bus=EventBus(tk_root)
    seen=[]
    bus.subscribe('results.updated',lambda **p:seen.append(threading.get_ident()))
    worker=threading.Thread(target=lambda:bus.publish('results.updated',study='lf',result_id='one'))
    worker.start();worker.join()
    deadline=time.monotonic()+2
    while not seen and time.monotonic()<deadline:tk_root.update();time.sleep(.01)
    assert seen==[threading.get_ident()]
    bus.close()


def test_theme_tokens_and_live_style(tk_root):
    theme=Theme()
    required='bg surface surface_alt border text text_muted accent accent_text ok warn error selection grid diagram_bg bus branch deenergized kv_345 kv_230 kv_138 kv_low v_low v_ok v_high'.split()
    for name in ('light','dark'):
        theme.set(name);theme.apply(tk_root)
        for key in required+[f'load_{i}' for i in range(121)]:assert theme.color(key)
        assert theme.mpl_rc()['axes.facecolor']==theme.color('surface')


def test_settings_corruption_and_roundtrip(tmp_path):
    p=tmp_path/'settings.json';p.write_text('broken',encoding='utf8')
    settings=Settings(p);assert settings.get('theme')=='light'
    settings.set('theme','dark');settings.save()
    assert Settings(p).get('theme')=='dark'


def test_language_catalogues_and_fallback():
    old=language()
    try:
        set_language('en');assert 'Network' in tr('window.diagram')
        set_language('es');assert 'Diagrama' in tr('window.diagram')
        with pytest.raises(ValueError):set_language('xx')
    finally:set_language(old)
