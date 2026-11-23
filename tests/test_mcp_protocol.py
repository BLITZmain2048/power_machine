from power_machine.mcp.server import McpServer

def _open(server):
    return server.handle({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"load_case","arguments":{"name_or_path":"ieee39"}}})["result"]["structuredContent"]["network_id"]

def test_initialize_advertises_tools_resources_and_prompts():
    result=McpServer().handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}})["result"]
    assert {"tools","resources","prompts"} <= set(result["capabilities"])

def test_schema_rejects_unknown_arguments():
    result=McpServer().handle({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_cases","arguments":{"bad":1}}})
    assert result["error"]["code"] == -32602

def test_run_cct_ieee39_returns_a_result():
    server=McpServer(); network_id=_open(server)
    result=server.handle({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"run_cct","arguments":{"network_id":network_id,"fault":{"engine":"ieee39_classical","fault_bus_id":"B16"},"options":{"lower_clearing_s":0.16,"upper_clearing_s":0.1775,"resolution_ms":20.0}}}})["result"]
    assert result.get("isError") is not True
    assert result["structuredContent"]["summary"]["study"] == "cct"

def test_run_quasi_dynamic_three_steps(tmp_path):
    server=McpServer(); network_id=_open(server)
    loads=[row["id"] for row in server.sessions.get_network(network_id).elements("loads")]
    factors={identifier:1.0 for identifier in loads}
    steps=[{"timestamp_iso":f"2026-11-18T00:{index:02d}:00","t_s":float(index*60),"factors":factors} for index in range(3)]
    result=server.handle({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"run_quasi_dynamic","arguments":{"network_id":network_id,"series":{"name":"mcp-short","steps":steps},"options":{"output":str(tmp_path / "qss")}}}})["result"]
    assert result.get("isError") is not True
    assert result["structuredContent"]["summary"]["study"] == "quasi_dynamic"
