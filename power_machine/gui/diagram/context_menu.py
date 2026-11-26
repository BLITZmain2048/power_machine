"""Theme-aware contextual action list with mouse and keyboard navigation."""
import tkinter as tk
from tkinter import ttk
from ..theme import theme


class ElementContextMenu(tk.Toplevel):
    def __init__(self,parent,title):
        super().__init__(parent,background=theme.color('border'))
        self.withdraw()
        self.overrideredirect(True)
        self.buttons=[]
        self.body=ttk.Frame(self,padding=4)
        self.body.pack(fill='both',expand=True,padx=1,pady=1)
        ttk.Label(self.body,text=title,font=theme.font('heading')).pack(fill='x',padx=6,pady=4)
        self.bind('<Escape>',lambda e:self.destroy())
        self.bind('<Up>',lambda e:self.step(-1))
        self.bind('<Down>',lambda e:self.step(1))
        self.bind('<ButtonPress-1>',self.outside,add='+')

    def add_command(self,*,label,command,state='normal'):
        def apply():
            self.destroy()
            command()
        button=ttk.Button(self.body,text=label,command=apply,state=state,padding=(8,3))
        button.pack(fill='x')
        self.buttons.append(button)

    def add_separator(self):ttk.Separator(self.body).pack(fill='x',pady=3)

    def tk_popup(self,x,y):
        self.update_idletasks()
        # A branch has two data sections, so keep actions reachable on 720p.
        x=max(0,min(round(x),self.winfo_screenwidth()-self.winfo_reqwidth()-8))
        y=max(0,min(round(y),self.winfo_screenheight()-self.winfo_reqheight()-8))
        self.geometry(f'+{x}+{y}')
        self.deiconify()
        self.lift()
        self.grab_set()
        enabled=[b for b in self.buttons if 'disabled' not in b.state()]
        if enabled:enabled[0].focus_force()

    def step(self,direction):
        enabled=[b for b in self.buttons if 'disabled' not in b.state()]
        if enabled:
            focused=self.focus_get()
            index=enabled.index(focused) if focused in enabled else 0
            enabled[(index+direction)%len(enabled)].focus_set()
        return 'break'

    def outside(self,event):
        if not (self.winfo_rootx()<=event.x_root<=self.winfo_rootx()+self.winfo_width() and
                self.winfo_rooty()<=event.y_root<=self.winfo_rooty()+self.winfo_height()):
            self.destroy()
