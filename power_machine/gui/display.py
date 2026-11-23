"""Formato de presentación sin modificar datos ni procedencia de los estudios."""
from pathlib import Path
import re


def short_path(value, root=None):
    """Muestra una ruta sin revelar la carpeta personal del equipo."""
    path = Path(value)
    if root is not None:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return '…/' + '/'.join(str(value).replace('\\', '/').rstrip('/').split('/')[-2:])


def public_text(value):
    """Abrevia rutas absolutas dentro de mensajes, incluidos los de excepciones."""
    text = str(value)
    pattern = r'[A-Za-z]:[\\/][^\n\r\t\"\'<>]*'
    text = re.sub(pattern, lambda m: short_path(m.group().rstrip()), text)
    text = re.sub(r'/(?:home|Users)/[^\s\"\'<>]+', lambda m: short_path(m.group()), text)
    return text


def elapsed_text(seconds):
    """Duración legible con precisión apropiada al orden de magnitud."""
    if seconds is None:
        return ''
    seconds = float(seconds)
    return f'{seconds * 1000:.0f} ms' if seconds < 1 else f'{seconds:.2f} s'
