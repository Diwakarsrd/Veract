"""Minimal MCP client (stdio transport, JSON-RPC 2.0, newline-delimited) — stdlib only.

Servers come from `.agent/policy.json` -> "mcp": {"name": {"command": [...], "env": {...}}}.
Each server tool becomes a runtime tool named `mcp.<server>.<tool>`. Every call goes through the broker as
action "mcp" on "<server>.<tool>": allowed by `mcp_allow` patterns, otherwise it needs approval (and is audited).
Note: the server process itself runs with the user's privileges, outside the command sandbox — only add
servers you trust.
"""
import atexit
import json
import subprocess
import threading
import itertools
from .tools import Tool, safe_env, redact

PROTOCOL = "2025-06-18"
_LIVE = []


def _close_all():
    for c in list(_LIVE):
        c.close()


atexit.register(_close_all)


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, name, command, env=None, cwd=None, timeout=30):
        if not isinstance(command, list) or not command:
            raise MCPError(f"mcp.{name}: 'command' must be a non-empty list")
        self.name, self.timeout = name, timeout
        e = safe_env()
        e.update({k: str(v) for k, v in (env or {}).items()})
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                                     env=e, cwd=cwd)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        _LIVE.append(self)
        init = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                           "clientInfo": {"name": "agent-runtime", "version": "0.3.0"}})
        self.server_info = init.get("serverInfo", {})
        self.notify("notifications/initialized")

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params=None):
        with self._lock:
            if self.proc.poll() is not None:
                raise MCPError(f"mcp.{self.name}: server exited ({self.proc.returncode})")
            rid = next(self._ids)
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            result = {}

            def read():
                for line in self.proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue                       # servers sometimes log to stdout; skip
                    if msg.get("id") == rid:
                        result["msg"] = msg
                        return
            t = threading.Thread(target=read, daemon=True)
            t.start()
            t.join(self.timeout)
            if "msg" not in result:
                self.close()                           # a late reply would desync the stream: drop the server
                raise MCPError(f"mcp.{self.name}: no response to {method} within {self.timeout}s")
            msg = result["msg"]
            if "error" in msg:
                raise MCPError(f"mcp.{self.name}: {method} failed: {msg['error'].get('message', msg['error'])}")
            return msg.get("result") or {}

    def list_tools(self):
        tools, cursor = [], None
        while True:
            r = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += r.get("tools", [])
            cursor = r.get("nextCursor")
            if not cursor:
                return tools

    def call(self, tool, arguments):
        return self.request("tools/call", {"name": tool, "arguments": arguments})

    def close(self):
        if self in _LIVE:
            _LIVE.remove(self)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _make_fn(client, tool_name):
    def fn(args, ctx):
        target = f"{client.name}.{tool_name}"
        ctx.require("mcp", target)
        r = client.call(tool_name, {k: v for k, v in args.items()})
        texts = [c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"]
        out = redact("\n".join(texts))[:20000]
        if r.get("isError"):
            raise RuntimeError(f"mcp {target} returned an error: {out[:300]}")
        return {"output": out, "items": [{"value": out[:1500], "source": f"mcp:{target}", "key": target}],
                "sources": [f"mcp:{target}"], "files": []}
    return fn


def load_servers(config, registry, workspace=None, on_event=None):
    """Start configured servers and register their tools. A broken server is reported, never fatal."""
    clients = []
    for name, spec in (config or {}).items():
        try:
            c = MCPClient(name, spec.get("command"), spec.get("env"), cwd=str(workspace) if workspace else None,
                          timeout=int(spec.get("timeout", 30)))
            tools = c.list_tools()
            for t in tools:
                schema = t.get("inputSchema") or {}
                tname = f"mcp.{name}.{t['name']}"
                registry[tname] = Tool(tname, (t.get("description") or "")[:200],
                                       json.dumps(schema.get("properties", {}))[:300], _make_fn(c, t["name"]),
                                       tuple(schema.get("required", [])))
            clients.append(c)
            if on_event:
                on_event(f"mcp server '{name}' loaded ({len(tools)} tools)")
        except Exception as e:
            if on_event:
                on_event(f"mcp server '{name}' failed to start: {e}")
    return clients
