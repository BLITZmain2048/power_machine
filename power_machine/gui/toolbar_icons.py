"""Iconos originales de 24 px, construidos con PhotoImage de Tk."""
import tkinter as tk
from .theme import theme


def icon(master, number, state="normal"):
    image = tk.PhotoImage(master=master, width=26, height=26)
    token = 'text_muted' if state == 'disabled' else 'accent' if state == 'active' else 'text'
    ink = theme.color(token)
    blue, green, red = [theme.color('text_muted' if state == 'disabled' else t) for t in ('accent','ok','error')]
    def box(x, y, xx, yy, color):
        image.put(color, to=(x, y, xx, yy))
    def line(x, y, xx, yy, color=ink, width=2):
        steps = max(abs(xx-x), abs(yy-y), 1)
        for i in range(steps+1):
            a, b = round(x+(xx-x)*i/steps), round(y+(yy-y)*i/steps)
            box(max(0,a), max(0,b), min(26,a+width), min(26,b+width), color)
    def path(points, color=ink):
        for a, b in zip(points, points[1:]):
            line(*a, *b, color)
    if number == 0:  # Hoja con signo más.
        path([(5,3),(16,3),(21,8),(21,23),(5,23),(5,3)])
        line(16,3,16,8)
        line(16,8,21,8)
        line(9,15,17,15,green)
        line(13,11,13,19,green)
    elif number == 1:  # Carpeta abierta.
        path([(2,8),(2,22),(22,22),(24,10),(8,10),(6,14),(3,14)],blue)
        path([(2,8),(3,4),(11,4),(14,8),(22,8)],blue)
    elif number == 2:  # Disquete.
        box(4,3,23,23,blue)
        box(8,3,18,10,theme.color('surface'))
        box(8,15,19,23,theme.color('surface'))
        line(11,18,16,18)
    elif number == 3:  # Flujo entre dos barras.
        line(3,5,3,22,blue,3)
        line(21,5,21,22,blue,3)
        line(5,13,19,13,green)
        path([(14,8),(19,13),(14,18)],green)
    elif number == 4:  # Estado inicial y equilibrio.
        path([(3,18),(8,18),(11,12),(15,12),(18,6),(23,6)],green)
        line(4,23,23,23)
        box(10,10,15,15,green)
    elif number == 5:  # Onda dinámica con botón de marcha.
        path([(2,15),(5,15),(8,5),(11,22),(15,9),(19,15),(24,15)],blue)
        path([(19,2),(24,5),(19,8),(19,2)],green)
    elif number == 6:
        box(5,5,22,22,red)
    elif number == 7:  # Evento en una línea temporal.
        line(3,21,23,21)
        line(12,4,12,22,red)
        path([(12,4),(22,7),(12,11)],red)
    elif number == 8:  # Gráfico nuevo.
        path([(4,3),(4,22),(24,22)])
        path([(7,18),(12,8),(17,15),(23,5)],blue)
        line(19,4,24,4,green)
    elif number == 9:  # Selección de variables.
        for y in (5,12,20):
            line(3,y,23,y)
        for x,y in ((9,5),(18,12),(6,20)):
            box(x-2,y-3,x+3,y+4,blue)
    elif number == 10:  # Rayo de cortocircuito.
        path([(16,2),(6,14),(13,14),(9,24),(23,9),(15,9),(16,2)],red)
    elif number == 11:  # Rama retirada N-1.
        line(2,7,24,7,blue)
        path([(3,20),(10,20),(16,14),(23,14)],blue)
        path([(9,12),(17,22)],red)
        path([(17,12),(9,22)],red)
    else:  # Consola de salida.
        path([(2,4),(24,4),(24,23),(2,23),(2,4)])
        path([(6,10),(10,13),(6,16)],green)
        line(13,17,20,17,green)
    return image
