"""Reusable, compact chrome for the two engineering windows."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from .i18n import tr
from .theme import theme
from .toolbar_icons import icon


class BrandBar(ttk.Frame):
    def __init__(self,parent,window_key,case_variable):
        super().__init__(parent,style='Titlebar.TFrame',padding=(12,5))
        self.window_key=window_key
        self.logo=None
        path=Path(__file__).parent/'assets/logo_256.png'
        if path.is_file():
            try:
                self.logo=tk.PhotoImage(master=self,file=str(path)).subsample(8)
                ttk.Label(self,image=self.logo,style='Titlebar.TLabel').pack(side='left',padx=(0,9))
            except tk.TclError:pass
        ttk.Label(self,text='POWER MACHINE',style='Titlebar.Brand.TLabel').pack(side='left')
        self.caption=ttk.Label(self,style='Titlebar.Window.TLabel',padding=(16,0))
        self.caption.pack(side='left')
        ttk.Label(self,textvariable=case_variable,style='Titlebar.Case.TLabel').pack(side='right',padx=8)
        self.retranslate()

    def retranslate(self):self.caption.configure(text=tr(self.window_key))


class PanelHeader(ttk.Label):
    def __init__(self,parent,key):
        self.key=key
        super().__init__(parent,style='PanelHeader.TLabel')
        self.retranslate()

    def retranslate(self):self.configure(text=tr(self.key).upper())


class MenuBar(ttk.Frame):
    """Themed menu strip backed by the unchanged command registry."""
    def __init__(self,parent,menus):
        super().__init__(parent,style='Toolbar.TFrame',padding=(7,1))
        self.menus=menus
        self.buttons={}
        for key,menu in menus.items():
            button=ttk.Menubutton(self,menu=menu,style='Chrome.TMenubutton',takefocus=True,width=0)
            button.pack(side='left')
            self.buttons[key]=button
            button.bind('<Return>',lambda e,k=key:self.post(k))
            button.bind('<Down>',lambda e,k=key:self.post(k))
        self._bindings=[]
        self.retranslate()
        parent.winfo_toplevel().bind('<F10>',lambda e:self.focus_menu(),add='+')

    def post(self,key):
        button=self.buttons[key]
        button.focus_set()
        self.menus[key].tk_popup(button.winfo_rootx(),button.winfo_rooty()+button.winfo_height())
        return 'break'

    def focus_menu(self):
        next(iter(self.buttons.values())).focus_set()
        return 'break'

    def retranslate(self):
        from .i18n import language
        top=self.winfo_toplevel()
        for sequence,ident in self._bindings:top.unbind(sequence,ident)
        self._bindings=[]
        keys=dict(zip(self.buttons,'aevcsghny' if language()=='es' else 'fevcrptwh'))
        for key,button in self.buttons.items():
            title=tr('menu.'+key)
            letter=keys[key]
            button.configure(text=title,underline=title.casefold().find(letter))
            sequence='<Alt-'+letter+'>'
            ident=top.bind(sequence,lambda e,k=key:self.post(k),add='+')
            self._bindings.append((sequence,ident))


class CompactToolbar(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent,style='Toolbar.TFrame',padding=(8,4))
        self.buttons=[]
        self.images=[]

    def action(self,key,command,number,*,shortcut='',run=False):
        from .app_shell import Tooltip
        button=ttk.Button(self,text=tr(key),command=command,compound='left',
                          style='Run.TButton' if run else 'Toolbar.TButton')
        button.action_id=number
        button.pack(side='left',padx=2)
        self.buttons.append((button,key,shortcut))
        Tooltip(button,lambda:tr(key)+(' · '+shortcut if shortcut else ''))
        self.refresh_icons()
        return button

    def separator(self):ttk.Separator(self,orient='vertical').pack(side='left',fill='y',padx=7,pady=3)

    def refresh_icons(self):
        images=[]
        for button,_,_ in self.buttons:
            states={s:icon(self,button.action_id,s,size=20) for s in ('normal','active','disabled')}
            images.extend(states.values())
            button.configure(image=(states['normal'],'disabled',states['disabled'],'active',states['active']))
        self.images=images

    def retranslate(self):
        for button,key,_ in self.buttons:button.configure(text=tr(key))


class SegmentedStatus(ttk.Frame):
    """No hard truncation. Full changing messages also appear as tooltips."""
    def __init__(self,parent,*,mode,selection,case,validity=None,evidence=None):
        super().__init__(parent,style='Status.TFrame')
        from .app_shell import Tooltip
        self.labels=[]
        variables=(mode,selection,validity,case) if validity is not None else (mode,selection,case)
        for n,var in enumerate(variables):
            if validity is not None and n==3:
                self.progress=ttk.Progressbar(self,length=65,maximum=100)
                self.progress.pack(side='left',padx=5)
                self.evidence_button=ttk.Button(self,text=tr('int2.evidence'),command=evidence,style='Toolbar.TButton')
                self.evidence_button.pack(side='left',padx=3)
            if n:ttk.Separator(self,orient='vertical').pack(side='left',fill='y',pady=5)
            label=ttk.Label(self,textvariable=var,style='Status.Mode.TLabel' if n==0 else 'Status.TLabel')
            if validity is not None:label.configure(width=(30,18,30,28)[n])
            label.pack(side='left',fill='x',expand=n==0)
            Tooltip(label,var.get)
            self.labels.append(label)


class ElidedLabel(ttk.Label):
    """Fit text to allocated space, exposing the complete value as a tooltip."""
    def __init__(self,parent,variable,**kwargs):
        super().__init__(parent,width=1,**kwargs)
        self.variable=variable
        from .app_shell import Tooltip
        Tooltip(self,variable.get)
        self._trace=variable.trace_add('write',self.update_text)
        self.bind('<Configure>',self.update_text)
        self.bind('<Destroy>',lambda e:variable.trace_remove('write',self._trace) if e.widget is self else None)
    def update_text(self,*args):
        from tkinter.font import Font
        font=Font(self,font=self.cget('font') or theme.font('body'))
        text=self.variable.get()
        limit=max(1,self.winfo_width()-8)
        if font.measure(text)>limit:
            while text and font.measure(text+'…')>limit:text=text[:-1]
            text+='…'
        self.configure(text=text)
