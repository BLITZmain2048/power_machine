"""Panel DSL independiente. Trabajos serializables, sin registro ni runtime de red."""
from .panel_widgets import action_button
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import uuid
import tkinter as tk
from tkinter import ttk, filedialog

from power_machine.dsl_editor import (template_draft, controller_templates, PreviewCase,
    revise_draft, save_draft, load_draft)
from power_machine.dsl_editor.schema import fingerprint

from ..i18n import tr
from ..theme import theme
from .panel_widgets import finish_panel, element_label, study_panel, ElementSelector, ResultTable, AdvancedOptions, significant, error_result

PANEL_SPEC = {"title_key": 'integration.dsl_models_and_controllers', "id": "dsl_editor", "title": tr('integration.dsl_models_and_controllers'), "api_version": 1,
              "factory": "create_panel", "order": 60}


class PanelController:
    """Control ensayable sin pantalla. Resultados de revisión anterior se descartan."""
    def __init__(self, context, display=None):
        self.context, self.display = context, display
        self.draft = template_draft("first_order")
        self.last_result = None

    def set_draft(self, draft):
        self.draft = deepcopy(draft)
        self.last_result = None

    def submit(self, operation, *, case=None, options=None):
        draft = asdict(self.draft)
        revision_hash = fingerprint(draft)
        data = {"operation": operation, "draft": draft, "draft_sha256": revision_hash}
        if case is not None:
            data["case"] = asdict(case)
            data["case_sha256"] = fingerprint(data["case"])
        request = {"schema": "pm-parity-request-v1", "module": "dsl_editor", "root": str(self.context.root),
            "snapshot": deepcopy(self.context.get_snapshot()), "options": deepcopy(options or {}), "input": data}
        snapshot_hash = fingerprint(request["snapshot"])
        def on_message(message):
            if revision_hash != fingerprint(asdict(self.draft)) or snapshot_hash != fingerprint(self.context.get_snapshot()):
                self.context.log('AVISO', tr('integration.model_study_or_draft_changed_during_the'))
                return
            if isinstance(message, (tuple, list)):
                kind = message[0]
                if kind == "result":
                    message = message[1]
                elif kind == "error":
                    self.context.log("ERROR", str(message[1]))
                    if self.display:
                        self.display(error_result(message[1]))
                    return
                elif kind == "log":
                    self.context.log(message[1], message[2])
                    return
                elif kind == "progress":
                    return
                else:
                    return
            if isinstance(message, dict) and message.get("schema") == "pm-parity-result-v1":
                self.last_result = message
                self.context.publish_result(message)
                if self.display:
                    self.display(message)
        return self.context.submit_job("dsl_editor", request, on_message)


