"""Consola de guiones Python aislada del hilo de la interfaz Tk."""
from __future__ import annotations

import multiprocessing
from pathlib import Path
from queue import Empty
import traceback
from .paths import configure_tk_libraries
configure_tk_libraries()
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from .i18n import tr
from .theme import theme


def _run_script(root: dict, source: str, queue) -> None:
    """Proceso hijo, solo recibe texto que el usuario escribió o abrió con aviso."""
    class QueueWriter:
        def write(self, text):
            if text:
                queue.put(("output", str(text)))
            return len(text)

        def flush(self):
            return None

    try:
        import contextlib
        from power_machine import api
        writer = QueueWriter()
        network = api.Network(root)
        namespace = {"__name__": "__main__", "__file__": "<guion-editor>", "api": api, "net": network}
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            exec(compile(source, "<guion-editor>", "exec"), namespace, namespace)
        queue.put(("done", tr('integration.script_finished')))
    except BaseException:
        queue.put(("error", traceback.format_exc()))


class ScriptingWindow(tk.Toplevel):
    """Editor de guiones con ejecución ``spawn`` y salida recibida por cola."""

    def __init__(self, parent, root_path):
        super().__init__(parent)
        self.root_path = root_path
        self.context = multiprocessing.get_context("spawn")
        self.process = None
        self.queue = None
        self.after_id = None
        self.title(tr('script.title'))
        self.geometry("1000x650")
        self.minsize(700, 450)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()

    def _build(self):
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        self.run_button = ttk.Button(bar, text=tr('script.run'), command=self.run)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text=tr('script.stop'), command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        self.open_button = ttk.Button(bar, text=tr('script.open'), command=self.open_file); self.open_button.pack(side="left", padx=(18, 0))
        self.clear_button = ttk.Button(bar, text=tr('script.clear_output'), command=lambda: self.output.delete("1.0", "end")); self.clear_button.pack(side="right")
        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        editor_frame, output_frame = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(editor_frame, weight=3)
        panes.add(output_frame, weight=2)
        self.editor = tk.Text(editor_frame, wrap="none", undo=True, font=theme.font('mono'))
        self.editor.pack(fill="both", expand=True)
        self.output = tk.Text(output_frame, wrap="word", state="normal", height=12, font=theme.font('mono'))
        self.output.pack(fill="both", expand=True)
        self.editor.insert("1.0", tr('script.template'))

    def open_file(self):
        path = filedialog.askopenfilename(parent=self, title=tr('script.open_title'), filetypes=[(tr('script.python_files'), "*.py"), (tr('script.all_files'), "*.*")])
        if not path:
            return
        if not messagebox.askyesno(tr('script.trust_title'), tr('script.trust_message'), parent=self):
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            messagebox.showerror(tr('script.title'), str(exc), parent=self)
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)

    def append(self, text: str):
        from .display import public_text
        self.output.insert("end", public_text(text))
        self.output.see("end")

    def run(self):
        if self.process is not None:
            return
        self.output.delete("1.0", "end")
        self.queue = self.context.Queue()
        self.process = self.context.Process(target=_run_script, args=(self.root_path, self.editor.get("1.0", "end-1c"), self.queue))
        self.process.start()
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.after_id = self.after(75, self.poll)

    def poll(self):
        self.after_id = None
        terminal = False
        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except Empty:
                break
            self.append(payload if payload.endswith("\n") else payload + "\n")
            terminal = terminal or kind in ("done", "error")
        if self.process is not None and (terminal or not self.process.is_alive()):
            self._cleanup_process()
            return
        self.after_id = self.after(75, self.poll)

    def _cleanup_process(self):
        if self.after_id is not None:
            self.after_cancel(self.after_id)
            self.after_id = None
        if self.process is not None:
            self.process.join(timeout=0.2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
            self.process.close()
            self.process = None
        if self.queue is not None:
            self.queue.close()
            self.queue = None
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def stop(self):
        if self.process is not None:
            self.append(tr('script.stopping') + "\n")
            self.process.terminate()
            self._cleanup_process()

    def close(self):
        self.stop()
        self.destroy()

    def retranslate(self):
        self.title(tr('script.title'))
        self.run_button.configure(text=tr('script.run'))
        self.stop_button.configure(text=tr('script.stop'))
        self.open_button.configure(text=tr('script.open'))
        self.clear_button.configure(text=tr('script.clear_output'))


def open_scripting_window(parent, root_path):
    """Abre la ventana, llamada de integración para el menú de la aplicación."""
    return ScriptingWindow(parent, root_path)


__all__ = ["ScriptingWindow", "open_scripting_window"]
