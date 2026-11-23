"""Panel del editor de variantes con navegación por subestación."""
from power_machine.gui.i18n import tr
from copy import deepcopy
import tkinter as tk
from tkinter import ttk, filedialog
from power_machine.gui.netedit.controller import EditorController
from power_machine.gui.netedit.portal import DiagramPortal
from power_machine.gui.netedit.tools import TOOLS
from power_machine.gui.element_dialog import open_element_dialog
from power_machine.gui.netedit.forms import ElementForm, creation_template
from power_machine.gui.diagram.layout import diagram_groups
from power_machine.netedit.commands import IDENTITY

PANEL_SPEC = {"title_key": 'integration.network_and_variant_editor', 'id': 'network_editor', 'title': tr('integration.network_and_variant_editor'), 'api_version': 1,
              'factory': 'create_panel', 'order': 40}
PALETTE = {key:(group,cls) for key,(group,cls,ports) in TOOLS.items()}


class NetPanel(ttk.Frame):
    def __init__(self, parent, context):
        super().__init__(parent)
        self.context = context
        self.controller = EditorController(context)
        self._selection_bus=getattr(self.winfo_toplevel(),'bus',None)
        self._selection_token=(self._selection_bus.subscribe('selection.changed',self._selection_changed)
                               if self._selection_bus else None)
        self.stale_reason = ''
        self.status = tk.StringVar(self)
        self.group_choice = tk.StringVar(self, tr('integration.overview'))
        nav = ttk.Frame(self)
        nav.pack(fill='x')
        self.groups = ttk.Combobox(nav, textvariable=self.group_choice, state='readonly', width=38)
        self.groups.pack(side='left', padx=3)
        self.groups.bind('<<ComboboxSelected>>', lambda e: self.navigate(self.group_map[self.group_choice.get()]))
        for text, cb in [(tr('edit.overview'), lambda: self.navigate(None)), ('−', lambda: self.canvas.zoom(1/1.2)),
                         ('+', lambda: self.canvas.zoom(1.2)), (tr('integration.fit'), lambda: self.canvas.fit()),
                         (tr('integration.undo'), self.controller.undo), (tr('integration.redo'), self.controller.redo),
                         (tr('integration.variant_load_flow'), lambda: self.controller.submit(run_loadflow=True))]:
            ttk.Button(nav, text=text, command=lambda f=cb: self.guard(f)).pack(side='left', padx=2, pady=3)
        actions = ttk.Frame(self)
        actions.pack(fill='x')
        self.palette = tk.StringVar(self, tr('integration.select'))
        ttk.Label(actions, text=tr('integration.palette')).pack(side='left')
        self.palette_box=ttk.Combobox(actions,textvariable=self.palette,state='readonly',width=19)
        self.palette_box.configure(values=[tr('integration.select')]+list(self.palette_labels()))
        self.palette_box.pack(side='left',padx=3)
        for text, cb in [(tr('integration.create'), self.create), (tr('integration.edit'), self.edit_selected), (tr('integration.complete'), self.complete_selected),
                         (tr('integration.connect'), self.connect_selected)]:
            ttk.Button(actions, text=text, command=lambda f=cb: self.guard(f)).pack(side='left', padx=2)
        more = ttk.Menubutton(actions, text=tr('integration.more_actions'))
        menu = tk.Menu(more, tearoff=False)
        for text, cb in [(tr('integration.disconnect_port'), self.disconnect_selected), (tr('integration.retire'), self.retire_selected),
                         (tr('integration.out_of_service'), self.outserv_selected), (tr('integration.copy_af74f7'), self.controller.copy),
                         (tr('integration.paste_draft'), self.controller.paste), (tr('integration.save_variant'), self.save), (tr('integration.open_variant'), self.open)]:
            menu.add_command(label=text, command=lambda f=cb: self.guard(f))
        more.configure(menu=menu)
        more.pack(side='left', padx=3)
        ttk.Label(self, textvariable=self.status, wraplength=940).pack(fill='x', padx=4)
        self.book = ttk.Notebook(self)
        self.book.pack(fill='both', expand=True)
        body = ttk.Frame(self.book)
        self.book.add(body, text=tr('integration.network_and_ports'))
        self.canvas = DiagramPortal(body, self.controller, on_edit=self.edit, on_port=self.port_menu,
                                   on_create=self.place, on_group=self.navigate)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        v = ttk.Scrollbar(body, command=self.canvas.yview)
        v.grid(row=0, column=1, sticky='ns')
        h = ttk.Scrollbar(body, orient='horizontal', command=self.canvas.xview)
        h.grid(row=1, column=0, sticky='ew')
        self.canvas.configure(xscrollcommand=h.set, yscrollcommand=v.set)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        diff_frame = ttk.Frame(self.book)
        self.book.add(diff_frame, text=tr('integration.variant_differences'))
        self.diff = ttk.Treeview(diff_frame, columns=('name', 'field', 'old', 'new'), show='headings')
        for key, label in [('name', tr('integration.element_49fa9f')), ('field', tr('integration.field')), ('old', tr('integration.base_value')), ('new', tr('integration.new_value'))]:
            self.diff.heading(key, text=label)
            self.diff.column(key, width=180)
        self.diff.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(diff_frame, command=self.diff.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(diff_frame, orient='horizontal', command=self.diff.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        diff_frame.rowconfigure(0, weight=1)
        diff_frame.columnconfigure(0, weight=1)
        self.diff.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.diff.bind('<<TreeviewSelect>>', self.select_difference)
        ttk.Label(self, text=tr('edit.ports_hint'),
                  wraplength=940).pack(fill='x', padx=4)
        self.controller.changed = self.refresh
        self.canvas.bind('<Control-z>', lambda e: self.guard(self.controller.undo))
        self.canvas.bind('<Control-y>', lambda e: self.guard(self.controller.redo))
        self.canvas.bind('<Control-c>', lambda e: self.guard(self.controller.copy))
        self.canvas.bind('<Control-v>', lambda e: self.guard(self.controller.paste))
        self.bind('<Control-z>', lambda e: self.guard(self.controller.undo))
        self.bind('<Control-y>', lambda e: self.guard(self.controller.redo))
        self.bind('<Destroy>', self.cleanup)
        self.refresh()
        self.after_idle(self.canvas.fit)

    def cleanup(self, event):
        if event.widget is self:
            if self._selection_bus:self._selection_bus.unsubscribe(self._selection_token)
            self.controller.close()

    def _selection_changed(self,ids,**payload):
        self.controller.selected_id=ids[0] if ids else None

    def guard(self, callback):
        try:
            return callback()
        except (ValueError, KeyError, OSError, TypeError) as exc:
            self.context.log('ERROR', str(exc))
            self.status.set(str(exc))

    def palette_labels(self):
        return {tr('edit.tool.'+key):key for key in PALETTE}

    def palette_key(self):
        index=self.palette_box.current()-1
        return list(PALETTE)[index] if 0<=index<len(PALETTE) else None

    def retranslate(self):
        selected=max(0,self.palette_box.current())
        self.palette_box.configure(values=[tr('integration.select')]+list(self.palette_labels()))
        self.palette_box.current(selected)
        self.refresh()
        if self.group_choice.get() not in self.group_map:
            self.group_choice.set(tr('integration.overview'))

    def mark_stale(self, reason):
        self.stale_reason = reason
        self.refresh()

    def refresh(self):
        self.group_map = {tr('integration.overview'): None}
        for group in diagram_groups(self.controller.model):
            label = group['name'] + ' · ' + '/'.join(f'{v:g}' for v in group['un_kv_levels']) + ' kV'
            if label in self.group_map:
                label += ' · ' + group['id']
            self.group_map[label] = group['id']
        self.groups.configure(values=list(self.group_map))
        self.canvas.redraw()
        self.diff.delete(*self.diff.get_children())
        self.diff_ids = {}
        for iid, name, field, old, new in self.controller.differences():
            row = self.diff.insert('', 'end', values=(name, field, tr('integration.absent') if old is None else old,
                                                    tr('integration.absent') if new is None else new))
            self.diff_ids[row] = iid
        snapshot = self.controller.session.snapshot()
        self.status.set(tr('integration.revision_drafts_no_pf_validation', value0=snapshot['revision'], value1=len(self.controller.drafts), value2=self.controller.flow_status)
                        + (tr('integration.stale')+self.stale_reason if self.stale_reason else ''))

    def navigate(self, group):
        self.group_choice.set(next((k for k, v in self.group_map.items() if v == group), tr('integration.overview')))
        self.canvas.navigate(group)

    def select_difference(self, event):
        selected = self.diff.selection()
        if selected:
            self.controller.select(self.diff_ids[selected[0]])

    def place(self, x, y):
        if self.palette_key() in PALETTE:
            self.create((x, y))

    def create(self, position=None):
        choice = self.palette_key()
        if choice not in PALETTE:
            self.status.set(tr('integration.choose_an_element_from_the_palette_and'))
            return
        view=self.canvas.open_view()
        view._edit_var.set(True)
        view.toggle_editing()
        view.choose_drawing_tool(choice)

    def edit(self, iid):
        if not iid:
            return
        return open_element_dialog(self,self.controller,iid)

    def edit_selected(self):
        self.edit(self.controller.selected_id)

    def complete_selected(self):
        iid = self.controller.selected_id
        if iid not in self.controller.drafts:
            raise ValueError(tr('integration.select_a_draft'))
        return self.edit(iid)

    def port_menu(self, iid):
        if iid:
            self.controller.select(iid)
            self.connect_selected()

    def port_dialog(self, disconnect=False):
        iid = self.controller.selected_id
        ports = self.controller.ports(iid)
        if not ports:
            raise ValueError(tr('integration.select_an_element_with_ports'))
        win = tk.Toplevel(self)
        win.title(tr('integration.element_port'))
        var = tk.StringVar(win, ports[0]['field'])
        for port in ports:
            ttk.Radiobutton(win, text=port['name']+' ('+port['field']+')', value=port['field'], variable=var).pack(anchor='w', padx=15, pady=5)
        def accept():
            field = var.get()
            win.destroy()
            if disconnect:
                self.guard(lambda: self.controller.reconnect(iid, field))
            else:
                self.canvas.begin_port(iid, field)
                self.status.set(tr('integration.select_the_destination_busbar_escape_cancels'))
        ttk.Button(win, text=tr('integration.accept'), command=accept).pack(pady=8)

    def connect_selected(self):
        self.port_dialog()

    def disconnect_selected(self):
        self.port_dialog(True)

    def retire_selected(self):
        if self.controller.selected_id:
            self.controller.retire(self.controller.selected_id)

    def outserv_selected(self):
        if self.controller.selected_id:
            self.controller.edit(self.controller.selected_id, {'outserv': 1})

    def save(self):
        path = filedialog.asksaveasfilename(parent=self, initialdir=self.context.root, defaultextension='.json')
        if path:
            self.controller.save(path)

    def open(self):
        path = filedialog.askopenfilename(parent=self, initialdir=self.context.root, filetypes=[(tr('integration.power_machine_variant'), '*.json')])
        if path:
            self.controller.open(path)


def create_panel(parent, context):
    return NetPanel(parent, context)