def create_panel(parent, context):
    panel, form, results = study_panel(parent)
    introduction = ttk.Label(form, wraplength=300)
    introduction.pack(anchor="w")
    toolbar = ttk.Frame(form)
    toolbar.pack(fill="x")
    template = tk.StringVar(value="first_order")
    ttk.Combobox(toolbar, textvariable=template, values=[r["id"] for r in controller_templates()], state="readonly", width=24).grid(row=0, column=0, sticky="ew")
    notebook = ttk.Notebook(results)
    notebook.pack(fill="both", expand=True)
    editors = {}
    pages = (("fields", "fields"), ("equations", "equations"), ("parameters", "parameters"),
             ("ports", "ports"), ("case", "case"), ("result", "result"))
    for key, label_key in pages:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=tr("panel.dsl." + label_key))
        text = tk.Text(frame, wrap="none", height=16)
        text.pack(fill="both", expand=True)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        ybar.pack(side="right", fill="y")
        text.configure(yscrollcommand=ybar.set)
        text.tag_configure("dsl_key", foreground=theme.color("accent"), font=theme.font("mono"))
        text.tag_configure("dsl_string", foreground=theme.color("ok"), font=theme.font("mono"))
        text.tag_configure("dsl_number", foreground=theme.color("warn"), font=theme.font("mono"))
        text.tag_configure("dsl_comment", foreground=theme.color("text_muted"), font=theme.font("mono"))
        text.bind("<KeyRelease>", lambda event, widget=text: highlight(widget), add="+")
        editors[key] = text
    def highlight(widget):
        """Resaltado ligero de JSON y ecuaciones sin alterar el texto editado."""
        import re
        widget.tag_remove("dsl_key", "1.0", "end")
        widget.tag_remove("dsl_string", "1.0", "end")
        widget.tag_remove("dsl_number", "1.0", "end")
        widget.tag_remove("dsl_comment", "1.0", "end")
        text = widget.get("1.0", "end-1c")
        for match in re.finditer(r'(?m)^\s*(#.*)$', text):
            widget.tag_add("dsl_comment", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(r'"(?:\\.|[^"\\])*"(?=\s*:)', text):
            widget.tag_add("dsl_key", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(r'"(?:\\.|[^"\\])*"', text):
            widget.tag_add("dsl_string", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(r'(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', text):
            widget.tag_add("dsl_number", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
    def replace_text(key, value):
        editors[key].delete("1.0", "end")
        editors[key].insert("1.0", value)
        highlight(editors[key])
    def display(result):
        panel.show_state(result)
        replace_text("result", json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        notebook.select(editors["result"].master)
    controller = PanelController(context, display)
    def fill():
        draft = controller.draft
        fields = json.loads(draft.source_text)
        replace_text("equations", "\n".join(fields.pop("sAddEquat", [])))
        for key, value in (("fields", fields), ("parameters", draft.parameters), ("ports", draft.ports)):
            replace_text(key, json.dumps(value, ensure_ascii=False, indent=2))
        names = fields.get("sInput", [])
        case = {"time_s": [0., .5, 1.], "inputs": {n: [1., 1., 1.] for n in names}, "initial": {}, "expected": None}
        replace_text("case", json.dumps(case, indent=2))
        replace_text("result", tr("panel.dsl.no_result"))
    def read():
        fields = json.loads(editors["fields"].get("1.0", "end"))
        fields["sAddEquat"] = editors["equations"].get("1.0", "end").splitlines()
        controller.set_draft(revise_draft(controller.draft, fields=fields,
            parameters=json.loads(editors["parameters"].get("1.0", "end")),
            ports=json.loads(editors["ports"].get("1.0", "end"))))
    def action(operation):
        try:
            read()
            case = None
            if operation in ("preview", "compare"):
                from power_machine.dsl_editor.schema import case_from_dict
                case = case_from_dict(json.loads(editors["case"].get("1.0", "end")))
            controller.submit(operation, case=case, options={"step_s": .001, "timeout_s": 30.})
        except (ValueError, KeyError, TypeError) as exc:
            context.log("ERROR", str(exc))
            replace_text("result", str(exc))
    def new():
        controller.set_draft(template_draft(template.get()))
        fill()
    def save():
        try:
            read()
            directory = Path(context.root) / "workspaces/dsl" / controller.draft.draft_id
            path = directory / ("draft_" + uuid.uuid4().hex + ".json")
            save_draft(controller.draft, path)
            context.log("INFO", tr('integration.draft_saved') + str(path))
        except (ValueError, OSError) as exc:
            context.log("ERROR", str(exc))
    def load():
        path = filedialog.askopenfilename(parent=panel, initialdir=Path(context.root)/"workspaces/dsl", filetypes=[(tr('integration.draft_json'), "*.json")])
        if path:
            try:
                controller.set_draft(load_draft(path))
                fill()
            except (ValueError, OSError, KeyError, TypeError) as exc:
                context.log("ERROR", str(exc))
    advanced = AdvancedOptions(form)
    advanced.pack(fill="x")
    buttons = (("new", new), ("open", load), ("save", save), ("compile", lambda: action("compile")),
               ("ir", lambda: action("blocks")), ("preview", lambda: action("preview")),
               ("compare", lambda: action("compare")))
    button_widgets = []
    for index, (label_key, callback) in enumerate(buttons):
        owner = toolbar if index < 4 else advanced.content
        button = ttk.Button(owner, command=callback)
        button.grid(row=1+index//3, column=index%3, sticky="ew")
        button_widgets.append((button, label_key))
    fill()
    panel.controller = controller
    def retranslate():
        introduction.configure(text=tr("panel.dsl.introduction"))
        for frame_id, (_, label_key) in zip(notebook.tabs(), pages):
            notebook.tab(frame_id, text=tr("panel.dsl." + label_key))
        for button, label_key in button_widgets:
            button.configure(text=tr("panel.dsl." + label_key))
        for text in editors.values():
            for tag, token in (("dsl_key", "accent"), ("dsl_string", "ok"), ("dsl_number", "warn"), ("dsl_comment", "text_muted")):
                text.tag_configure(tag, foreground=theme.color(token), font=theme.font("mono"))
            highlight(text)
    gate_button = action_button(panel.actions, 'run', text=tr('integration.run'), command=lambda : action('compile'))
    gate_button.grid(row=1, column=0, sticky="w")
    panel.retranslate = retranslate
    result = finish_panel(panel, context, controller, tr("panel.dsl.scope"))
    retranslate()
    return result
