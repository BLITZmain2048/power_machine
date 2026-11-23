"""Hospeda el editor compartido que publica G-EDIT, sin duplicar formularios."""
from tkinter import ttk
from .i18n import tr


class PropertiesDock(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,padding=8,width=300)
        self.app=app
        self.editor=None
        self.heading=ttk.Label(self,text=tr('tools.properties'))
        self.heading.pack(fill='x')
        self.body=ttk.Frame(self)
        self.body.pack(fill='both',expand=True)
        ttk.Label(self.body,text=tr('tools.select_element'),wraplength=260).pack(fill='x')
    def show_element(self,ident):
        for widget in self.body.winfo_children():widget.destroy()
        try:
            from .element_dialog import ElementEditor
            controller=editor_controller(self.app)
            self.editor=ElementEditor(self.body,controller,ident,on_applied=self.applied)
            from .element_dialog import element_title
            self.heading.configure(text=element_title(self.editor.item,controller.model),wraplength=280)
            self.editor.pack(fill='both',expand=True)
            self.editor.apply_button.master.pack_configure(side='bottom',before=self.editor.notebook)
            if self.editor.item.get('class')=='ElmLod':
                self.editor.notebook.select(self.editor.tabs['loadflow'])
        except ImportError:
            ttk.Label(self.body,text=tr('tools.editor_pending'),wraplength=260).pack(fill='x')
    def applied(self,*args,**kwargs):
        self.app.network_changed()
        self.app._tools_network_revision=(id(self.app.document),self.app.document.revision)
        self.app.state_text.set(tr('tools.results_stale'))
    def retranslate(self):
        from .element_dialog import element_title
        self.heading.configure(text=element_title(self.editor.item,self.editor.controller.model) if self.editor else tr('tools.properties'))

def editor_controller(app):
    from .netedit.integration import get_editor_controller
    return get_editor_controller(app)

def ensure_session(app):
    session=app.document.network
    if session.state is None:
        from power_machine.netedit import new_variant
        from power_machine.netedit.schema import variant_to_dict
        session.load_data(app.model,dict(schema='power_machine-gui-network-v1',
                                        variant=variant_to_dict(new_variant(app.model)),drafts={}))
    return session
