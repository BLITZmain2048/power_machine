"""Acciones de benchmark, sin ecuaciones eléctricas en la interfaz."""
from power_machine.gui.i18n import tr
from copy import deepcopy
from pathlib import Path
from queue import Empty
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog
from .document import Document
from .panel_context import PanelContext, cancel_panel_timers

def benchmark_study(api, model, path, root):
    """Estudio independiente, base y unidades heredadas del modelo elegido."""
    relative = Path(path).resolve().relative_to(Path(root).resolve()).as_posix() if path and Path(path).resolve().is_relative_to(Path(root).resolve()) else ''
    return api.StudyCase(name=(tr('integration.ieee_39_base_case') if model.get('meta',{}).get('benchmark')=='ieee39' else model.get('meta',{}).get('name') or (Path(path).parent.name if path else tr('integration.imported_network'))),
                         monitored=api.default_monitored(model),
                         ldf_options={'q_limits': False, 'gui_model_path': relative})


def benchmark_scene(model):
    """Usa el layout determinista existente, sin ventana ni cambios eléctricos."""
    from .diagram.layout import build_scene, diagram_groups
    from power_machine.netedit import new_variant, materialize_variant
    checked = materialize_variant(model, new_variant(model))
    groups = diagram_groups(checked)
    if not groups:
        raise ValueError(tr('integration.the_model_has_no_busbars_for_the'))
    return build_scene(checked, groups[0]['id'] if len(groups) == 1 else None)


