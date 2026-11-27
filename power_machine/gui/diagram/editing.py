"""PowerFactory-style drawing gestures on the shared diagram surface."""
import tkinter as tk
from tkinter import ttk,messagebox
from ..theme import theme
from ..i18n import tr
from ..netedit.tools import TOOLS,drawing_template
from .icons import icon
from .layout import Symbol,Wire


class DiagramEditing:
    def setup_editing(self):
        self._edit_var=tk.BooleanVar(self,False)
        self._drawing_tool='select'
        self._drawing_buses=[]
        self._reconnect_target=None
        self._toolbox=ttk.Frame(self._body,padding=4)
        self._edit_mode_text=tk.StringVar(self)
        self._toolbox_images=[]
        self.canvas.bind('<Escape>',self.cancel_drawing)
        self.canvas.bind('<Delete>',lambda event:self.delete_selection())
        self.canvas.bind('<Control-z>',lambda event:self._edit_history(False))
        self.canvas.bind('<Control-y>',lambda event:self._edit_history(True))
        self._edit_tooltip_job=None

    @property
    def editing(self):return hasattr(self,'_edit_var') and self._edit_var.get()

    @property
    def editor_controller(self):
        from ..netedit.integration import get_editor_controller
        if not getattr(self.app,'document',None):return None
        return get_editor_controller(self.app)

    def editing_toolbar(self):
        if not hasattr(self,'_edit_var'):return
        parent=self._toolbar
        ttk.Separator(parent,orient='vertical').pack(side='left',fill='y',padx=4)
        ttk.Checkbutton(parent,text=tr('edit.enable'),variable=self._edit_var,
                        command=self.toggle_editing).pack(side='left',padx=4)
        ttk.Label(parent,textvariable=self._edit_mode_text,style='Muted.TLabel',font=theme.font('small')).pack(side='left')
        self._build_toolbox()

    def _build_toolbox(self):
        for child in self._toolbox.winfo_children():child.destroy()
        self._toolbox_images=[]
        for tool in ('select',*TOOLS):
            image=self._drawing_icon(tool)
            self._toolbox_images.append(image)
            ttk.Button(self._toolbox,image=image,text=tr('edit.tool.'+tool),compound='left',
                command=lambda t=tool:self.choose_drawing_tool(t),padding=(4,5)).pack(fill='x',pady=1)
        self._edit_mode_text.set(tr('edit.mode_on' if self.editing else 'edit.mode_off'))

    def _drawing_icon(self,tool):
        if tool=='select':return icon(self._toolbox,'select')
        image=tk.PhotoImage(master=self._toolbox,width=22,height=22)
        ink=theme.color('accent')
        def line(x,y,xx,yy):
            steps=max(abs(xx-x),abs(yy-y),1)
            for i in range(steps+1):
                a,b=round(x+(xx-x)*i/steps),round(y+(yy-y)*i/steps)
                image.put(ink,to=(a,b,min(22,a+2),min(22,b+2)))
        if tool=='bus':line(3,11,19,11)
        elif tool in ('line','switch'):
            line(2,11,8,11)
            line(8,11,15,5 if tool=='switch' else 11)
            line(15,11,20,11)
        elif tool.startswith('transformer'):
            for cx,cy in [(8,9),(14,13)]+([(6,16)] if tool=='transformer3' else []):
                for a,b in [((-4,-3),(3,-3)),((3,-3),(5,1)),((5,1),(1,5)),((1,5),(-4,2)),((-4,2),(-4,-3))]:line(cx+a[0],cy+a[1],cx+b[0],cy+b[1])
        elif tool=='load':
            line(11,2,11,7)
            line(4,7,18,7)
            line(4,7,11,19)
            line(18,7,11,19)
        elif tool=='shunt':
            line(11,2,11,8)
            line(3,8,19,8)
            line(3,13,19,13)
            line(11,13,11,20)
        else:
            for a,b in [((5,4),(16,4)),((16,4),(20,11)),((20,11),(16,18)),((16,18),(5,18)),((5,18),(2,11)),((2,11),(5,4))]:line(*a,*b)
            line(7,12,10,8)
            line(10,8,14,14)
            line(14,14,17,10)
            if tool=='static':line(4,20,19,2)
        return image

    def toggle_editing(self):
        if self.editing:
            if not hasattr(self,'_editing_base_positions'):
                self._editing_base_positions={key:(s.x,s.y) for key,s in self.scene.symbols.items()}
            if self.editor_controller is None:
                self._edit_var.set(False)
                return
            self._toolbox.pack(side='left',fill='y',before=self.canvas)
        else:
            self._toolbox.pack_forget()
        self.cancel_drawing()
        self._edit_mode_text.set(tr('edit.mode_on' if self.editing else 'edit.mode_off'))
        self.bus.publish('diagram.mode.changed',editing=self.editing)

    def set_edit_mode(self,enabled):
        """Enable drawing explicitly, or return the diagram to view-only mode."""
        self._edit_var.set(bool(enabled))
        self.toggle_editing()
        return self.editing

    def choose_drawing_tool(self,tool):
        self.cancel_drawing()
        self._drawing_tool=tool
        self.set_tool('select')
        self.status.set(tr('diagram.focus_hint') if tool=='select' else
                        tr('edit.place_bus') if tool=='bus' else tr('edit.pick_bus'))
        self.canvas.focus_set()

    def cancel_drawing(self,event=None):
        self._drawing_buses=[]
        self._drawing_tool='select'
        self._reconnect_target=None
        self.canvas.delete('drawing_preview')
        self._set_status()
        if event is not None:return 'break'

    def _bus_hit(self,event):
        for key,symbol in self.scene.symbols.items():
            if symbol.kind!='bus' or symbol.item.get('outserv'):continue
            x,y=self._xy(symbol.x,symbol.y)
            if abs(event.x-x)<=max(10,symbol.width*self.scale*self.x_factor/2) and abs(event.y-y)<=10:
                return key

    def drawing_press(self,event):
        if self.editing and self._reconnect_target:
            bus=self._bus_hit(event)
            if bus:
                identity,field=self._reconnect_target
                self.editor_controller.reconnect(identity,field,bus)
                self.cancel_drawing()
            return True
        if not self.editing or self._drawing_tool=='select':return False
        tool=self._drawing_tool
        group,cls,count=TOOLS[tool]
        if count:
            bus=self._bus_hit(event)
            if bus is None:
                self.status.set(tr('edit.pick_bus'))
                return True
            if bus in self._drawing_buses:
                self.status.set(tr('edit.distinct_buses'))
                return True
            self._drawing_buses.append(bus)
            if len(self._drawing_buses)<count:return True
        x=(event.x-self.offset[0])/(self.scale*self.x_factor)
        y=(event.y-self.offset[1])/self.scale
        data=drawing_template(tool)
        fields=('bus_hv','bus_mv','bus_lv') if group=='branches3' else ('bus1','bus2') if group=='switches' else ('bus_from','bus_to') if group=='branches' else ('bus',)
        for field,bus in zip(fields,self._drawing_buses):data[field]=bus
        data['editor_position']=[x,y+45 if count==1 else y]
        if count>1:
            points=[self.scene.symbols[key] for key in self._drawing_buses]
            data['editor_position']=[sum(p.x for p in points)/len(points),sum(p.y for p in points)/len(points)]
        if self.model.get('buses'):data['scope']=self.model['buses'][0].get('scope')
        controller=self.editor_controller
        identity=controller.create(group,data,draft=True)
        self.cancel_drawing()
        self._rebuild_scene()
        self.draw()
        self.select_ids([identity])
        controller.selected_id=identity
        self.open_editor(identity)
        return True

    def drawing_motion(self,event):
        if not self.editing or not self._drawing_buses:return
        points=[]
        for bus in self._drawing_buses:
            symbol=self.scene.symbols[bus]
            points.extend(self._xy(symbol.x,symbol.y))
        self.canvas.delete('drawing_preview')
        self.canvas.create_line(*points,event.x,event.y,fill=theme.color('accent'),width=2,
            dash=(5,3),tags='drawing_preview')

    def open_editor(self,identity):
        from ..element_dialog import open_element_dialog
        controller=self.editor_controller
        if controller is None:
            self.bus.publish('element.open',id=identity)
            return
        window=open_element_dialog(self.winfo_toplevel(),controller,identity)
        window.editor.set_readonly(not self.editing)
        token=self._edit_var.trace_add('write',lambda *args:window.editor.set_readonly(not self.editing))
        def detach(event):
            if event.widget is window:
                try:self._edit_var.trace_remove('write',token)
                except (AttributeError,tk.TclError):pass
        window.bind('<Destroy>',detach,add='+')
        self._last_element_dialog=window
        return window

    def begin_reconnect(self,identity,field=None):
        ports=self.editor_controller.ports(identity)
        if not ports:return
        self.cancel_drawing()
        self._reconnect_target=(identity,field or ports[0]['field'])
        self.status.set(tr('edit.pick_bus'))

    def _double(self,event):
        key=self._hit(event)
        symbol=self.scene.symbols.get(key)
        if symbol and (symbol.remote_group or symbol.kind=='group'):
            return self.app.open_diagram(symbol.remote_group or key)
        if key:self.open_editor(key)

    def _context(self,event):
        from .view import context_actions
        from .context_menu import ElementContextMenu
        key=self._hit(event)
        if not key:return
        self._hide_tooltip()
        if key not in self.selected_ids:self.select_ids([key])
        if self._menu is not None:
            try:self._menu.destroy()
            except tk.TclError:pass
        menu=ElementContextMenu(self,self.index.get(key,{}).get('name',key))
        self._menu=menu
        self.add_context_data(menu,key)
        for label,action in [(tr('edit.edit'),'edit'),(tr('edit.delete'),'delete'),
                (tr('edit.inserv') if self.index.get(key,{}).get('outserv') else tr('edit.outserv'),'service'),
                (tr('edit.disconnect'),'disconnect_ports')]:
            menu.add_command(label=label,command=lambda a=action:self.edit_action(key,a),state='normal' if self.editing else 'disabled')
        menu.add_separator()
        for label,action in context_actions(self.index.get(key,{}),self.gen_trip_supported):
            if action in ('properties','outserv'):continue
            menu.add_command(label=label,command=lambda a=action:self.request_action(key,a))
        menu.tk_popup(event.x_root,event.y_root)

    def edit_action(self,key,action):
        if not self.editing:
            self.status.set(tr('edit.readonly'))
            return
        try:
            controller=self.editor_controller
            controller.selected_id=key
            if action=='edit':return self.open_editor(key)
            if action=='delete':return self.delete_selection()
            if action=='service':controller.edit(key,dict(outserv=not bool(self.index[key].get('outserv'))))
            if action=='disconnect_ports':
                controller.disconnect(key)
        except (ValueError,TypeError,KeyError) as exc:self.status.set(str(exc))

    def delete_selection(self):
        if not self.editing:
            self.status.set(tr('edit.readonly'))
            return 'break'
        ids=list(self.selected_ids)
        if ids and messagebox.askyesno(tr('edit.delete'),tr('edit.confirm_delete',count=len(ids)),parent=self.winfo_toplevel()):
            self.editor_controller.retire_many(ids)
            self.select_ids([])
        return 'break'

    def _edit_history(self,redo):
        if not self.editing:return 'break'
        (self.editor_controller.redo if redo else self.editor_controller.undo)()
        return 'break'

    def editing_positions(self):
        controller=getattr(self.app,'_diagram_edit_controller',None)
        if controller is None or controller.session is not getattr(getattr(self.app,'document',None),'network',None):return
        moving=getattr(self,'_drag',None)
        pending={key:self.positions[key] for key in self.selected_ids if key in self.positions} if moving and moving[0] else {}
        self.positions.update(getattr(self,'_editing_base_positions',{}))
        self.positions.update(controller.layout.get('groups',{}).get('__editor__',{}))
        self.positions.update(pending)
        self._draft_drag_positions=pending

    def editing_scene(self):
        controller=getattr(self.app,'_diagram_edit_controller',None)
        if controller is None or controller.session is not getattr(getattr(self.app,'document',None),'network',None):return
        retired=set()
        for command in controller.variant.commands:
            if command.op=='retire':retired.add(command.object_id)
            elif command.data.get('outserv') is False or command.data.get('outserv')==0:retired.discard(command.object_id)
        for key in retired:self.scene.symbols.pop(key,None)
        self.scene.wires[:]=[w for w in self.scene.wires if w.id not in retired]
        kinds=dict(buses='bus',loads='load',gens='generator',shunts='shunt',branches='branch',branches3='transformer3',switches='switch')
        for key,item in controller.drafts.items():
            x,y=getattr(self,'_draft_drag_positions',{}).get(key,item.get('editor_position',[100,100]))
            self.index[key]=item
            self.scene.symbols[key]=Symbol(key,kinds[item['group']],x,y,75 if item['group']=='buses' else 18,18,
                (item.get('name') or tr('edit.draft'))+' *',item.get('un_kv'),item,
                (x+14,y+14,x+160,y+38))
            for port in controller.ports(key):
                bus=self.scene.symbols.get(port['bus_id'])
                if bus:self.scene.wires.append(Wire(key,bus.id,key,[(bus.x,bus.y),(x,y)],bus.un_kv))
