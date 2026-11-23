"""Line-delimited JSON-RPC MCP server."""
from __future__ import annotations
import json, sys
from typing import Any
from power_machine import __version__
from .session import SessionStore
from .tools import TOOL_DEFINITIONS, ToolDispatcher
VERSIONS=("2025-11-25","2025-06-18","2025-03-26","2024-11-05")
def response(i:Any,r:Any): return {"jsonrpc":"2.0","id":i,"result":r}
def error(i:Any,c:int,m:str,d:Any=None):
    e={"code":c,"message":m}
    if d is not None:e["data"]=d
    return {"jsonrpc":"2.0","id":i,"error":e}
class McpServer:
    """Serve tools, resources, and prompts over stdio."""
    def __init__(self):
        self.sessions=SessionStore(); self.tools=ToolDispatcher(self.sessions,self.progress)
    def progress(self, token:Any, progress:float, total:float, message:str|None=None):
        if token is not None:
            params={"progressToken":token,"progress":progress,"total":total}
            if message:params["message"]=message
            print(json.dumps({"jsonrpc":"2.0","method":"notifications/progress","params":params}),flush=True)
    def handle(self,q:Any):
        if not isinstance(q,dict) or q.get("jsonrpc")!="2.0" or not isinstance(q.get("method"),str): return error(None,-32600,"Invalid JSON-RPC request.")
        notification="id" not in q
        if notification:return None
        i=q["id"]; p=q.get("params",{})
        if not isinstance(p,dict):return error(i,-32602,"params must be an object.")
        try:
            m=q["method"]
            if m=="initialize":
                v=p.get("protocolVersion"); v=v if v in VERSIONS else VERSIONS[0]
                return response(i,{"protocolVersion":v,"capabilities":{"tools":{"listChanged":False},"resources":{"subscribe":False,"listChanged":False},"prompts":{"listChanged":False}},"serverInfo":{"name":"power_machine","version":__version__},"instructions":"Load a case, inspect capabilities, run a study, retrieve its result, and compare alternatives without inventing missing electrical data."})
            if m=="ping":return response(i,{})
            if m=="tools/list":return response(i,{"tools":TOOL_DEFINITIONS})
            if m=="tools/call":return response(i,self.tools.call(p.get("name",""),p.get("arguments",{}),p.get("_meta",{}).get("progressToken")))
            if m=="resources/list":return response(i,{"resources":[{"uri":"power-machine://cases/ieee39","name":"IEEE 39 case","mimeType":"application/json"},{"uri":"power-machine://schema/network","name":"Network schema","mimeType":"application/json"}]})
            if m=="resources/templates/list":return response(i,{"resourceTemplates":[{"uriTemplate":"power-machine://networks/{network_id}/summary","name":"Network summary"},{"uriTemplate":"power-machine://results/{result_id}","name":"Study result"}]})
            if m=="resources/read":return response(i,self.resource(p["uri"]))
            if m=="prompts/list":return response(i,{"prompts":[{"name":n,"description":d,"arguments":[{"name":"network_id","required":False}]} for n,d in [("study_n1_contingencies","Study N-1 severity and coverage."),("compare_operating_alternatives","Compare independent operating alternatives."),("explain_missing_data","Explain missing study data without filling values.")]]})
            if m=="prompts/get":return response(i,self.prompt(p["name"],p.get("arguments",{})))
            return error(i,-32601,"Method not found.")
        except (KeyError,ValueError) as exc:return error(i,-32602,"Invalid parameters.",exc.args[0] if exc.args else None)
        except Exception as exc:return error(i,-32603,"Internal server error.",{"code":"internal_error","message":str(exc)})
    def resource(self,uri:str):
        if uri=="power-machine://cases/ieee39":data={"case":"ieee39","units":{"voltage":"p.u.","active_power":"MW","reactive_power":"Mvar"}}
        elif uri=="power-machine://schema/network":data={"type":"object","description":"Power Machine network document. Use list_elements and get_element for inspection."}
        elif uri.startswith("power-machine://networks/") and uri.endswith("/summary"):
            data=self.sessions.get_network(uri.split("/")[3]).summary().to_dict()
        elif uri.startswith("power-machine://results/"):
            data=self.sessions.get_result(uri.split("/")[-1].split("?")[0]).result
        else:raise ValueError({"code":"unknown_resource","message":"Unknown resource URI."})
        return {"contents":[{"uri":uri,"mimeType":"application/json","text":json.dumps(data,ensure_ascii=False,allow_nan=False)}]}
    def prompt(self,name:str,args:dict[str,Any]):
        texts={"study_n1_contingencies":"Define severity as unsupplied MW, then loading, then voltage deviation. Report evaluated, omitted, failed, and total cases. If coverage is partial, say most severe among evaluated cases. Do not invent missing electrical data.","compare_operating_alternatives":"Copy the base network before every alternative. Compare before and after results, constraints, coverage, and diagnostics. An alternative is not optimal or dynamically secure unless evidence establishes it.","explain_missing_data":"Explain missing data by study and element. State the required units and source. Do not fill absent electrical parameters."}
        if name not in texts:raise ValueError({"code":"unknown_prompt","message":"Unknown prompt."})
        return {"messages":[{"role":"user","content":{"type":"text","text":texts[name]+(f" Network: {args['network_id']}." if args.get("network_id") else "")}}]}
def main():
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    server=McpServer()
    for line in sys.stdin:
        try:q=json.loads(line); r=server.handle(q)
        except json.JSONDecodeError:r=error(None,-32700,"Parse error.")
        if r is not None:print(json.dumps(r,ensure_ascii=False,allow_nan=False),flush=True)
if __name__=="__main__":main()
