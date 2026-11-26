"""One application-wide registry for study keys in data and diagram windows."""
import tkinter as tk
from tkinter import ttk
from .i18n import tr


STUDY_KEYS = {
    '<F11>':'initial_conditions', '<F12>':'simulation_dialog',
    '<Escape>':'stop', '<F9>':'focus_other', '<Control-s>':'save_case',
    '<Control-o>':'open_case', '<Control-n>':'new_case', '<Control-z>':'undo', '<Control-y>':'redo',
}


def install_study_shortcuts(app):
    if getattr(app,'_study_shortcut_bindings',None):
        return
    app._study_shortcut_bindings={}
    def stop_once(event):
        identity=(event.serial,event.time,str(event.widget))
        if getattr(app,'_handled_escape',None)==identity:return
        app._handled_escape=identity
        app.stop()
    tag='PowerMachineEscape'
    app.bind_class(tag,'<Escape>',stop_once)
    def history_key(event,method):
        view=next((v for v in getattr(app,'diagrams',{}).values() if getattr(v,'canvas',None)==event.widget),None)
        if view is not None and getattr(view,'editing',False):return
        getattr(app,method)()
        return 'break'
    app.bind_class(tag,'<Control-z>',lambda event:history_key(event,'undo'))
    app.bind_class(tag,'<Control-y>',lambda event:history_key(event,'redo'))
    def prepare_canvas(event):
        widget=event.widget
        if isinstance(widget,tk.Canvas):
            manager=getattr(app,'windows',None)
            if widget.winfo_toplevel() in (app,getattr(manager,'window',None)):
                tags=widget.bindtags()
                if tag not in tags:widget.bindtags((tag,*tags))
    app.bind_all('<Map>',prepare_canvas,add='+')
    if hasattr(app,'status'):
        app._study_shortcut_hint=ttk.Label(app.status,text=tr('diagram.focus_hint'))
        app._study_shortcut_hint.pack(side='right',padx=6)
        token=app.bus.subscribe('language.changed',lambda **p:app._study_shortcut_hint.configure(text=tr('diagram.focus_hint')))
        app._study_shortcut_hint.bind('<Destroy>',lambda event:app.bus.unsubscribe(token),add='+')
    for sequence,method in STUDY_KEYS.items():
        def invoke(event,method=method):
            try:
                top=event.widget.winfo_toplevel()
                manager=getattr(app,'windows',None)
                diagram=getattr(manager,'window',None)
                if top not in (app,diagram):
                    return
                if method=='stop':
                    stop_once(event)
                elif method=='focus_other':
                    manager.focus_other()
                else:
                    getattr(app,method)()
                return 'break'
            except tk.TclError:
                return
        app._study_shortcut_bindings[sequence]=app.bind_all(sequence,invoke,add='+')
