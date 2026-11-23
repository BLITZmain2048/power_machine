"""MCP tools, schemas, input validation, and result access."""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Any, Callable
from .session import SessionStore

NETWORK={"type":"string","minLength":1,"description":"Session network identifier.","examples":["network_id"]}
POS={"type":"number","exclusiveMinimum":0}
PAGE={"type":"integer","minimum":1,"maximum":500,"default":100,"description":"Maximum returned rows."}
CURSOR={"type":"string","pattern":"^[0-9]+$","default":"0"}
def obj(properties:dict[str,Any], required:list[str]=[], **extra:Any)->dict[str,Any]:
    return {"type":"object","properties":properties,"required":required,"additionalProperties":False,**extra}
def ann(read:bool, destructive:bool=False, idem:bool=True)->dict[str,bool]:
    return {"readOnlyHint":read,"destructiveHint":destructive,"idempotentHint":idem}
SUMMARY=obj({"result_id":{"type":"string"},"study":{"type":"string"},"status":{"type":"string"},"coverage":{"type":"object"},"worst_cases":{"type":"array","items":{"type":"object"}}},["result_id","study","status","coverage","worst_cases"])
STUDY=obj({"summary":SUMMARY},["summary"])
def tool(name:str, description:str, schema:dict[str,Any], output:dict[str,Any]|None=None, **a:Any)->dict[str,Any]:
    value={"name":name,"description":description,"inputSchema":schema,"annotations":ann(**a)}
    if output: value["outputSchema"]=output
    return value
