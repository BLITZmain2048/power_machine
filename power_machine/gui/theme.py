"""Semantic light and dark palettes for Tk and matplotlib."""
import weakref
from .bus import bus
LIGHT=dict(bg='#edf1f6',surface='#ffffff',surface_alt='#e4eaf2',border='#c2cddd',text='#162337',text_muted='#53647b',accent='#1768ba',accent_text='#ffffff',ok='#1e7c50',warn='#946000',error='#b73040',selection='#c9dff9',grid='#d5dce7',diagram_bg='#ffffff',bus='#205db0',branch='#53647b',deenergized='#929cab',kv_345='#2464bc',kv_230='#8b4ea6',kv_138='#bf6a15',kv_low='#22836f',v_low='#bd414c',v_ok='#288c66',v_high='#bc7330')
DARK=dict(bg='#151b26',surface='#1e2837',surface_alt='#29364a',border='#40516a',text='#e6edf7',text_muted='#a7b7ce',accent='#67adff',accent_text='#11253e',ok='#65d0a0',warn='#f2c46c',error='#ff8591',selection='#304f78',grid='#39485c',diagram_bg='#192230',bus='#8abaff',branch='#acbed4',deenergized='#637083',kv_345='#8abaff',kv_230='#cea5ff',kv_138='#ffc383',kv_low='#77d9be',v_low='#ff8591',v_ok='#65d0a0',v_high='#f2c46c')
for palette in (LIGHT,DARK):
    for i in range(121):
        palette['load_'+str(i)]=palette['ok'] if i<80 else palette['warn'] if i<100 else palette['error']
# Shell v3 tokens. Electrical palettes above remain stable for existing overlays.
LIGHT.update(bg='#e7e8e9',panel='#f1f2f3',paper='#ffffff',soft='#f7f8f9',
    line='#c9cdd1',ink='#30363d',muted='#65707b',accent='#173653',accent2='#d99a28',
    sky='#56a9d3',select='#fff0d2',grid='#e9edf0',alert='#ad493f',
    titlebar='#353a40',title_ink='#edf0f3',title_muted='#c2c9d1')
DARK.update(bg='#282b2f',panel='#30343a',paper='#22262b',soft='#292e34',
    line='#484e57',ink='#e3e7ec',muted='#a8b0bc',accent='#83bddb',accent2='#e5b45b',
    sky='#83bddb',select='#4c4030',grid='#363d45',alert='#df8e82',
    titlebar='#20252b',title_ink='#edf0f3',title_muted='#bdc7d1')
for palette in (LIGHT,DARK):
    for alias,token in dict(surface='paper',surface_alt='panel',border='line',text='ink',
                            text_muted='muted',selection='select',error='alert',diagram_bg='paper').items():
        palette[alias]=palette[token]
