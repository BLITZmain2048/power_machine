"""Short-circuit study panel with traceable source inputs."""
from __future__ import annotations
from .panel_widgets import action_button
import copy, json, math
from dataclasses import asdict
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk
from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions
from power_machine.shortcircuit.results import file_sha256, fingerprint
from ..i18n import tr
from .panel_widgets import AdvancedOptions, ElementSelector, ResultTable, diagnostic_lines, element_label, error_result, finish_panel, human_status, panel_retranslate, study_panel

PANEL_SPEC={"id":"shortcircuit","title":"Short circuit IEC / ANSI","api_version":1,"factory":"create_panel","order":10}

def result_text(result):
    if "progress" in result:return tr("panel.shortcircuit.progress",sequence=result["progress"].get("sequence",""))
    payload=result.get("payload",{})
    lines=[tr("panel.shortcircuit.status",value=human_status(result.get("status","no result"))),result.get("gui_validation_label",tr("panel.shortcircuit.not_validated")),tr("panel.shortcircuit.result_bus",value=str(result.get("gui_fault_label",payload.get("fault",{}).get("object_id",""))))]
    for key,label in (("Ik_initial_sym_ka","Ik''"),("ip_peak_ka","ip"),("Ib_sym_ka","Ib"),("Ith_ka","Ith")):
        lines.append(tr("panel.shortcircuit.value",label=label,value=payload[key]) if payload.get(key) is not None else tr("panel.shortcircuit.not_covered",label=label))
    if payload.get("rl"):lines.append(tr("panel.shortcircuit.rl",value=payload["rl"]["rms_window_ka"]))
    return "\n".join(lines+diagnostic_lines(result))

def presentation_quantities(result,model):
    shown=copy.deepcopy(result); payload=shown.setdefault("payload",{}); fault=payload.get("fault",{})
    bus=next((b for b in model.get("buses",[]) if b.get("id")==fault.get("bus_id",fault.get("object_id"))),{})
    if bus:shown["gui_fault_label"]=element_label(bus,model)
    ik,un=payload.get("Ik_initial_sym_ka"),bus.get("un_kv")
    if payload.get("Sk_initial_mva") is None and ik is not None and un is not None:payload["Sk_initial_mva"]=math.sqrt(3)*float(un)*float(ik)
    z=payload.get("z_th_ohm"); z=z.get("1",z) if isinstance(z,dict) else z
    if isinstance(z,dict): r,x=z.get("real"),z.get("imag")
    elif isinstance(z,(list,tuple)) and len(z)==2:r,x=z
    elif isinstance(z,complex):r,x=z.real,z.imag
    else:r=x=None
    if payload.get("r_x_ratio") is None and r is not None and x:payload["r_x_ratio"]=float(r)/float(x)
    return shown

class PanelController:
    def __init__(self,context,on_display=None):self.context,self.on_display,self.evidence_label=context,on_display,None
    def submit(self,fault,options,supplemental_path):
        snapshot=copy.deepcopy(self.context.get_snapshot()); self.submitted_model=snapshot["model"]
        if not str(supplemental_path).strip():raise ValueError(tr("panel.shortcircuit.data_required"))
        path=Path(supplemental_path); path=(path if path.is_absolute() else Path(self.context.root)/path).resolve()
        if not path.is_file():raise ValueError(tr("panel.shortcircuit.data_invalid"))
        if callable(getattr(self,"on_options",None)):self.on_options(options)
        if callable(getattr(self,"on_submit",None)):self.on_submit(fault.bus_id,path)
        root=Path(self.context.root).resolve()
        if path.is_relative_to(root):source={"supplemental_path":str(path.relative_to(root)),"supplemental_sha256":file_sha256(path)}
        else:
            data=json.loads(path.read_text(encoding="utf-8-sig")); source={"supplemental":data,"supplemental_sha256":fingerprint(data)}
        from ..evidence import sc_reference_label
        try:self.evidence_label=sc_reference_label(snapshot["model"],options,json.loads(path.read_text(encoding="utf-8-sig")),fault)
        except (ValueError,OSError,KeyError):self.evidence_label=None
        if any(snapshot.get("study",{}).get("scenario",{}).values()):self.evidence_label=None
        request={"schema":"power_machine-parity-request-v1","module":"shortcircuit","root":str(self.context.root),"snapshot":snapshot,"options":asdict(options),"input":{"fault":asdict(fault),**source}}
        self.context.select_element(fault.bus_id); return self.context.submit_job("shortcircuit",request,self.on_message)
    def on_message(self,message):
        kind=message[0]
        if kind=="result":
            result=copy.deepcopy(message[1]); from ..evidence import sc_result_matches_reference
            if self.evidence_label and sc_result_matches_reference(result,self.submitted_model):result["gui_validation_label"]=self.evidence_label
            self.context.publish_result(result); self.context.log("INFO","Short circuit: "+result["status"])
            if self.on_display:self.on_display(result)
        elif kind=="log":self.context.log(message[1],message[2])
        elif kind=="error":
            self.context.log("ERROR",str(message[1])); self.on_display and self.on_display(error_result(message[1]))
        elif kind=="progress" and self.on_display:self.on_display({"progress":message[1]})

