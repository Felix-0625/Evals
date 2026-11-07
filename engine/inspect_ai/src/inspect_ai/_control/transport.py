"""Resolve owner-local control transports without accepting remote addresses."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ControlConnection:
    base_url: str
    headers: dict[str, str]
    uds: str | None


def control_connection(path: Path) -> ControlConnection:
    """Read a Windows credential sidecar, or use the original Unix socket."""
    if path.suffix != ".tcp":
        return ControlConnection(base_url="http://localhost", headers={}, uds=str(path))
    record = json.loads(path.read_text(encoding="utf-8"))
    port, token = record.get("port"), record.get("token")
    if (record.get("protocol") != "inspect-control-tcp-v1" or
            type(port) is not int or not 1 <= port <= 65535 or
            not isinstance(token, str) or len(token) != 64 or
            any(char not in "0123456789abcdef" for char in token)):
        raise ValueError("Invalid local Inspect control transport record")
    return ControlConnection(base_url=f"http://127.0.0.1:{port}",
                             headers={"Authorization": f"Bearer {token}"}, uds=None)
