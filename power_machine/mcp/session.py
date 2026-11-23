"""In-memory MCP session state for networks and study results."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
import uuid


@dataclass
class NetworkEntry:
    """A network retained for the lifetime of one MCP server process."""

    network: Any
    label: str
    created_at: float


@dataclass
class ResultEntry:
    """A complete study result that can be retrieved through MCP."""

    result: dict[str, Any]
    network_id: str
    study: str
    created_at: float
    path: Path


class SessionStore:
    """Owns bounded network and result state for a single MCP connection."""

    def __init__(self, network_limit: int | None = None) -> None:
        configured = network_limit if network_limit is not None else os.environ.get("POWER_MACHINE_MCP_NETWORK_LIMIT", "8")
        try:
            self.network_limit = max(1, int(configured))
        except (TypeError, ValueError):
            self.network_limit = 8
        self._networks: OrderedDict[str, NetworkEntry] = OrderedDict()
        self._results: OrderedDict[str, ResultEntry] = OrderedDict()

    @staticmethod
    def output_directory() -> Path:
        """Return the configured durable directory for MCP result documents."""
        configured = os.environ.get("POWER_MACHINE_OUTPUT_DIR")
        directory = Path(configured) if configured else Path(tempfile.gettempdir()) / "power_machine_mcp"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def open_network(self, network: Any, label: str | None = None) -> str:
        """Register a network and return a session-local opaque identifier."""
        if len(self._networks) >= self.network_limit:
            raise ValueError({"code": "network_limit_reached", "message": "Close a network before opening another one."})
        network_id = uuid.uuid4().hex
        self._networks[network_id] = NetworkEntry(network, label or getattr(network, "name", "network"), time.time())
        return network_id

    def get_network(self, network_id: str) -> Any:
        """Return an open network and record it as recently used."""
        try:
            entry = self._networks.pop(network_id)
        except KeyError as exc:
            raise ValueError({"code": "unknown_network", "message": "network_id is unknown or closed."}) from exc
        self._networks[network_id] = entry
        return entry.network

    def list_networks(self) -> list[dict[str, Any]]:
        """List currently open networks without exposing implementation objects."""
        return [
            {"network_id": network_id, "label": entry.label, "created_at_unix_s": entry.created_at}
            for network_id, entry in self._networks.items()
        ]

    def close_network(self, network_id: str) -> bool:
        """Release an open network and leave its persisted results readable."""
        return self._networks.pop(network_id, None) is not None

    def save_result(self, network_id: str, study: str, result: dict[str, Any]) -> str:
        """Persist a complete JSON result and retain it in the active session."""
        result_id = uuid.uuid4().hex
        path = self.output_directory() / f"mcp_result_{result_id}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        self._results[result_id] = ResultEntry(result, network_id, study, time.time(), path)
        return result_id

    def get_result(self, result_id: str) -> ResultEntry:
        """Return a previously stored study result."""
        try:
            return self._results[result_id]
        except KeyError as exc:
            raise ValueError({"code": "unknown_result", "message": "result_id is unknown in this session."}) from exc

    def close_all(self) -> None:
        """Release process-local references without deleting persisted study results."""
        self._networks.clear()
        self._results.clear()