TOOL_DEFINITIONS=[
tool("list_cases","List included cases.",obj({}),obj({"cases":{"type":"array","items":{"type":"string"}}},["cases"]),read=True),
tool("load_case","Open an included case or a user supplied model path.",obj({"name_or_path":{"type":"string","minLength":1,"examples":["ieee39"]}},["name_or_path"]),obj({"network_id":NETWORK,"summary":{"type":"object"}},["network_id","summary"]),read=False,idem=False),
tool("list_networks","List open session networks.",obj({}),obj({"networks":{"type":"array","items":{"type":"object"}}},["networks"]),read=True),
tool("copy_network","Create an independent in-memory alternative.",obj({"network_id":NETWORK,"label":{"type":"string","minLength":1,"default":"alternative","examples":["redispatch alternative"]}},["network_id"]),obj({"network_id":NETWORK,"summary":{"type":"object"}},["network_id","summary"]),read=False,idem=False),
tool("close_network","Close a session network without deleting persisted results.",obj({"network_id":NETWORK},["network_id"]),obj({"closed":{"type":"boolean"}},["closed"]),read=False,destructive=True,idem=True),
tool("network_summary","Read network metadata and element counts.",obj({"network_id":NETWORK},["network_id"]),obj({"summary":{"type":"object"}},["summary"]),read=True),
tool("list_elements","List network elements with optional type, column selection, and pagination.",obj({"network_id":NETWORK,"kind":{"type":"string","minLength":1},"columns":{"type":"array","items":{"type":"string"},"maxItems":30},"cursor":CURSOR,"limit":PAGE},["network_id"]),obj({"elements":{"type":"array","items":{"type":"object"}},"nextCursor":{"type":["string","null"]}},["elements"]),read=True),
tool("get_element","Read one element by stable identifier.",obj({"network_id":NETWORK,"id":{"type":"string","minLength":1,"examples":["B01"]}},["network_id","id"]),obj({"element":{"type":"object"}},["element"]),read=True),
tool("set_element","Change explicit editable attributes in the selected session network.",obj({"network_id":NETWORK,"id":{"type":"string","minLength":1},"attrs":{"type":"object","minProperties":1,"additionalProperties":True}},["network_id","id","attrs"]),obj({"updated":{"type":"boolean"}},["updated"]),read=False,idem=False),
tool("switch_element","Open or close a declared switch.",obj({"network_id":NETWORK,"id":{"type":"string","minLength":1},"closed":{"type":"boolean","examples":[True]}},["network_id","id","closed"]),obj({"updated":{"type":"boolean"}},["updated"]),read=False,idem=True),
tool("study_capabilities","Preflight a study and report missing data before calculation.",obj({"network_id":NETWORK,"study":{"type":"string","enum":["loadflow","shortcircuit","contingency","pv_curve","qv_curve","sensitivities","rms","cct","quasi_dynamic","batch"]}},["network_id","study"]),obj({"study":{"type":"string"},"evaluable":{"type":"boolean"},"missing_data":{"type":"array","items":{"type":"object"}}},["study","evaluable","missing_data"]),read=True),
tool("run_loadflow","Run AC load flow. Voltages are per unit and powers are MW or Mvar.",obj({"network_id":NETWORK,"q_limits":{"type":"boolean","default":False}},["network_id"]),STUDY,read=False,idem=True),
tool("run_contingency","Run N-1 or N-2 static contingencies. Severity ranks unsupplied MW, loading, then voltage deviation.",obj({"network_id":NETWORK,"elements":{"type":"array","items":{"type":"string"},"minItems":1},"criterion":{"type":"string","enum":["N-1","N-2"],"default":"N-1"},"max_cases":{"type":"integer","minimum":1,"maximum":10000,"default":100},"island_reference":{"type":"string","enum":["largest_generator","none"],"default":"largest_generator"},"progressToken":{}},["network_id"]),STUDY,read=False,idem=True),
tool("run_shortcircuit","Run a single fault with explicit short-circuit source data.",obj({"network_id":NETWORK,"bus":{"type":"string","minLength":1},"fault":{"type":"string","enum":["3ph","ll","lg","llg"],"default":"3ph"},"method":{"type":"string","enum":["IEC60909","iec60909"],"default":"IEC60909"},"sc_data":{"type":"object","minProperties":1}},["network_id","bus","sc_data"]),STUDY,read=False,idem=True),
tool("run_pv_curve","Run a PV curve with explicit study inputs.",obj({"network_id":NETWORK,"inputs":{"type":"object","minProperties":1}},["network_id","inputs"]),STUDY,read=False,idem=True),
tool("run_qv_curve","Run a QV curve with explicit study inputs.",obj({"network_id":NETWORK,"inputs":{"type":"object","minProperties":1}},["network_id","inputs"]),STUDY,read=False,idem=True),
tool("run_sensitivities","Run local AC sensitivities for an injection bus.",obj({"network_id":NETWORK,"injection_bus_id":{"type":"string","minLength":1},"delta_p_mw":{**POS,"default":1.0,"description":"Active-power perturbation in MW."}},["network_id","injection_bus_id"]),STUDY,read=False,idem=True),
tool("run_rms","Run RMS dynamics. Time values are seconds.",obj({"network_id":NETWORK,"events":{"type":"array","items":{"type":"object"}},"t_end_s":{**POS,"maximum":3600},"step_s":{"type":"number","exclusiveMinimum":0,"maximum":60},"linear_solver":{"type":"string","enum":["compatibility","numpy","scipy","auto"],"default":"compatibility"},"retry_steps":{"type":"boolean","default":False}},["network_id","events","t_end_s","step_s"]),STUDY,read=False,idem=True),
tool("run_cct","Run critical clearing time for an explicit fault.",obj({"network_id":NETWORK,"fault":{"type":"object","minProperties":1},"options":{"type":"object","default":{}}},["network_id","fault"]),STUDY,read=False,idem=True),
tool("run_quasi_dynamic","Run a sequential quasi-dynamic user Series.",obj({"network_id":NETWORK,"series":obj({"name":{"type":"string","minLength":1},"steps":{"type":"array","minItems":1,"items":obj({"timestamp_iso":{"type":"string","minLength":1},"t_s":{"type":"number"},"factors":{"type":"object","minProperties":1,"additionalProperties":{"type":"number"}}},["timestamp_iso","t_s","factors"])},"provenance":{"type":"object","default":{}}},["name","steps"]),"options":{"type":"object","default":{}}},["network_id","series"]),STUDY,read=False,idem=True),
tool("run_batch","Run supported scenarios with batch execution and optional progress notifications.",obj({"network_id":NETWORK,"study":{"type":"string","enum":["loadflow","contingency"]},"scenarios":{"type":"array","minItems":1,"items":{"type":"object"}},"study_options":{"type":"object","default":{}},"options":{"type":"object","default":{}},"progressToken":{}},["network_id","study","scenarios"]),STUDY,read=False,idem=True),
tool("get_result","Read a stored study result with row filters, columns, and pagination.",obj({"result_id":{"type":"string","minLength":1},"cursor":CURSOR,"limit":PAGE,"columns":{"type":"array","items":{"type":"string"},"maxItems":30},"filter":{"type":"object","additionalProperties":True}},["result_id"]),obj({"result_id":{"type":"string"},"rows":{"type":"array","items":{"type":"object"}},"nextCursor":{"type":["string","null"]}},["result_id","rows"]),read=True),
tool("compare_results","Compare two stored study results without declaring a mitigation feasible.",obj({"before_result_id":{"type":"string","minLength":1},"after_result_id":{"type":"string","minLength":1}},["before_result_id","after_result_id"]),obj({"before_result_id":{"type":"string"},"after_result_id":{"type":"string"},"changes":{"type":"object"}},["before_result_id","after_result_id","changes"]),read=True),
]
def validate(schema:dict[str,Any], value:Any, path:str="$")->Any:
    if value is None and "default" in schema: return schema["default"]
    types=schema.get("type"); allowed=types if isinstance(types,list) else [types]
    ok=not types or any((t=="object" and isinstance(value,dict)) or (t=="array" and isinstance(value,list)) or (t=="string" and isinstance(value,str)) or (t=="boolean" and isinstance(value,bool)) or (t=="number" and isinstance(value,(int,float)) and not isinstance(value,bool)) or (t=="integer" and isinstance(value,int) and not isinstance(value,bool)) or (t=="null" and value is None) for t in allowed)
    if not ok: raise ValueError({"code":"invalid_input","message":f"{path} has an invalid type."})
    if "enum" in schema and value not in schema["enum"]: raise ValueError({"code":"invalid_input","message":f"{path} is not an allowed value."})
    if isinstance(value,(int,float)):
        if "minimum" in schema and value<schema["minimum"] or "exclusiveMinimum" in schema and value<=schema["exclusiveMinimum"]: raise ValueError({"code":"invalid_input","message":f"{path} is below its minimum."})
        if "maximum" in schema and value>schema["maximum"]: raise ValueError({"code":"invalid_input","message":f"{path} exceeds its maximum."})
    if isinstance(value,str) and len(value)<schema.get("minLength",0): raise ValueError({"code":"invalid_input","message":f"{path} is empty."})
    if isinstance(value,list):
        if len(value)<schema.get("minItems",0) or len(value)>schema.get("maxItems",10**9): raise ValueError({"code":"invalid_input","message":f"{path} has an invalid item count."})
        return [validate(schema.get("items",{}),v,f"{path}[{i}]") for i,v in enumerate(value)]
    if isinstance(value,dict):
        props=schema.get("properties",{}); unknown=set(value)-set(props)
        if schema.get("additionalProperties") is False and unknown: raise ValueError({"code":"invalid_input","message":f"{path} has unknown properties."})
        result=dict(value)
        for key in schema.get("required",[]):
            if key not in result: raise ValueError({"code":"invalid_input","message":f"{path}.{key} is required."})
        for key,sub in props.items():
            if key in result: result[key]=validate(sub,result[key],f"{path}.{key}")
            elif "default" in sub: result[key]=sub["default"]
        return result
    return value
