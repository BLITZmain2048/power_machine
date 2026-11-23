"""Acciones de presentación conectadas a las APIs de diagramas y gráficos."""
from copy import deepcopy
import math
from types import SimpleNamespace
from .i18n import tr
from .theme import theme
import tkinter as tk
from tkinter import ttk, filedialog
from .plots import PlotPage, axis_scale


class ViewActions:
    def connect_view_menus(self):
        view=self.menus['view']
        view.add_command(label=tr('shell.data_manager_table_and_filters'),command=self.open_data_table)
        colors=tk.Menu(view,tearoff=False)
        view.add_cascade(label=tr('shell.single_line_diagram_colour'),menu=colors)
        for mode,title in [('voltage',tr('shell.nominal_voltage_kv')),('voltage_band',tr('shell.voltage_band_p_u')),('loading',tr('shell.loading')),('state',tr('shell.operating_state'))]:
            colors.add_command(label=title,command=lambda mode=mode:self.diagram_color(mode))
        view.add_command(label=tr('shell.voltage_band_c0f07f'),command=self.voltage_band_dialog)
        view.add_command(label=tr('shell.result_box_format_2514f2'),command=self.result_format_dialog)
        view.add_command(label=tr('shell.detail_by_zoom'),command=self.detail_zoom_dialog)
        view.add_command(label=tr('shell.find_and_centre_004b11'),command=self.find_element_dialog,accelerator='Ctrl+F')
        view.add_command(label=tr('shell.single_line_diagram_legend_c014d7'),command=self.legend_dialog)
        view.add_checkbutton(label=tr('shell.output_window'),variable=self.output_visible,command=self.toggle_output_from_menu)
        graphs=self.menus['plots']
        graphs.add_separator()
        for title,action in [(tr('shell.x_y_plot_3e9575'),self.xy_dialog),(tr('shell.axis_scales_f09886'),self.axis_scale_dialog),
            (tr('shell.absolute_frequency_or_deviation'),self.frequency_dialog),(tr('shell.compare_runs'),self.choose_runs),
            (tr('shell.measurement_cursors_8006f5'),self.cursor_dialog),(tr('shell.export_pdf'),lambda:self.plot_export('pdf')),
            (tr('shell.export_png'),lambda:self.plot_export('png')),(tr('shell.export_csv'),lambda:self.plot_export('csv'))]:
            graphs.add_command(label=title,command=action)
        tools=self.menus['tools']
        for ident,title in [('network_editor',tr('shell.network_editor')),('dsl_editor',tr('shell.dsl_editor'))]:
            tools.add_command(label=title,command=lambda ident=ident:self.open_parity_panel(ident))
        tools.add_command(label=tr('shell.saved_results_and_comparison'),command=self.choose_runs)
        windows=self.menus['window']
        windows.add_command(label=tr('shell.next_page'),command=lambda:self.next_page(1),accelerator='Ctrl+Tab')
        windows.add_command(label=tr('shell.previous_page'),command=lambda:self.next_page(-1),accelerator=tr('integration.ctrl_shift_tab'))
        windows.add_command(label=tr('shell.close_current_page'),command=self.close_tab)
        windows.add_command(label=tr('shell.page_list'),command=self.show_page_list)
        windows.add_command(label=tr('shell.reset_layout'),command=self.reset_workspace_layout)
        self.menus['help'].add_command(label=tr('shell.latest_load_flow_island_details'),command=self.show_island_details)
        self.bind('<Control-f>',lambda event:self.find_element_dialog())

    def reset_output_height(self):
        if self.output_visible.get():
            height=self.vertical.winfo_height()
            self.vertical.sashpos(0,max(365,int(height*.78)))

    def toggle_output_from_menu(self):
        self.set_output_visible(self.output_visible.get())

    def toggle_output(self):
        self.set_output_visible(not self.output_visible.get())

    def set_output_visible(self,visible):
        self.output_visible.set(visible)
        panes=self.vertical.panes()
        if visible and str(self.output) not in panes:
            self.vertical.add(self.output,weight=1)
            self.vertical.update_idletasks()
            self.reset_output_height()
        elif not visible and str(self.output) in panes:
            self.vertical.forget(self.output)

    def close_page(self,page):
        if str(self.diagrams.get(None)) == str(page):
            return
        self.book.select(page)
        self.close_tab()
        self.book.sync()

    def next_page(self,direction):
        tabs=self.book.tabs()
        if tabs:
            index=tabs.index(self.book.select()) if self.book.select() in tabs else 0
            self.book.select(tabs[(index+direction)%len(tabs)])
        return 'break'

    def show_page_list(self):
        self.book.list_menu.tk_popup(self.book.bar.winfo_rootx()+20,self.book.bar.winfo_rooty()+25)

    def diagram_page(self):
        host = self.windows._dock_book if self.windows.docked else (self.windows.window.book if self.windows.window else None)
        if host is not None and host.winfo_exists() and host.select():
            return host.nametowidget(host.select())
        self.open_diagram()
        return self.diagrams.get(None)

    def plot_page(self):
        current=self.current_tab()
        if isinstance(current,PlotPage):
            return current
        if self.plot_pages:
            self.book.select(self.plot_pages[0])
            return self.plot_pages[0]
        self.output.log('AVISO',tr('shell.open_a_plot_page_to_use_this'))
        return None

    def form(self,title,fields,apply):
        window=tk.Toplevel(self)
        window.title(title)
        window.transient(self)
        body=ttk.Frame(window,padding=14)
        body.pack(fill='both',expand=True)
        window.fields={}
        for row,(key,label,value,choices) in enumerate(fields):
            ttk.Label(body,text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=5)
            variable=tk.StringVar(window,value=str(value))
            window.fields[key]=variable
            widget=ttk.Combobox(body,textvariable=variable,values=choices,state='readonly',width=45) if choices else ttk.Entry(body,textvariable=variable,width=45)
            widget.grid(row=row,column=1,sticky='ew',pady=5)
        error=tk.StringVar(window)
        ttk.Label(body,textvariable=error,foreground=theme.color('error'),wraplength=520).grid(row=len(fields),column=0,columnspan=2,sticky='w')
        def execute():
            try:
                apply({key:value.get() for key,value in window.fields.items()})
                window.destroy()
            except (ValueError,KeyError,TypeError) as exc:
                error.set(str(exc))
        window.apply_button=ttk.Button(body,text=tr('shell.apply'),command=execute)
        window.apply_button.grid(row=len(fields)+1,column=1,sticky='e',pady=(12,0))
        ttk.Button(body,text=tr('shell.cancel'),command=window.destroy).grid(row=len(fields)+1,column=0,sticky='w')
        window.error=error
        return window

    def diagram_color(self,mode):
        page=self.diagram_page()
        if page:
            page.set_color_mode(mode)

    def voltage_band_dialog(self):
        page=self.diagram_page()
        if page is None:return
        low,high=page.voltage_limits
        def apply(fields):
            values=tuple(float(fields[k]) for k in ('low','high'))
            if not all(math.isfinite(v) for v in values) or not 0<values[0]<values[1]:
                raise ValueError(tr('shell.the_band_must_be_finite_positive_and'))
            page.set_color_mode('voltage_band',voltage_limits=values)
        return self.form(tr('shell.voltage_band'),[('low',tr('shell.minimum_p_u'),low,None),('high',tr('shell.maximum_p_u'),high,None)],apply)

    def result_format_dialog(self):
        page=self.diagram_page()
        if page is None:return
        names=[('u_kv',tr('shell.voltage_kv')),('u_pu',tr('shell.voltage_p_u')),('angle_deg',tr('shell.angle')),
            ('p_from_mw',tr('shell.sending_end_p_mw')),('p_to_mw',tr('shell.receiving_end_p_mw')),
            ('q_from_mvar',tr('shell.sending_end_q_mvar')),('q_to_mvar',tr('shell.receiving_end_q_mvar')),('loading_pct',tr('shell.loading'))]
        def apply(fields):
            digits={key:int(value) for key,value in fields.items()}
            if any(not 0<=value<=6 for value in digits.values()):raise ValueError(tr('shell.use_between_zero_and_six_decimal_places'))
            page.set_result_format(digits)
        return self.form(tr('shell.result_box_format'),[(key,label+tr('shell.decimal_places'),2,None) for key,label in names],apply)

    def detail_zoom_dialog(self):
        page=self.diagram_page()
        if page is None:return
        def apply(fields):
            value=float(fields['zoom'])
            if not math.isfinite(value) or value<=0:raise ValueError(tr('shell.zoom_must_be_positive_and_finite'))
            page.set_detail_zoom(value/100)
        return self.form(tr('shell.single_line_diagram_detail'),[('zoom',tr('shell.detail_threshold'),65,None)],apply)

    def find_element_dialog(self):
        page=self.diagram_page()
        if page is None:return
        def apply(fields):
            query=fields['query'].strip().casefold()
            if not query:raise ValueError(tr('shell.enter_a_name_or_identifier'))
            matches=[item for item in self.index.values() if query in (item.get('name','')+' '+item['id']).casefold()]
            if not matches:
                ident=page.find_element(fields['query'])
                if ident is None:raise ValueError(tr('shell.no_matches_in_the_model'))
                return
            matches.sort(key=lambda item:(item.get('name','').casefold()!=query,item.get('class')!='ElmTerm',item['id']))
            ident=matches[0]['id']
            target=page
            if not target.center_on(ident):
                from .diagram import diagram_groups
                item=matches[0]
                bus=item['id'] if item.get('class')=='ElmTerm' else item.get('bus',item.get('bus_from',item.get('bus_hv')))
                group=next((group for group in diagram_groups(self.model) if bus in group['bus_ids']),None)
                if group:
                    self.open_diagram(group['id'])
                    target=self.diagrams[group['id']]
                    self.update_idletasks()
                    target.center_on(ident)
            self.select(ident)
        return self.form(tr('shell.find_and_centre'),[('query',tr('shell.pf_name_or_identifier'),'',None)],apply)

    def legend_dialog(self):
        page=self.diagram_page()
        if page is None:return
        window=tk.Toplevel(self)
        window.title(tr('shell.single_line_diagram_legend'))
        text=getattr(page,'_legend_var',None)
        ttk.Label(window,text=text.get() if text else tr('shell.legend_unavailable'),wraplength=580,padding=18).pack()
        ttk.Button(window,text=tr('shell.close'),command=window.destroy).pack(pady=10)
        return window

    def persist_plot(self,page,spec):
        if self.document:
            index=next((i for i,item in enumerate(self.document.study.plots) if item is page.spec),None)
            if index is not None:
                self.document.edit(lambda study:study.plots.__setitem__(index,deepcopy(spec)))
                page.spec=self.document.study.plots[index]
                self.update_title()
            else:page.spec=deepcopy(spec)
        else:page.spec=deepcopy(spec)
        page.redraw(force=True)

    def axis_scale_dialog(self):
        page=self.plot_page()
        if page is None:return
        def apply(fields):
            index=int(fields['axis'])-1
            values={key:float(fields[key]) if fields[key].strip() else None for key in ('xmin','xmax','ymin','ymax')}
            if any(v is not None and not math.isfinite(v) for v in values.values()):raise ValueError(tr('shell.limits_must_be_finite'))
            limits={}
            for axis,lo,hi in [('xlim_s','xmin','xmax'),('ylim','ymin','ymax')]:
                if (values[lo] is None)!=(values[hi] is None):raise ValueError(tr('shell.complete_both_ends_or_leave_both_empty'))
                if values[lo] is not None:limits[axis]=[values[lo],values[hi]]
            spec=deepcopy(page.spec)
            spec.setdefault('axis_scales',{})[str(index)]=limits
            axis_scale(spec,index)
            self.persist_plot(page,spec)
            page.set_axis_scale(index,xlim_s=limits.get('xlim_s'),ylim=limits.get('ylim'))
        return self.form(tr('shell.axis_scales'),[('axis','Eje',1,list(range(1,len(page.axes)+1))),
            ('xmin',tr('shell.minimum_time_s'),'',None),('xmax',tr('shell.maximum_time_s'),'',None),
            ('ymin',tr('shell.minimum_y_axis_unit'),'',None),('ymax',tr('shell.maximum_y_axis_unit'),'',None)],apply)

    def frequency_dialog(self):
        page=self.plot_page()
        if page is None:return
        def apply(fields):
            value=float(fields['base'])
            if not math.isfinite(value) or value<=0:raise ValueError(tr('shell.the_base_must_be_positive_and_finite'))
            spec=deepcopy(page.spec)
            spec.update(frequency_mode=fields['mode'],frequency_base_hz=value)
            self.persist_plot(page,spec)
            page.frequency_mode.set(fields['mode'])
        return self.form(tr('shell.frequency_display'),[('mode',tr('integration.mode'),page.frequency_mode.get(),['absolute','deviation']),
            ('base',tr('shell.explicit_base_hz'),self.model.get('meta',{}).get('f_nom_hz',50),None)],apply)

    def cursor_dialog(self):
        page=self.plot_page()
        if page is None:return
        def apply(fields):
            axis=page.axes[int(fields['axis'])-1]
            values=[float(fields[key]) for key in ('first','second')]
            if not all(math.isfinite(value) for value in values):raise ValueError(tr('shell.times_must_be_finite'))
            for value in values:page.capture_cursor(SimpleNamespace(inaxes=axis,xdata=value))
        return self.form(tr('shell.measurement_cursors'),[('axis','Eje',1,list(range(1,len(page.axes)+1))),
            ('first',tr('shell.first_cursor_s'),0,None),('second',tr('shell.second_cursor_s'),.01,None)],apply)

    def xy_dialog(self):
        page=self.plot_page()
        if page is None:return
        keys=page.buffer.keys[1:]
        if not keys:
            self.output.log('AVISO',tr('shell.no_acquired_signals_for_the_x_y'))
            return
        labels={key:key for key in keys}
        def apply(fields):
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg,NavigationToolbar2Tk
            figure=page.xy_figure(labels[fields['x']],labels[fields['y']])
            frame=ttk.Frame(self.book)
            canvas=FigureCanvasTkAgg(figure,master=frame)
            canvas.get_tk_widget().pack(fill='both',expand=True)
            NavigationToolbar2Tk(canvas,frame)
            def export():
                path=filedialog.asksaveasfilename(parent=self,defaultextension='.pdf',filetypes=[('PDF','*.pdf')])
                if path:figure.savefig(path,format='pdf',facecolor='white')
            ttk.Button(frame,text=tr('shell.export_pdf_3dd7d5'),command=export).pack(anchor='e')
            frame.figure,frame.canvas=figure,canvas
            frame.export_pdf=export
            self.book.add(frame,text=tr('shell.x_y_plot'))
            self.book.select(frame)
            canvas.draw_idle()
        return self.form(tr('shell.x_y_plot'),[('x',tr('shell.x_signal'),keys[0],keys),('y',tr('shell.y_signal'),keys[min(1,len(keys)-1)],keys)],apply)

    def plot_export(self,kind):
        current=self.current_tab()
        if kind=='pdf' and hasattr(current,'export_pdf'):
            current.export_pdf()
        else:
            page=self.plot_page()
            if page:getattr(page,'export_'+kind)()

    def log_island_summary(self,result):
        self._lf_islands=result.get('islands',[])
        dead=[island for island in self._lf_islands if island.get('status')=='sin_referencia']
        failed=[island for island in self._lf_islands if island.get('status') not in ('resuelto','sin_referencia')]
        if not (dead or failed):return
        if not hasattr(self,'_island_runs'):
            self._island_runs={}
            self.output.text.bind('<<Modified>>',self.restore_island_links,add='+')
        marker=f'LF-{len(self._island_runs)+1}'
        identifiers={ident for island in self._lf_islands for group in ('loads','gens','shunts')
                     for ident in island.get('elements',{}).get(group,[])}
        self._island_runs[marker]=(deepcopy(self._lf_islands),{ident:deepcopy(self.index[ident]) for ident in identifiers if ident in self.index})
        self.output.log('INFO',tr('shell.de_energized_islands', value0=len(dead))+(tr('shell.failed_islands', value0=len(failed)) if failed else '')+tr('shell.open_details')+marker)
        self.restore_island_links()

    def restore_island_links(self,event=None):
        text=self.output.text
        if event is not None and not text.edit_modified():return
        text.edit_modified(False)
        for marker,(islands,index) in getattr(self,'_island_runs',{}).items():
            start='1.0'
            while True:
                found=text.search(tr('shell.open_details_da6794')+marker,start,stopindex='end')
                if not found:break
                begin,end=text.index(found+' linestart'),text.index(found+' lineend')
                tag='island_detail_'+marker
                text.tag_add('island_detail',begin,end)
                text.tag_add(tag,begin,end)
                text.tag_configure(tag,foreground=theme.color('accent'),underline=True)
                text.tag_bind(tag,'<Button-1>',lambda event,rows=islands,items=index:self.show_island_details(rows,items))
                start=end+'+1c'

    def show_island_details(self,islands=None,index=None):
        window=tk.Toplevel(self)
        window.title(tr('shell.load_flow_island_details'))
        window.geometry('760x400')
        tree=ttk.Treeview(window,columns=('state','equipment'),show='tree headings')
        tree.heading('#0',text=tr('shell.island'))
        tree.heading('state',text=tr('shell.state'))
        tree.heading('equipment',text=tr('shell.equipment_without_supply_3c4714'))
        tree.column('#0',width=70)
        tree.column('state',width=150)
        tree.column('equipment',width=500)
        index=self.index if index is None else index
        for island in (getattr(self,'_lf_islands',[]) if islands is None else islands):
            parent=tree.insert('','end',text=str(island.get('id')),values=(island.get('status'),''))
            if island.get('status')!='resuelto':
                from .presentation import element_label
                for group in ('loads','gens','shunts'):
                    for ident in island.get('elements',{}).get(group,[]):
                        tree.insert(parent,'end',values=(group,element_label(index.get(ident,{'id':ident}),index)))
        scroll=ttk.Scrollbar(window,command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        tree.pack(fill='both',expand=True)
        return window

    def configure_net_toolbar(self,panel):
        """Adapta acciones públicas del editor heredado sin modificar su código."""
        from .app_shell import icon,Tooltip
        children=panel.winfo_children()
        if hasattr(panel,'groups'):
            panel.integration_images=[]
            for owner in children[:2]:
                for number,button in enumerate(owner.winfo_children()):
                    if isinstance(button,ttk.Button):
                        title=str(button.cget('text'))
                        picture=icon(panel,number)
                        panel.integration_images.append(picture)
                        button.configure(image=picture,compound='image',style='Compact.TButton')
                        Tooltip(button,title)
            panel.canvas.configure(width=1,height=140)
            def wrap(event):
                if event.widget==panel and event.width>100:
                    for child in children:
                        if isinstance(child,ttk.Label):child.configure(wraplength=event.width-12)
            panel.bind('<Configure>',wrap,add='+')
            return
        if not children or not all(isinstance(child,ttk.Button) for child in children[0].winfo_children()):
            return
        original=children[0]
        original.pack_forget()
        toolbar=ttk.Frame(panel,padding=(2,1))
        toolbar.pack(fill='x',before=children[1])
        panel.integration_images=[]
        actions=[(tr('integration.create'),panel.create),(tr('integration.edit'),panel.edit_selected),(tr('shell.connect_port'),panel.connect_selected),
            (tr('shell.disconnect_port'),panel.disconnect_selected),(tr('integration.retire'),panel.retire_selected),
            (tr('integration.undo'),panel.controller.undo),(tr('integration.redo'),panel.controller.redo),(tr('shell.save_variant'),panel.save),
            (tr('shell.open_variant'),panel.open),('Materializar',lambda:panel.controller.submit()),
            (tr('shell.load_flow'),lambda:panel.controller.submit(run_loadflow=True))]
        for number,(title,action) in enumerate(actions):
            picture=icon(panel,number)
            panel.integration_images.append(picture)
            button=ttk.Button(toolbar,image=picture,style='Compact.TButton',command=lambda action=action:panel.guard(action))
            button.pack(side='left',padx=1)
            Tooltip(button,title)
        panel.canvas.configure(width=1,height=140)
        panel.integration_toolbar=toolbar

    def configure_plot_page(self,page):
        """Monta acceso compacto a las acciones públicas de PlotPage."""
        from .app_shell import icon,Tooltip
        page.toolbar.master.pack_forget()
        canvas_widget=page.canvas.get_tk_widget()
        canvas_widget.pack_forget()
        for child in page.winfo_children():
            if isinstance(child,ttk.Label) and str(child.cget('textvariable')) in (str(page.statistics),str(page.cursor)):
                child.pack_configure(side='bottom',fill='x')
        toolbar=ttk.Frame(page,padding=(2,1))
        toolbar.pack(side='bottom',fill='x')
        page.integration_images=[]
        def toggle():
            page.show_pf.set(not page.show_pf.get())
            page.toggle_reference()
        actions=[(tr('shell.reset_view'),page.toolbar.home),(tr('shell.previous_view'),page.toolbar.back),
            (tr('shell.next_view'),page.toolbar.forward),('Desplazar',page.toolbar.pan),(tr('shell.zoom_region'),page.toolbar.zoom),
            (tr('shell.export_csv_5755f9'),page.export_csv),(tr('shell.export_png_5fe908'),page.export_png),(tr('shell.export_pdf_3dd7d5'),page.export_pdf),
            (tr('shell.show_or_hide_the_available_reference'),toggle)]
        for number,(title,action) in enumerate(actions):
            picture=icon(page,number)
            page.integration_images.append(picture)
            button=ttk.Button(toolbar,image=picture,style='Compact.TButton',command=action)
            button.pack(side='left',padx=1)
            Tooltip(button,title)
        ttk.Label(toolbar,textvariable=page.reference_note,style='Muted.TLabel').pack(side='left',padx=5)
        def wrap(event):
            if event.widget==page and event.width>100:
                for child in page.winfo_children():
                    if isinstance(child,ttk.Label) and str(child.cget('textvariable')) in (str(page.statistics),str(page.cursor)):
                        child.configure(wraplength=event.width-15)
        page.bind('<Configure>',wrap,add='+')
        canvas_widget.pack(fill='both',expand=True)
        page.integration_toolbar=toolbar

    def open_data_table(self,group='loads',kind=None):
        from .app_data_views import open_data_table
        return open_data_table(self,group,kind)

    def refresh_data_windows(self):
        from .app_data_views import refresh_data_windows
        refresh_data_windows(self)
