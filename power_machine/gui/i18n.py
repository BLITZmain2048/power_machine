"""Live English and Spanish message catalogues."""
import json,locale,os
from pathlib import Path
from .bus import bus
_CATALOGUES={}
_missing=set()
_code='es' if (locale.getlocale()[0] or '').lower().startswith('es') else 'en'
def _catalogue(code):
    if code not in _CATALOGUES:
        try:_CATALOGUES[code]=json.loads((Path(__file__).parent/'locale'/f'{code}.json').read_text(encoding='utf8'))
        except (OSError,ValueError):_CATALOGUES[code]={}
    return _CATALOGUES[code]
def language():return _code
def set_language(code):
    global _code
    if code not in ('en','es'):raise ValueError('Unsupported language: '+str(code))
    _code=code
    bus.publish('language.changed',code=code)
def tr(key,**fmt):
    english=_catalogue('en')
    text=_catalogue(_code).get(key)
    if text is None:
        text=english.get(key,key)
        if os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get('POWER_MACHINE_I18N_TEST'):_missing.add((_code,key))
    return text.format(**fmt) if fmt else text
def missing_keys():return set(_missing)


def translate_text(value):
    """Translate existing display text without rebuilding an edited widget."""
    from string import Formatter
    import re
    value = str(value)
    target = _catalogue(_code)
    for code in ('en','es'):
        for key, source in _catalogue(code).items():
            if source == value:
                return target.get(key, _catalogue('en').get(key, source))
            if '{' not in source:
                continue
            pattern, names = '', []
            try:
                for literal, name, spec, conversion in Formatter().parse(source):
                    pattern += re.escape(literal)
                    if name is not None:
                        names.append(name)
                        pattern += '(.*?)'
                match = re.fullmatch(pattern, value, re.DOTALL)
                if match:
                    # Values are already formatted. Strip numeric specifications.
                    output = ''
                    values = dict(zip(names, match.groups()))
                    for literal, name, spec, conversion in Formatter().parse(target.get(key, source)):
                        output += literal + (values.get(name, '') if name is not None else '')
                    return output
            except ValueError:
                continue
    return value


def retranslate_tree(root):
    """Refresh shell labels and delegate live panels through their public hook."""
    import tkinter as tk
    from tkinter import ttk
    def visit(widget):
        try:
            hook = getattr(widget, 'retranslate', None)
            if callable(hook):
                hook()
            if isinstance(widget, (tk.Tk, tk.Toplevel)):
                widget.title(translate_text(widget.title()))
            if isinstance(widget, tk.Menu):
                end = widget.index('end')
                for index in range((end if end is not None else -1)+1):
                    if widget.type(index) not in ('separator','tearoff'):
                        widget.entryconfigure(index, label=translate_text(widget.entrycget(index,'label')))
            elif 'text' in widget.keys():
                widget.configure(text=translate_text(widget.cget('text')))
            if isinstance(widget, ttk.Notebook):
                for tab in widget.tabs():
                    widget.tab(tab, text=translate_text(widget.tab(tab,'text')))
            if isinstance(widget, ttk.Treeview):
                for column in ('#0', *widget['columns']):
                    widget.heading(column, text=translate_text(widget.heading(column,'text')))
            for child in widget.winfo_children():
                visit(child)
        except tk.TclError:
            return
    visit(root)
