"""Compatibility facade forwarding the variant panel to the main diagram."""
from tkinter import ttk
from ..i18n import tr


class DiagramPortal:
    """No Canvas is created here. Geometry and editing use DiagramWidget."""
    def __init__(self,parent,controller,**callbacks):
        self.parent,self.controller=parent,controller
        self.on_edit=callbacks.get('on_edit',lambda identity:None)
        self.host=ttk.Frame(parent,padding=20)
        ttk.Label(self.host,text=tr('edit.diagram_hint'),wraplength=700).pack(fill='x',pady=10)
        ttk.Button(self.host,text=tr('edit.open_diagram'),command=self.open_view).pack(anchor='w')
        self.tree=ttk.Treeview(self.host,columns=('name','class','state'),show='headings',height=8)
        for key in ('name','class','state'):
            self.tree.heading(key,text=tr('edit.column.'+key))
        self.tree.pack(fill='both',expand=True,pady=(8,0))
        self.tree.bind('<<TreeviewSelect>>',self.select)
        self.tree.bind('<Double-Button-1>',lambda e:self.on_edit(self.controller.selected_id))

    def open_view(self):
        app=self.parent.winfo_toplevel()
        app.open_diagram()
        return app.diagrams[None]

    def grid(self,**options):self.host.grid(**options)
    def configure(self,**options):
        for key in ('xscrollcommand','yscrollcommand'):
            if key in options:self.tree.configure(**{key:options[key]})
    def bind(self,*args,**kwargs):return self.host.bind(*args,**kwargs)
    def xview(self,*args):return self.tree.xview(*args)
    def yview(self,*args):return self.tree.yview(*args)
    def redraw(self):
        self.tree.delete(*self.tree.get_children())
        items=self.controller.session.list_elements()+list(self.controller.drafts.values())
        for item in items:
            self.tree.insert('','end',iid=item['id'],values=(item.get('name') or tr('edit.draft'),
                item['class'],tr('edit.draft') if item['id'] in self.controller.drafts else
                tr('edit.outserv') if item.get('outserv') else tr('edit.inserv')))
    def select(self,event):
        selected=self.tree.selection()
        if selected:self.controller.select(selected[0])
    def fit(self):self.open_view().fit()
    def zoom(self,factor):
        view=self.open_view()
        view._zoom(view.canvas.winfo_width()/2,view.canvas.winfo_height()/2,factor)
    def navigate(self,group):self.parent.winfo_toplevel().open_diagram(group)
    def begin_port(self,identity,field):
        view=self.open_view()
        view._edit_var.set(True)
        view.toggle_editing()
        view.begin_reconnect(identity,field)