class Theme:
    palettes={'light':LIGHT,'dark':DARK}
    pad_s,pad_m,pad_l=4,8,16
    def __init__(self,name='light'):
        self.name=name
        self._roots=weakref.WeakSet()
    def color(self,token):return self.palettes[self.name][token]
    def font(self,name):
        return {'body':('Segoe UI',10),'small':('Segoe UI',9),'heading':('Segoe UI',11,'bold'),'mono':('Consolas',10)}[name]
    def set(self,name):
        if name not in self.palettes:raise ValueError('Unknown theme: '+str(name))
        self.name=name
        for root in list(self._roots):
            try:self.apply(root)
            except Exception:pass
        bus.publish('theme.changed',name=name)
    def apply(self,root):
        from tkinter import ttk
        self._roots.add(root)
        root.option_add('*Font', self.font('body'))
        style=ttk.Style(root)
        style.theme_use('clam')
        c=self.color
        style.configure('.',background=c('bg'),fieldbackground=c('surface'),troughcolor=c('bg'),
            foreground=c('text'),font=self.font('body'),bordercolor=c('border'),
            lightcolor=c('border'),darkcolor=c('border'),highlightbackground=c('border'))
        for kind in ('TFrame','TLabelframe','TLabelframe.Label','TLabel','TCheckbutton','TRadiobutton','Status.TLabel','TPanedwindow','TSizegrip'):
            style.configure(kind,background=c('bg'),foreground=c('text'))
        style.configure('TLabelframe',bordercolor=c('border'),lightcolor=c('border'),darkcolor=c('border'))
        for kind in ('TCheckbutton','TRadiobutton'):
            style.configure(kind,indicatorbackground=c('surface'),indicatorforeground=c('text'))
            style.map(kind,background=[('disabled',c('bg')),('active',c('selection'))],
                foreground=[('disabled',c('text_muted'))],
                indicatorbackground=[('selected',c('accent')),('!selected',c('surface'))])
        style.configure('TSeparator',background=c('border'))
        style.configure('TButton',background=c('surface_alt'),foreground=c('text'),lightcolor=c('border'),darkcolor=c('border'),bordercolor=c('border'),focuscolor=c('accent'),padding=(10,6))
        style.configure('Compact.TButton', padding=(5,3))
        style.configure('Title.TLabel', font=self.font('heading'))
        style.map('TButton',background=[('pressed',c('selection')),('active',c('selection'))],foreground=[('disabled',c('text_muted'))])
        style.configure('Treeview',background=c('surface'),fieldbackground=c('surface'),foreground=c('text'),rowheight=26)
        style.map('Treeview',background=[('selected',c('selection'))],foreground=[('selected',c('text'))])
        style.configure('Treeview.Heading',background=c('surface_alt'),foreground=c('text'),lightcolor=c('border'),darkcolor=c('border'),padding=7)
        style.configure('TNotebook',background=c('bg'),bordercolor=c('border'),lightcolor=c('border'),darkcolor=c('border'))
        style.configure('TNotebook.Tab',background=c('surface_alt'),foreground=c('text'),lightcolor=c('border'),darkcolor=c('border'),padding=(12,7))
        style.map('TNotebook.Tab',background=[('selected',c('surface')),('active',c('selection'))])
        for kind in ('TEntry','TCombobox','TSpinbox'):
            style.configure(kind,fieldbackground=c('surface'),background=c('surface_alt'),foreground=c('text'),insertcolor=c('text'),arrowcolor=c('text'),bordercolor=c('border'),lightcolor=c('border'),darkcolor=c('border'))
            style.map(kind,fieldbackground=[('readonly',c('surface'))],foreground=[('readonly',c('text'))])
        for orientation in ('Horizontal','Vertical'):
            for kind in ('TScrollbar','TScale','TProgressbar'):
                style.configure(orientation+'.'+kind,background=c('surface_alt'),troughcolor=c('bg'),
                    arrowcolor=c('text'),bordercolor=c('border'),lightcolor=c('border'),darkcolor=c('border'))
            style.map(orientation+'.TScrollbar',background=[('active',c('selection')),('pressed',c('selection'))])
        style.configure('TMenubutton',background=c('surface_alt'),foreground=c('text'),arrowcolor=c('text'),
                        bordercolor=c('border'),lightcolor=c('border'),darkcolor=c('border'))
        for name,token in [('Muted','text_muted'),('Warning','warn'),('Error','error'),('Section','text')]:
            style.configure(name+'.TLabel',foreground=c(token),background=c('bg'))
        for orientation in ('Horizontal','Vertical'):
            style.configure(orientation+'.TScale',background=c('bg'),troughcolor=c('soft'),bordercolor=c('line'),lightcolor=c('line'),darkcolor=c('line'))
            style.map(orientation+'.TScale',background=[('disabled',c('panel')),('active',c('select'))])
        style.configure('Event.Card.TFrame',background=c('soft'),bordercolor=c('line'),relief='solid',borderwidth=1)
        style.configure('Event.Card.TLabel',background=c('soft'),foreground=c('ink'))
        style.configure('Event.Target.TLabel',background=c('soft'),foreground=c('muted'),font=self.font('small'))
        style.configure('Event.Time.TLabel',background=c('soft'),foreground=c('accent2'),font=self.font('body'))
        self._chrome_styles(root,style)
        self._defaults(root)
        # One hook per interpreter, shared by all of its Toplevels. Apply also to
        # widgets created after the theme was selected, without rebuilding panels.
        host=root._root()
        hook=getattr(host,'_power_machine_theme_hook',None)
        if hook is None:
            host._power_machine_theme_hook={'theme':self}
            host.bind_all('<Map>',lambda event:self._mapped(host,event),add='+')
        else:
            hook['theme']=self
        self._native(root)

    def _chrome_styles(self,root,style):
        """Stable extension points for the network and analysis workspaces."""
        import tkinter as tk
        c=self.color
        for prefix,background in (('Titlebar','titlebar'),('Panel','panel'),('Inspector','panel'),
                                   ('Status','bg'),('Toolbar','soft')):
            style.configure(prefix+'.TFrame',background=c(background))
            style.configure(prefix+'.TLabel',background=c(background),foreground=c('title_ink' if prefix=='Titlebar' else 'ink'))
        style.configure('Titlebar.Brand.TLabel',background=c('titlebar'),foreground=c('title_ink'),font=('Segoe UI',11,'bold'))
        style.configure('Titlebar.Window.TLabel',background=c('titlebar'),foreground=c('title_muted'))
        style.configure('Titlebar.Case.TLabel',background=c('titlebar'),foreground=c('accent2'))
        style.configure('PanelHeader.TLabel',background=c('bg'),foreground=c('muted'),font=('Segoe UI',9,'bold'),padding=(10,7))
        style.configure('Inspector.Code.TLabel',background=c('select'),foreground=c('accent'),font=('Segoe UI',13,'bold'),padding=8)
        style.configure('Inspector.Title.TLabel',background=c('panel'),foreground=c('ink'),font=('Segoe UI',10,'bold'))
        style.configure('Inspector.Key.TLabel',background=c('panel'),foreground=c('muted'))
        style.configure('Inspector.Value.TLabel',background=c('panel'),foreground=c('ink'),anchor='e')
        style.configure('Status.TLabel',font=self.font('small'),padding=(8,4))
        style.configure('Status.Mode.TLabel',foreground=c('accent'),font=self.font('small'),padding=(8,4))
        style.configure('Toolbar.TButton',background=c('paper'),padding=(7,3),borderwidth=1)
        style.layout('Chrome.TMenubutton',[('Menubutton.border',{'sticky':'nsew','children':[
            ('Menubutton.padding',{'sticky':'nsew','children':[('Menubutton.label',{'sticky':'w'})]})]})])
        style.configure('Chrome.TMenubutton',background=c('panel'),foreground=c('ink'),padding=(8,4),borderwidth=0,arrowcolor=c('muted'))
        style.map('Chrome.TMenubutton',background=[('active',c('select'))])
        style.configure('Run.TButton',background=c('select'),foreground=c('ink'),bordercolor=c('accent2'),padding=(10,3))
        style.map('Run.TButton',background=[('active',c('accent2')),('pressed',c('accent2'))])
        style.configure('Explorer.Treeview',background=c('panel'),fieldbackground=c('panel'),rowheight=30)
        style.configure('Explorer.Subtitle.Treeview',foreground=c('muted'),font=self.font('small'))
        # Image elements paint a true two-pixel stripe, independent of OS theme.
        host=root._root()
        images=getattr(host,'_chrome_tab_images',{})
        for prefix,edge in (('Doc','top'),('Results','bottom')):
            active=tk.PhotoImage(master=root,width=3,height=5)
            inactive=tk.PhotoImage(master=root,width=3,height=5)
            active.put(c('paper'),to=(0,0,3,5))
            inactive.put(c('panel'),to=(0,0,3,5))
            active.put(c('accent') if prefix=='Doc' else c('accent2'),to=(0,0,3,2) if edge=='top' else (0,3,3,5))
            element=prefix+'.stripe'
            # Reuse Tcl images on live theme changes so the style references stay valid.
            if prefix in images:
                for old,new in zip(images[prefix],(inactive,active)):
                    root.tk.call(str(old),'copy',str(new))
            else:
                images[prefix]=(inactive,active)
                style.element_create(element,'image',inactive,('selected',active),border=(0,2,0,2),sticky='nsew')
            style.layout(prefix+'.TNotebook.Tab',[(element,{'sticky':'nsew','children':[
                ('Notebook.padding',{'side':'top','sticky':'nsew','children':[
                    ('Notebook.label',{'side':'top','sticky':''})]})]})])
            style.configure(prefix+'.TNotebook',background=c('bg'),borderwidth=0,tabmargins=(3,3,0,0))
            style.configure(prefix+'.TNotebook.Tab',padding=(14,8),foreground=c('ink'),background=c('panel'))
            style.map(prefix+'.TNotebook.Tab',foreground=[('selected',c('ink')),('!selected',c('muted'))])
        host._chrome_tab_images=images
        style.layout('Hidden.TNotebook.Tab',[])

    def _defaults(self, root):
        c=self.color
        for option,token in [('background','bg'),('foreground','text'),('highlightBackground','border'),
                             ('highlightColor','accent'),('activeBackground','selection'),('activeForeground','text'),
                             ('selectBackground','selection'),('selectForeground','text'),('insertBackground','text')]:
            root.option_add('*'+option,c(token))
        for kind in ('Text','Listbox','Entry','Spinbox','Menu'):
            root.option_add('*'+kind+'.background',c('surface'))
        for option,token in [('background','surface'),('foreground','text'),('selectBackground','selection'),
                             ('selectForeground','text'),('highlightBackground','border')]:
            root.option_add('*TCombobox*Listbox.'+option,c(token))

    @staticmethod
    def _mapped(host,event):
        import tkinter as tk
        owner=host._power_machine_theme_hook['theme']
        widget=event.widget
        if isinstance(widget,tk.Misc):
            owner._native(widget,recursive=False)
        else:
            # ttk creates its popdown in Tcl, so tkinter has no Python wrapper.
            try:
                if host.tk.call('winfo','class',str(widget))=='Listbox':
                    host.tk.call(str(widget),'configure','-background',owner.color('surface'),
                        '-foreground',owner.color('text'),'-selectbackground',owner.color('selection'),
                        '-selectforeground',owner.color('text'),'-highlightbackground',owner.color('border'))
            except tk.TclError:
                pass

    def _semantic(self,value,default):
        """Carry semantic colors across palettes, replacing factory colors."""
        candidates=('bg','surface','surface_alt','diagram_bg','selection') if default in ('bg','surface','surface_alt','diagram_bg') else ('text','text_muted','accent','ok','warn','error')
        for token in candidates:
            if any(str(value).lower()==palette[token].lower() for palette in self.palettes.values()):
                return self.color(token)
        return self.color(default)
    def title_bar(self, window):
        import sys
        if sys.platform != 'win32': return False
        try:
            import ctypes as ct
            from ctypes import wintypes as wt
            user = ct.windll.user32
            user.GetAncestor.argtypes = [wt.HWND,wt.UINT]
            user.GetAncestor.restype = wt.HWND
            hwnd = user.GetAncestor(window.winfo_id(),2)
            value = ct.c_int(self.name == 'dark')
            function = ct.windll.dwmapi.DwmSetWindowAttribute
            function.argtypes = [wt.HWND,wt.DWORD,ct.c_void_p,wt.DWORD]
            for attribute in (20,19):
                if function(hwnd,attribute,ct.byref(value),ct.sizeof(value)) == 0: return True
        except (AttributeError,OSError,ValueError): pass
        return False

    def _native(self,widget,recursive=True):
        import tkinter as tk
        from tkinter import ttk
        c=self.color
        try:
            if isinstance(widget,ttk.Widget):
                style=ttk.Style(widget)
                name=widget.cget('style') or widget.winfo_class()
                if name.startswith(('Titlebar.','PanelHeader.','Inspector.','Status.')) and 'font' in widget.keys():
                    font=style.lookup(name,'font')
                    if font:widget.configure(font=font)
                for option,token in ([] if name.startswith(('Titlebar.','Toolbar.','Run.','Panel','Doc.','Results.','Explorer.','Inspector.','Status.','Chrome.','Event.')) else [('background','bg'),('fieldbackground','surface'),('foreground','text')]):
                    value=style.lookup(name,option)
                    if value:
                        style.configure(name,**{option:self._semantic(value,token)})
                for option,token in [('background','bg'),('foreground','text')]:
                    if option in widget.keys() and widget.cget(option):
                        widget.configure(**{option:self._semantic(widget.cget(option),token)})
                if isinstance(widget,ttk.Combobox):
                    popup=str(widget)+'.popdown.f.l'
                    if widget.tk.call('winfo','exists',popup):
                        widget.tk.call(popup,'configure','-background',c('surface'),'-foreground',c('text'),
                            '-selectbackground',c('selection'),'-selectforeground',c('text'))
            if isinstance(widget,(tk.Tk,tk.Toplevel)):self.title_bar(widget)
            if isinstance(widget,(tk.Tk,tk.Toplevel,tk.Frame,tk.LabelFrame,tk.PanedWindow)):
                widget.configure(background=c('bg'))
            if isinstance(widget,tk.LabelFrame):widget.configure(foreground=c('text'))
            if isinstance(widget,tk.Canvas):widget.configure(background=c('diagram_bg') if getattr(widget,'_diagram_canvas',False) else c('bg'))
            if isinstance(widget,tk.Menu):widget.configure(background=c('surface'),foreground=c('text'),activebackground=c('selection'),activeforeground=c('text'))
            if isinstance(widget,tk.Listbox):widget.configure(background=c('surface'),foreground=c('text'),selectbackground=c('selection'),selectforeground=c('text'),disabledforeground=c('text_muted'))
            if isinstance(widget,tk.Text):widget.configure(background=c('surface'),foreground=c('text'),insertbackground=c('text'),selectbackground=c('selection'),selectforeground=c('text'),inactiveselectbackground=c('selection'))
            if isinstance(widget,tk.Label):widget.configure(background=c('bg'),foreground=self._semantic(widget.cget('foreground'),'text'))
            if isinstance(widget,(tk.Entry,tk.Spinbox)):
                widget.configure(background=c('surface'),foreground=c('text'),insertbackground=c('text'),
                    readonlybackground=c('surface_alt'),disabledbackground=c('surface_alt'),disabledforeground=c('text_muted'),
                    selectbackground=c('selection'),selectforeground=c('text'))
            if isinstance(widget,tk.Spinbox):widget.configure(buttonbackground=c('surface_alt'))
            if isinstance(widget,(tk.Button,tk.Checkbutton,tk.Radiobutton,tk.Menubutton)):
                widget.configure(background=c('surface_alt'),foreground=c('text'),activebackground=c('selection'),activeforeground=c('text'),disabledforeground=c('text_muted'))
            if isinstance(widget,(tk.Checkbutton,tk.Radiobutton)):widget.configure(selectcolor=c('surface'))
            if isinstance(widget,tk.Scrollbar):widget.configure(background=c('surface_alt'),activebackground=c('selection'),troughcolor=c('bg'))
            for option,token in [('highlightbackground','border'),('highlightcolor','accent')]:
                if option in widget.keys():widget.configure(**{option:c(token)})
        except tk.TclError:pass
        if recursive:
            for child in widget.winfo_children():self._native(child)
    def mpl_rc(self):
        c=self.color
        return {'figure.facecolor':c('surface'),'axes.facecolor':c('surface'),'axes.edgecolor':c('border'),'text.color':c('text'),'axes.labelcolor':c('text'),'xtick.color':c('text_muted'),'ytick.color':c('text_muted'),'grid.color':c('grid')}
theme=Theme()
