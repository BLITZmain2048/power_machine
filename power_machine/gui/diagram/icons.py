"""Original compact diagram icons and keyboard-accessible tooltips."""
import tkinter as tk
from ..theme import theme


def icon(master, name, active=False):
    image = tk.PhotoImage(master=master, width=24, height=24)
    ink = theme.color('accent' if active else 'text')
    def line(a, b):
        steps = max(abs(b[0]-a[0]), abs(b[1]-a[1]), 1)
        for i in range(steps+1):
            x,y = (round(a[j]+(b[j]-a[j])*i/steps) for j in (0,1))
            image.put(ink,to=(x,y,min(24,x+2),min(24,y+2)))
    def path(*points):
        for a,b in zip(points,points[1:]): line(a,b)
    def box(x,y,w,h): path((x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y))
    if name=='select': path((5,3),(5,20),(10,15),(14,22),(17,20),(13,13),(21,13),(5,3))
    elif name=='move':
        path((12,2),(12,22)); path((2,12),(22,12))
        for points in [((8,6),(12,2),(16,6)),((8,18),(12,22),(16,18)),((6,8),(2,12),(6,16)),((18,8),(22,12),(18,16))]: path(*points)
    elif name=='pan': path((6,12),(6,7),(9,7),(9,3),(12,3),(12,7),(15,7),(15,5),(18,5),(18,10),(21,10),(21,16),(17,22),(9,22),(3,15),(3,12),(6,12))
    elif name=='fit':
        for points in [((3,9),(3,3),(9,3)),((15,3),(21,3),(21,9)),((3,15),(3,21),(9,21)),((15,21),(21,21),(21,15))]: path(*points)
    elif name=='save': box(3,3,18,18); box(7,3,10,6); box(7,14,10,7)
    elif name=='minimap': box(2,4,20,16); path((4,15),(9,9),(13,13),(18,7)); box(12,5,8,7)
    elif name=='detail': box(2,3,20,18); line((15,3),(15,21)); line((17,8),(20,8)); line((17,12),(20,12))
    elif name=='layers':
        for y in (4,9,14): path((2,y+3),(12,y),(22,y+3),(12,y+6),(2,y+3))
    elif name=='search':
        path((5,3),(12,3),(16,7),(16,13),(12,17),(5,17),(2,13),(2,7),(5,3)); line((15,16),(22,23))
    elif name=='legend':
        for y in (4,11,18): box(2,y,4,3); line((10,y+1),(21,y+1))
    elif name=='result_labels':
        box(3,3,18,14)
        path((7,17),(7,22),(12,17))
        for y in (7,12):line((7,y),(17,y))
    return image


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.job = self.window = None
        for event in ('<Enter>', '<FocusIn>'): widget.bind(event, self.schedule, add='+')
        for event in ('<Leave>', '<FocusOut>', '<ButtonPress>', '<Destroy>'): widget.bind(event, self.hide, add='+')

    def schedule(self, event=None):
        self.hide()
        self.job = self.widget.after(400, self.show)

    def show(self):
        self.job = None
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.geometry(f'+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty()+self.widget.winfo_height()+4}')
        tk.Label(self.window,text=self.text,background=theme.color('surface'),foreground=theme.color('text'),
            font=theme.font('small'),relief='solid',borderwidth=1,padx=6,pady=4).pack()

    def hide(self, event=None):
        if self.job is not None:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.window is not None:
            self.window.destroy()
            self.window = None
