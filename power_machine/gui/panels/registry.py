"""Registro panel integration de fábricas autónomas. No crea ventanas ni importa Application."""
from dataclasses import dataclass
from importlib import import_module

from ..i18n import tr


PANEL_MODULES = tuple("power_machine.gui.panels." + name for name in
                      ("shortcircuit", "contingency", "modal", "network_editor", "protection", "dsl_editor",
                       "calc_pvqv", "calc_sens", "calc_ccbar", "calc_cct", "calc_met", "calc_qds", "calc_lfctl"))
JOB_MODULES = {name: "power_machine." + name for name in
               ("shortcircuit", "contingency", "modal", "netedit", "protection", "dsl_editor",
                "voltage_stability", "sensitivities", "sc_sweep", "cct", "dyn_metrics", "qds_panel", "lf_controls")}
JOB_MODULES['modal'] = 'power_machine.gui.panels.modal'


@dataclass(frozen=True)
class PanelEntry:
    id: str
    title: str
    order: int
    factory: object
    title_key: str | None = None

    def display_title(self):
        return tr(self.title_key) if self.title_key else self.title


class PanelRegistry:
    """Un descriptor defectuoso no impide cargar los restantes."""
    def __init__(self, modules=PANEL_MODULES, *, importer=import_module):
        self.entries = {}
        self.errors = {}
        self.unavailable = {}
        for name in modules:
            try:
                module = importer(name)
                spec = module.PANEL_SPEC
                if spec.get("api_version") != 1:
                    raise ValueError(tr('integration.incompatible_panel_api_version'))
                ident, title, order = spec["id"], spec["title"], spec["order"]
                title_key = spec.get("title_key")
                if title_key:
                    title = tr(title_key)
                if not isinstance(ident, str) or not ident or ident in self.entries:
                    raise ValueError(tr('integration.missing_or_duplicate_panel_id'))
                if not isinstance(title, str) or not title or type(order) is not int:
                    raise ValueError(tr('integration.invalid_panel_title_or_order'))
                factory = getattr(module, spec["factory"])
                if not callable(factory):
                    raise ValueError(tr('integration.panel_factory_is_not_callable'))
                self.entries[ident] = PanelEntry(ident, title, order, factory, title_key)
            except ModuleNotFoundError as exc:
                if name.rsplit(".", 1)[-1].startswith("calc_") and (exc.name == name or exc.name in JOB_MODULES.values()):
                    self.unavailable[name] = tr('integration.integration_pending')
                else:
                    self.errors[name] = str(exc)
            except Exception as exc:
                self.errors[name] = str(exc)

    def ordered(self):
        return sorted(self.entries.values(), key=lambda entry: (entry.order, entry.id))

    def create(self, panel_id, parent, context):
        return self.entries[panel_id].factory(parent, context)


def job_entrypoint(module_id):
    """Solo los seis motores declarados pueden ejecutarse desde un panel."""
    return import_module(JOB_MODULES[module_id]).execute_request