def safe(value:Any)->Any:
    if hasattr(value,"to_dict"): return safe(value.to_dict())
    if isinstance(value,dict): return {str(k):safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [safe(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value): return None
    return value
class ToolDispatcher:
    def __init__(self, sessions:SessionStore, progress:Callable[[Any,float,float,str|None],None]|None=None):
        self.sessions=sessions; self.progress=progress; self.defs={x["name"]:x for x in TOOL_DEFINITIONS}
    def _api(self):
        from power_machine import api
        return api
    def _network(self,a): return self.sessions.get_network(a["network_id"])
    def _summary(self, study:str, result:dict[str,Any], rid:str)->dict[str,Any]:
        payload=result.get("payload",result); table=result.get("cases"); cases=table.get("records",[]) if isinstance(table,dict) else (payload.get("cases",[]) if isinstance(payload,dict) else [])
        ranked=sorted((x for x in cases if isinstance(x,dict)),key=lambda x:(-float(x.get("metrics",{}).get("newly_unsupplied_p_mw",0) or 0),-float(x.get("metrics",{}).get("severity",0) or 0),x.get("case_id","")))[:5]
        coverage=result.get("coverage",payload.get("coverage",{}) if isinstance(payload,dict) else {})
        return {"result_id":rid,"study":study,"status":str(result.get("status","unknown")),"coverage":safe(coverage if isinstance(coverage,dict) else {}),"worst_cases":safe(ranked)}
    def _study(self,a,study,fn,*args,**kwargs):
        result=safe(fn(self._network(a),*args,**kwargs)); rid=self.sessions.save_result(a["network_id"],study,result); return {"summary":self._summary(study,result,rid)}
    def call(self,name:str,args:Any,progress_token:Any=None)->dict[str,Any]:
        if name not in self.defs: raise ValueError({"code":"unknown_tool","message":"Unknown tool."})
        a=validate(self.defs[name]["inputSchema"],{} if args is None else args)
        try:
            api=self._api()
            if name=="list_cases": value={"cases":api.list_cases()}
            elif name=="load_case":
                net=api.load_case(a["name_or_path"]); value={"network_id":self.sessions.open_network(net,a["name_or_path"]),"summary":safe(net.summary())}
            elif name=="list_networks": value={"networks":self.sessions.list_networks()}
            elif name=="copy_network":
                net=self._network(a).copy(); value={"network_id":self.sessions.open_network(net,a["label"]),"summary":safe(net.summary())}
            elif name=="close_network": value={"closed":self.sessions.close_network(a["network_id"])}
            elif name=="network_summary": value={"summary":safe(self._network(a).summary())}
            elif name=="list_elements":
                rows=safe(self._network(a).elements(a.get("kind"))); value=self._page(rows,a)
                value["elements"]=value.pop("rows")
            elif name=="get_element": value={"element":safe(self._network(a).get(a["id"]))}
            elif name=="set_element": self._network(a).set(a["id"],**a["attrs"]); value={"updated":True}
            elif name=="switch_element": self._network(a).switch(a["id"],a["closed"]); value={"updated":True}
            elif name=="study_capabilities":
                cap=getattr(api,"study_capabilities",None); value=safe(cap(self._network(a),a["study"])) if cap else {"study":a["study"],"evaluable":False,"missing_data":[{"code":"capability_api_unavailable"}]}
            elif name=="run_loadflow": value=self._study(a,"loadflow",api.loadflow,options=api.LoadflowOptions(q_limits=a["q_limits"]))
            elif name=="run_contingency": value=self._study(a,"contingency",api.contingency,elements=a.get("elements"),criterion=a["criterion"],options=api.ContingencyOptions(max_cases=a["max_cases"],island_reference=a["island_reference"]))
            elif name=="run_shortcircuit": value=self._study(a,"shortcircuit",api.shortcircuit,bus=a["bus"],fault=a["fault"],options=api.ShortCircuitOptions(method=a["method"].lower()),sc_data=a["sc_data"])
            elif name=="run_pv_curve": value=self._study(a,"pv_curve",api.pv_curve,inputs=a["inputs"])
            elif name=="run_qv_curve": value=self._study(a,"qv_curve",api.qv_curve,inputs=a["inputs"])
            elif name=="run_sensitivities": value=self._study(a,"sensitivities",api.sensitivities,a["injection_bus_id"],delta_p_mw=a["delta_p_mw"])
            elif name=="run_rms": value=self._study(a,"rms",api.rms,a["events"],t_end_s=a["t_end_s"],step_s=a["step_s"],options=api.RmsOptions(linear_solver=a["linear_solver"],retry_steps=a["retry_steps"]))
            elif name=="run_cct": value=self._study(a,"cct",api.cct,a["fault"],options=api.CctOptions(**a["options"]))
            elif name=="run_quasi_dynamic":
                from power_machine.qss.series import Series, SeriesStep
                source=a["series"]
                series=Series(source["name"],tuple(SeriesStep(**row) for row in source["steps"]),source.get("provenance",{}))
                value=self._study(a,"quasi_dynamic",api.quasi_dynamic,series,options=api.QuasiDynamicOptions(**a["options"]))
            elif name=="run_batch":
                from power_machine.batch import run_batch
                from power_machine.batch import BatchOptions
                batch=run_batch(self._network(a),a["study"],a["scenarios"],study_options=a["study_options"],options=BatchOptions(**a["options"]),progress=lambda item:self.progress(progress_token,item.get("completed",0),item.get("total",1),item.get("code")) if self.progress else None)
                raw={"status":"completed","coverage":{"evaluated":len(batch.to_records()),"complete":all(row.get("status") in {"completed","limit_violation"} for row in batch.to_records())},"cases":{"records":batch.to_records()},"output":batch.output}
                rid=self.sessions.save_result(a["network_id"],"batch",raw); value={"summary":self._summary("batch",raw,rid)}
            elif name=="get_result": value=self._page(self._rows(self.sessions.get_result(a["result_id"]).result),a); value["result_id"]=a["result_id"]
            else:
                before=self.sessions.get_result(a["before_result_id"]).result; after=self.sessions.get_result(a["after_result_id"]).result
                value={"before_result_id":a["before_result_id"],"after_result_id":a["after_result_id"],"changes":{"before_status":before.get("status"),"after_status":after.get("status")}}
            return {"content":[{"type":"text","text":json.dumps(value,ensure_ascii=False,allow_nan=False)}],"structuredContent":value}
        except Exception as exc:
            err=exc.args[0] if exc.args and isinstance(exc.args[0],dict) else {"code":"unsupported_study" if type(exc).__name__=="UnsupportedStudyError" else "calculation_error","message":str(exc) or type(exc).__name__}
            return {"content":[{"type":"text","text":json.dumps(err,ensure_ascii=False)}],"structuredContent":err,"isError":True}
    def _rows(self,result):
        for name in ("cases","buses","faults","samples","records"):
            table=result.get(name)
            if isinstance(table,dict) and isinstance(table.get("records"),list): return table["records"]
        payload=result.get("payload",result)
        return payload.get("cases",[]) if isinstance(payload,dict) and isinstance(payload.get("cases"),list) else [result]
    def _page(self,rows,a):
        start=int(a.get("cursor","0")); limit=a.get("limit",100); rows=rows[start:start+limit]
        columns=a.get("columns")
        if columns: rows=[{k:r.get(k) for k in columns if k in r} for r in rows]
        return {"rows":rows,"nextCursor":str(start+limit) if len(rows)==limit else None}
