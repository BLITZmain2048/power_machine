"""Prueba de punta a punta del protocolo MCP por subproceso."""

from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]


class McpClient:
    """Cliente mínimo para intercambiar mensajes JSON-RPC por líneas."""

    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "power_machine.mcp"],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)

    def request(self, identifier, method, params=None):
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        response = json.loads(self.lines.get(timeout=30))
        assert response["id"] == identifier
        assert "error" not in response
        return response["result"]

    def notify_initialized(self):
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.process.stdin.flush()

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        self.process.wait(timeout=10)


def _content_json(result):
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    content = result.get("content", [])
    assert content
    return json.loads(content[0]["text"])


def test_mcp_initialize_list_load_case_and_run_loadflow():
    client = McpClient()
    try:
        initialized = client.request(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
        assert initialized["protocolVersion"] == "2025-11-25"
        client.notify_initialized()
        tools = client.request(2, "tools/list")
        names = {tool["name"] for tool in tools["tools"]}
        assert {"load_case", "run_loadflow"} <= names
        loaded = _content_json(client.request(3, "tools/call", {"name": "load_case", "arguments": {"name_or_path": "ieee39"}}))
        network_id = loaded["network_id"]
        loadflow = _content_json(client.request(4, "tools/call", {"name": "run_loadflow", "arguments": {"network_id": network_id}}))
        json.dumps(loadflow, allow_nan=False)
    finally:
        client.close()
