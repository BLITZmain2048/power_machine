"""Cinta de acciones con etiquetas y grupos que caben en una ventana compacta."""
import tkinter as tk
from tkinter import ttk
from .i18n import tr, set_language, language
from .theme import theme
from .toolbar_icons import icon


def edit_network(app):
    app.open_diagram()
    view = app.diagrams.get(None)
    if hasattr(view, 'set_edit_mode'):
        view.set_edit_mode(True)
    elif hasattr(view, 'edit_mode'):
        view.edit_mode.set(True)
    elif hasattr(view, '_edit_var'):
        view._edit_var.set(True)
        view.toggle_editing()


def build_ribbon(app):
    from .app_shell import Tooltip
    from .load_tools import open_load_tools
    app.menus['edit'].add_command(label=tr('tools.scale_loads'),command=lambda:open_load_tools(app))
    header=ttk.Frame(app,padding=(10,3))
    header.pack(fill='x')
    from pathlib import Path
    logo=Path(__file__).parent/'assets/logo_256.png'
    if logo.is_file():
        try:
            app._header_logo=tk.PhotoImage(master=app,file=str(logo)).subsample(8,8)
            ttk.Label(header,image=app._header_logo).pack(side='left',padx=(0,8))
        except tk.TclError:pass
    ttk.Label(header,text='Power Machine',font=theme.font('heading')).pack(side='left')
    ttk.Button(header,text=tr('study.manager'),command=app.study_selector).pack(side='right',padx=4)
    ttk.Button(header,text=tr('study.run_history'),command=app.run_history_dialog).pack(side='right',padx=4)
    book=ttk.Notebook(app)
    book.pack(fill='x')
    groups=[('file',[('shell.new',app.new_case,0,'Ctrl+N'),('shell.open',app.open_case,1,'Ctrl+O'),('shell.save',app.save_case,2,'Ctrl+S')]),
      ('edit',[('shell.undo',app.undo,0,'Ctrl+Z'),('shell.redo',app.redo,0,'Ctrl+Y'),('tools.edit_network',lambda:edit_network(app),7,''),('tools.data_short',app.open_data_table,9,''),('tools.scale_loads',lambda:open_load_tools(app),9,'')]),
      ('calculation',[(key,lambda p=p:app.open_parity_panel(p),n,'') for key,p,n in [
          ('shell.short_circuit','shortcircuit',10),('shell.contingencies','contingency',11),
          ('integration.small_signal_analysis','modal',8),('tools.cct','calc_cct',8),('tools.qds','calc_qds',8),('tools.pvqv','calc_pvqv',8),('tools.sensitivity','calc_sens',8)]]+
          [('shell.load_flow',app.run_loadflow,3,'F10'),('shell.rms_simulation',app.simulation_dialog,5,'F12'),('shell.stop',app.stop,6,'Esc')]),
      ('results',[('tools.tables',app.show_results,9,''),('integration.plots',app.new_plot,8,''),('tools.compare_pf',app.open_native_comparison,8,''),('study.generate_report',app.generate_report_dialog,2,'')]),
      ('window',[('tools.diagram',app.open_diagram,7,'F9'),('tools.theme',lambda:theme.set('dark' if theme.name=='light' else 'light'),9,''),('tools.language',lambda:set_language('es' if language()=='en' else 'en'),9,'')])]
    app.action_buttons,app.toolbar_images=[],[]
    captions=[]
    for group,actions in groups:
        page=ttk.Frame(book,padding=4)
        book.add(page,text=tr('tools.group.'+group))
        for pos,(key,command,n,shortcut) in enumerate(actions):
            picture=icon(app,n)
            app.toolbar_images.append(picture)
            button=ttk.Button(page,image=picture,text=tr(key),compound='left',command=command)
            button.grid(row=pos//5,column=pos%5,padx=3,pady=2,sticky='ew')
            page.columnconfigure(pos%5,weight=1)
            button.action_id=n
            button.action_label=tr(key)
            captions.append((button,key))
            Tooltip(button,lambda k=key,s=shortcut:tr(k)+(' · '+s if s else ''))
            if n==6:app.stop_button=button;button.state(['disabled'])
            else:app.action_buttons.append(button)
        page.group_key=group
    def retranslate():
        for button,key in captions:button.configure(text=tr(key))
        for tab in book.tabs():book.tab(tab,text=tr('tools.group.'+book.nametowidget(tab).group_key))
    book.retranslate=retranslate
    compact=ttk.Frame(app,padding=4)
    app.ribbon_menus=[]
    for group,actions in groups:
        button=ttk.Menubutton(compact,text=tr('tools.group.'+group))
        menu=tk.Menu(button,tearoff=False)
        for key,command,n,shortcut in actions:
            menu.add_command(label=tr(key),accelerator=shortcut,command=command)
        button.configure(menu=menu)
        button.pack(side='left',padx=5)
        app.ribbon_menus.append((button,menu,group,actions))
    old_translate=book.retranslate
    def translate_all():
        old_translate()
        for button,menu,group,actions in app.ribbon_menus:
            button.configure(text=tr('tools.group.'+group))
            for index,(key,command,n,shortcut) in enumerate(actions):
                menu.entryconfigure(index,label=tr(key))
    book.retranslate=translate_all
    mode=[None]
    def responsive(event):
        if event.widget is not app:return
        narrow=event.width<1500
        if mode[0]==narrow:return
        mode[0]=narrow
        if narrow:
            book.pack_forget()
            compact.pack(fill='x',after=header)
        else:
            compact.pack_forget()
            book.pack(fill='x',after=header)
        if hasattr(app,'status_fields'):
            for label in app.status_fields[3:5]:
                if narrow:label.pack_forget()
                elif not label.winfo_manager():label.pack(side='left')
    app.bind('<Configure>',responsive,add='+')
    app.ribbon_compact=compact
    app.ribbon=book
    app.toolbar=book
    return book
