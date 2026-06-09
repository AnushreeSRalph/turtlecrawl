"""
mcp_client.py — Synchronous MCP client for turtlecrawl.

Communicates with the Go MCP server via stdin/stdout JSON-RPC 2.0.
The server is spawned as a subprocess using the collector binary.

Protocol: Model Context Protocol 2024-11-05
Transport: newline-delimited JSON over subprocess stdio

"""
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class MCPError(Exception):
    """Raised when the MCP server returns a JSON-RPC error."""
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(f"MCP error {code}: {message}")


class MCPClient:
    """
    Synchronous MCP client that communicates with a Go MCP server via stdio.

    The server binary is spawned on __enter__ and terminated on __exit__.
    All calls are blocking; the lock prevents concurrent JSON-RPC calls from
    interleaving on the wire.
    """

    def __init__(self, server_binary: str, *extra_args: str):
        self._binary = server_binary
        self._extra_args = list(extra_args)
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *_):
        self.close()

    def start(self) -> None:
        """Spawn the MCP server subprocess and run the initialize handshake."""
        cmd = [self._binary, "mcp-server"] + self._extra_args
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._initialize()

    def close(self) -> None:
        """Terminate the server subprocess cleanly."""
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None

    # ── JSON-RPC transport ────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send_request(self, method: str, params: dict | None = None) -> Any:
        """
        Send a JSON-RPC request and block until the matching response arrives.
        Thread-safe via _lock.
        """
        if self._proc is None:
            raise RuntimeError("MCPClient: server not started")

        with self._lock:
            req_id = self._next_id()
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params

            line = json.dumps(msg) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()

            # Read until we get the response for our request ID
            while True:
                raw = self._proc.stdout.readline()
                if not raw:
                    # Server closed stdout — check stderr for diagnostic info
                    stderr = self._proc.stderr.read()
                    raise RuntimeError(
                        f"MCPClient: server closed stdout. stderr: {stderr!r}"
                    )

                raw = raw.strip()
                if not raw:
                    continue

                try:
                    resp = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"MCPClient: invalid JSON from server: {e}  raw={raw!r}")

                if resp.get("id") != req_id:
                    # Skip notifications or responses for other requests
                    continue

                if "error" in resp:
                    err = resp["error"]
                    raise MCPError(err.get("code", -1), err.get("message", "unknown"))

                return resp.get("result", {})

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        if self._proc is None:
            return
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    # ── MCP protocol ──────────────────────────────────────────────────────────

    def _initialize(self) -> None:
        """Run the MCP initialize handshake."""
        result = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "turtlecrawl-agent",
                "version": "2.0.0",
            },
        })
        # Confirm the server's declared protocol version
        server_version = result.get("protocolVersion", "unknown")
        server_name = result.get("serverInfo", {}).get("name", "unknown")
        # Send initialized notification (required by MCP spec)
        self._send_notification("notifications/initialized")
        self._server_info = {"name": server_name, "version": server_version}

    def list_tools(self) -> list[dict]:
        """Return the list of tools the server exposes."""
        result = self._send_request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """
        Call a named tool and return its result as a parsed Python object.

        The MCP server returns content as an array of typed items.
        We extract the first text item and parse it as JSON.
        """
        result = self._send_request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

        is_error = result.get("isError", False)
        content = result.get("content", [])

        for item in content:
            if item.get("type") == "text":
                text = item["text"]
                try:
                    parsed = json.loads(text)
                    if is_error and isinstance(parsed, dict) and "error" not in parsed:
                        parsed["_mcp_error"] = True
                    return parsed
                except json.JSONDecodeError:
                    return {"result": text}

        return {}

    @property
    def server_info(self) -> dict:
        """Return the server's name and version from the initialize response."""
        return getattr(self, "_server_info", {})