class BenchmarkActions:
    def open_native_comparison(self, directory=None):
        """Abre una corrida nativa existente junto a la referencia IEEE 39 incluida."""
        if not self.available():return
        from power_machine.resources import ref_data_available
        if not ref_data_available():
            self.show_error(ValueError(tr("reference_data_not_included")))
            return
        if directory is None:
            directory=filedialog.askdirectory(parent=self,title=tr('integration.native_e39_din_01_run_with_signals'),initialdir=self.root_path)
        if not directory:return
        try:
            import json
            from power_machine.resources import readonly_path
            from .reference_plots import native_comparison
            from .app_plots import EngineeringPlotPage
            summary=json.loads((Path(directory)/'summary.json').read_text(encoding='utf-8-sig'))
            if summary.get('state_count')!=166 or summary.get('trial')!='E39-DIN-01':
                raise ValueError(tr('integration.select_a_native_166_state_run_of'))
            spec,own,reference=native_comparison(directory,readonly_path('cases','ieee39_ref','E39-DIN-01'))
            page=EngineeringPlotPage(self.book,spec,own,reference,source_label='Power Machine',reference_label='REF')
            page.gui_validation_label=tr('integration.compared_with_reference_simulator_selected_channels_1_2')
            self.book.add(page,text=tr('integration.ieee_39_native_rms_reference_simulator'))
            self.book.select(page)
            self.output.log('INFO',tr('integration.native_rms_166_states_e39_din_01'))
            return page
        except Exception as exc:self.show_error(exc)

    def rms_fault_demo(self):
        """Prepara el ensayo clásico publicado, sin cambiar parámetros eléctricos."""
        if not self.available():
            return
        if self.model.get('meta',{}).get('benchmark') != 'ieee39':
            self.output.log('AVISO',tr('integration.this_example_requires_ieee_39_define_events'))
            return
        def configure(study):
            study.name=tr('integration.ieee_39_three_phase_fault_b16')
            study.options.t_end_s=8.0
            study.options.dt_s=.005
            study.events=[self.api.SimEvent(1.0,'short_circuit','B16',{'r_ohm':0.0,'x_ohm':0.0}),
                          self.api.SimEvent(1.1,'clear_short_circuit','B16',{})]
            study.ldf_options['gui_classical_event']='E39-DIN-01'
            generators={g['id']:g for g in self.model['gens']}
            curves=[]
            for ident in ('G32','G39'):
                for variable,unit,axis,label in (('s:phi','deg',0,tr('integration.mean')),('s:xspeed','p.u.',1,'Velocidad')):
                    key=f"{generators[ident]['path']}|{variable}|{unit}"
                    curves.append(dict(key=key,axis=axis,label=f'{ident} · {label}'))
                    if key not in study.monitored:study.monitored.append(key)
            study.plots=[dict(name=tr('integration.three_phase_fault_machine_response'),axes=2,curves=curves,
                show_ref=False,frequency_base_hz=60.,events=[dict(time_s=1.,label=tr('integration.fault_b16')),dict(time_s=1.1,label='Despeje')])]
        self.document.edit(configure)
        self.update_document()
        self.rebuild_plots()
        self.output.log('INFO',tr('integration.classical_rms_three_phase_fault_b16_at'))
        self.start_simulation()

    def choose_benchmark(self):
        path = filedialog.askopenfilename(parent=self, initialdir=self.root_path,
                                          filetypes=[(tr('integration.power_machine_model'), '*.json')])
        if path:
            self.open_ieee39(path)

    def open_ieee39(self, model_path=None):
        """La misma acción admite modelos futuros mediante ruta explícita."""
        if not self.available() or not self.maybe_save():
            return
        try:
            from .case_io import load_case
            network = load_case(model_path or 'ieee39')
            path = self.case_path(model_path or 'ieee39')
            model = network_model(network)
            # Construir geometría antes de reemplazar el documento activo.
            benchmark_scene(model)
            case = benchmark_study(self.api, model, path, self.root_path)
            self.manager.shutdown()
            self.base_model = deepcopy(model)
            self.model, self.model_path = model, path
            self.index = self.api.element_index(model)
            self.output.index = self.index
            self.catalog = self.api.signal_catalog(self.root_path, model=model)
            self.install_document(Document(case))

            self.output.log('INFO', tr('integration.network_loaded_from_hz_no_ref_validation', value0=path, value1=model['meta']['f_nom_hz']))
        except Exception as exc:
            self.show_error(exc)

    def open_benchmark_panel(self):
        return self.open_parity_panel('shortcircuit')

    def case_path(self, case):
        from power_machine.resources import ieee39_root
        if str(case).lower() == 'ieee39':
            return ieee39_root() / 'model.json'
        path = Path(case).resolve()
        if path.is_dir():
            return path / 'model.json' if (path / 'model.json').is_file() else None
        return path

    def import_csv(self):
        folder = filedialog.askdirectory(parent=self, title=tr('integration.import_csv_export'))
        if not folder or not self.available() or not self.maybe_save():
            return
        try:
            from .case_io import import_csv_export
            network = import_csv_export(folder)
            self.activate_network(network_model(network), None)
        except Exception as exc:
            self.show_error(exc)

    def activate_network(self, model, path):
        self.base_model = deepcopy(model)
        self.model = deepcopy(model)
        self.model_path = path
        self.install_document(Document(benchmark_study(self.api, model, path, self.root_path)))

    def save_network(self):
        if not self.available():
            return
        path = filedialog.asksaveasfilename(parent=self, title=tr('integration.save_network'),
            initialdir=self.root_path, defaultextension='.json', initialfile='model.json', filetypes=[(tr('integration.network_json'), '*.json')])
        if path:
            try:
                import json
                from power_machine.resources import confined_output
                path = confined_output(path, self.root_path)
                Path(path).write_text(json.dumps(self.model, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
                self.output.log('INFO', tr('integration.network_saved_in') + str(path))
            except Exception as exc:
                self.show_error(exc)

    def benchmark_cc(self):
        if not self.available():
            return
        self.open_parity_panel('shortcircuit')
        item = self.parity_panels.get('shortcircuit')
        if item is None:
            return
        from power_machine.gui.panels.shortcircuit import PanelController
        from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions
        from tkinter import simpledialog
        bus = simpledialog.askstring(tr('integration.ieee_39_short_circuit'), tr('integration.busbar_id_for_three_phase_fault'),
                                     initialvalue=self.model['buses'][0]['id'], parent=self)
        if not bus:
            return
        supplemental = self.model_path.parent / 'shortcircuit_data.json'
        if not supplemental.exists():
            selected = filedialog.askopenfilename(parent=self, initialdir=self.model_path.parent,
                                                  filetypes=[(tr('integration.short_circuit_data'), '*.json')])
            if not selected:
                return
            supplemental = Path(selected)
        context = item[1]
        try:
            PanelController(context, self.show_benchmark_cc).submit(
                FaultSpec(bus, '3ph'), ShortCircuitOptions('iec60909', 'maximum',
                frequency_hz=float(self.model['meta']['f_nom_hz'])), supplemental)
        except (ValueError, OSError) as exc:
            self.show_error(exc)

    def show_benchmark_cc(self, result):
        if 'progress' in result:
            return
        from power_machine.gui.panels.shortcircuit import result_text
        page = ttk.Frame(self.book, padding=10)
        text = tk.Text(page, wrap='word')
        text.pack(fill='both', expand=True)
        text.insert('end', result_text(result))
        text.configure(state='disabled')
        self.book.add(page, text='IEEE 39 · CC')
        self.book.select(page)

    def run_benchmark_dynamic(self, event):
        if not self.available():
            return
        self.buffer.reset([])
        self._progress_decile = 0
        self.set_busy(True, tr('integration.computing') + event, simulation=True)
        try:
            self.manager.start_benchmark(self.root_path, deepcopy(self.operational), self.model_path,
                                         event, end_s=self.document.study.options.t_end_s,
                                         step_s=self.document.study.options.dt_s)
        except Exception as exc:
            self.set_busy(False)
            self.show_error(exc)



def network_model(network):
    """Obtiene una copia de la red para los paneles del motor."""
    if isinstance(network, dict):
        return deepcopy(network)
    return network.to_model()
