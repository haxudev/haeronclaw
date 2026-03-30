import json
import logging
import os
from typing import Any, Dict, Optional

from copilot import MCPLocalServerConfig, MCPRemoteServerConfig, MCPServerConfig

_MCP_SERVERS_CACHE: Optional[Dict[str, MCPServerConfig]] = None


def _parse_mcp_server_config(server: Dict[str, Any]) -> Optional[MCPServerConfig]:
    server_type = str(server.get("type", "")).lower()

    if "command" in server or server_type == "local":
        local_config: MCPLocalServerConfig = {
            "type": "local",
            "command": str(server.get("command", "")),
            "args": server.get("args", []),
            "env": server.get("env", {}),
            "tools": server.get("tools", ["*"]),
        }
        if not local_config["command"]:
            return None
        return local_config

    if "url" in server or server_type in {"http", "sse"}:
        remote_type = server_type if server_type in {"http", "sse"} else "http"
        remote_config: MCPRemoteServerConfig = {
            "type": remote_type,  # type: ignore
            "url": str(server.get("url", "")),
            "headers": server.get("headers"),
            "tools": server.get("tools", ["*"]),
        }
        if not remote_config["url"]:
            return None
        return remote_config

    return None


def _load_mcp_servers_from_file() -> Dict[str, MCPServerConfig]:
    # Cold start in Azure Functions may run with a standby CWD.
    # Prefer resolving relative to this module's package directory.
    function_app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(function_app_dir, ".vscode", "mcp.json"),
        os.path.join(function_app_dir, "mcp.json"),
        os.path.join(os.getcwd(), ".vscode", "mcp.json"),
        os.path.join(os.getcwd(), "mcp.json"),
    ]

    for path in candidates:
        if not os.path.exists(path):
            continue

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logging.warning(f"Failed to read MCP config from {path}: {e}")
            continue

        servers = data.get("servers", {})
        if not isinstance(servers, dict):
            logging.warning(f"Invalid MCP config in {path}: 'servers' must be an object")
            return {}

        parsed_servers: Dict[str, MCPServerConfig] = {}
        for name, config in servers.items():
            if not isinstance(name, str) or not isinstance(config, dict):
                continue
            parsed = _parse_mcp_server_config(config)
            if parsed is not None:
                parsed_servers[name] = parsed

        if parsed_servers:
            logging.info(f"Loaded {len(parsed_servers)} MCP server(s) from {path}")
        else:
            logging.info(f"No valid MCP servers found in {path}")
        return parsed_servers

    return {}


def get_cached_mcp_servers() -> Dict[str, MCPServerConfig]:
    global _MCP_SERVERS_CACHE
    if _MCP_SERVERS_CACHE is None:
        _MCP_SERVERS_CACHE = _load_mcp_servers_from_file()
    return _MCP_SERVERS_CACHE