def create_panel(parent,context):
    shell,frame,results=study_panel(parent); snapshot=context.get_snapshot(); model=snapshot["model"]
    buses=[b for b in model.get("buses",[]) if b.get("outserv",0)!=1]; profile="iec_pf2016_c1_v1" if model.get("meta",{}).get("benchmark")=="ieee39_pf" else "iec_radial_v1"
    choices={"kind":("3ph","ll","lg","llg"),"method":("iec60909","ansi_ieee"),"profile":("iec_radial_v1","iec_pf2016_c1_v1","analytical_rl_v1"),"case":("maximum","minimum"),"lv_tolerance":("","6","10")}
    defaults={"bus":buses[0]["id"] if buses else "","kind":"3ph","method":"iec60909","profile":profile,"case":"maximum","rf":"0","xf":"0","duration":"0.1","break":"0.02","prefault":"1.0","frequency":str(model.get("meta",{}).get("f_nom_hz",50)),"lv_tolerance":"","alpha":"0","rl_start":"0","rl_end":"0.02","path":""}
    vars_={k:tk.StringVar(frame,value=v) for k,v in defaults.items()}; fields=(("bus","bus"),("kind","fault_kind"),("method","method"),("profile","profile"),("case","case"),("rf","rf"),("xf","xf"),("duration","duration"),("break","break"),("prefault","prefault"),("frequency","frequency"),("lv_tolerance","lv_tolerance"),("alpha","alpha"),("rl_start","rl_start"),("rl_end","rl_end"),("path","data"))
    advanced=AdvancedOptions(frame) if parent is not None else None
    if advanced:advanced.grid(row=len(fields),column=0,columnspan=2,sticky="ew")
    labels={}; full_paths={}
    for row,(key,label_key) in enumerate(fields):
        owner=advanced.content if advanced and key not in ("bus","kind","method","profile","case","path") else frame
        labels[key]=ttk.Label(owner,text=tr("panel.shortcircuit."+label_key)); labels[key].grid(row=row,column=0,sticky="w",pady=2)
        if key=="bus" and parent is not None:widget=ElementSelector(owner,buses,model,vars_[key])
        else:widget=ttk.Combobox(owner,textvariable=vars_[key],values=choices[key],state="readonly") if key in choices else ttk.Entry(owner,textvariable=vars_[key])
        widget.grid(row=row,column=1,sticky="ew")
    table=ResultTable(results,[("ik","Ik'' [kA]"),("ip","ip [kA]"),("ib","Ib [kA]"),("sk","Sk'' [MVA]"),("rx","R/X [1]")],context.root) if parent is not None else None
    if table:table.grid(row=0,column=0,columnspan=3,sticky="nsew")
    output=tk.Text(results,height=6,width=70,wrap="word",state="disabled"); output.grid(row=1,column=0,columnspan=3,sticky="nsew")
    def display(result):
        shown=presentation_quantities(result,model)
        if hasattr(shell,"show_state"):
            shell.validation_label=shown.get("gui_validation_label",tr("panel.shortcircuit.not_validated")); shell.show_state(shown); shell.set_diagnostics(shown.get("diagnostics",[]))
        if table and "progress" not in shown:
            p=shown.get("payload",{}); table.set_rows([{k:p.get(v) for k,v in (("ik","Ik_initial_sym_ka"),("ip","ip_peak_ka"),("ib","Ib_sym_ka"),("sk","Sk_initial_mva"),("rx","r_x_ratio"))}])
        output.configure(state="normal"); output.delete("1.0","end"); output.insert("end",result_text(shown)); output.configure(state="disabled")
    controller=PanelController(context,display)
    def set_source(path):path=Path(path).resolve(); full_paths[path.name]=str(path); vars_["path"].set(path.name)
    def browse():
        path=filedialog.askopenfilename(parent=frame,initialdir=context.root,filetypes=((tr("panel.shortcircuit.data_file"),"*.json"),))
        if path:set_source(path)
    def submit():
        try:
            method=vars_["method"].get(); options=ShortCircuitOptions(method,vars_["case"].get(),frequency_hz=float(vars_["frequency"].get()),min_break_time_s=float(vars_["break"].get()) if vars_["break"].get() else None,fault_duration_s=float(vars_["duration"].get()) if vars_["duration"].get() else None,profile=vars_["profile"].get() if method=="iec60909" else "analytical_rl_v1",prefault_voltage_pu=float(vars_["prefault"].get()) if method=="ansi_ieee" else None,lv_tolerance_pct=float(vars_["lv_tolerance"].get()) if vars_["lv_tolerance"].get() else None,inception_angle_rad=float(vars_["alpha"].get()),rl_window_start_s=float(vars_["rl_start"].get()),rl_window_end_s=float(vars_["rl_end"].get()))
            controller.submit(FaultSpec(vars_["bus"].get(),vars_["kind"].get(),float(vars_["rf"].get()),float(vars_["xf"].get())),options,full_paths.get(vars_["path"].get(),vars_["path"].get()))
        except (ValueError,OSError) as exc:context.log("ERROR",str(exc)); display(error_result(exc))
    browse_button=ttk.Button(frame,text=tr("panel.shortcircuit.choose_data"),command=browse); calculate_button=action_button(frame, 'run', text=tr('panel.shortcircuit.calculate'), command=submit)
    browse_button.grid(row=len(fields)+1,column=0,sticky="w"); calculate_button.grid(row=len(fields)+1,column=1,sticky="w")
    notice=ttk.Label(frame,text=tr("panel.shortcircuit.notice"),wraplength=650); notice.grid(row=len(fields)+2,column=0,columnspan=3,sticky="w")
    frame.columnconfigure(1,weight=1); results.columnconfigure(0,weight=1); results.rowconfigure(1,weight=1); shell.controller=controller
    def configure_case(config):
        for source,key in (("frequency_hz","frequency"),("supplemental_path","path"),("bus_id","bus"),("profile","profile"),("min_break_time_s","break"),("method","method"),("case","case")):
            if config.get(source) is not None:set_source(config[source]) if key=="path" else vars_[key].set(str(config[source]))
    shell.configure_case=configure_case; controller.on_options=lambda options:configure_case(asdict(options)); controller.on_submit=lambda ident,path:configure_case({"bus_id":ident,"supplemental_path":str(path)})
    def changed(*_):
        if table and table.rows:shell.mark_stale(tr("panel.shortcircuit.stale"))
    for variable in vars_.values():variable.trace_add("write",changed)
    def preflight_inputs():
        raw=full_paths.get(vars_["path"].get(),vars_["path"].get())
        if not raw:raise ValueError(tr("panel.shortcircuit.data_required"))
        data=json.loads((Path(raw) if Path(raw).is_absolute() else Path(context.root)/raw).read_text(encoding="utf-8-sig"))
        return {"input":{"supplemental":data,"supplemental_sha256":fingerprint(data),"selected_bus_ids":[vars_["bus"].get()]},"options":{"method":vars_["method"].get(),"case":vars_["case"].get(),"frequency_hz":float(vars_["frequency"].get()),"profile":vars_["profile"].get(),"fault_kind":vars_["kind"].get(),"min_break_time_s":float(vars_["break"].get())}}
    shell.preflight_inputs=preflight_inputs
    def retranslate():
        for key,label_key in fields:labels[key].configure(text=tr("panel.shortcircuit."+label_key))
        browse_button.configure(text=tr("panel.shortcircuit.choose_data")); calculate_button.configure(text=tr("panel.shortcircuit.calculate")); notice.configure(text=tr("panel.shortcircuit.notice"))
    panel_retranslate(shell,retranslate)
    shell.recovery_key = 'panel.shortcircuit.choose_data'
    shell.recovery_action = browse
    return finish_panel(shell,context,controller,tr("panel.shortcircuit.scope"))
