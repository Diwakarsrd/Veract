"""v0.3: configuration, approval flow, search providers, MCP tools, sandboxed commands, schema migration, CLI."""
import io
import json
import os
import sys
import tempfile
import threading
import textwrap
import unittest
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_runtime import config as cfg, tools as tools_mod
from agent_runtime.capabilities import CapabilityBroker, PermissionDenied
from agent_runtime.cli import main
from agent_runtime.models import RunState, migrate
from agent_runtime.tools import ToolContext, run_command


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_tmp)
        self.ws = Path(self.tmp.name)
        self._sb, self._se = dict(tools_mod.SANDBOX), dict(tools_mod.SEARCH)

    def _cleanup_tmp(self):
        from agent_runtime.memory import _close_all
        _close_all()
        from agent_runtime.mcp import _close_all as _mcp_close_all
        _mcp_close_all()
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def tearDown(self):
        tools_mod.SANDBOX, tools_mod.SEARCH = self._sb, self._se

    def write_policy(self, d):
        f = self.ws / ".agent" / "policy.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d))


class Config(Base):
    def test_defaults_and_overrides(self):
        broker, rt = cfg.load(self.ws)
        self.assertEqual(broker, {})
        self.assertEqual(rt["sandbox"]["mode"], "limits")
        self.write_policy({"net_domains": ["*.github.com"], "sandbox": {"mode": "none"}})
        broker, rt = cfg.load(self.ws)
        self.assertEqual(broker["net_domains"], ["*.github.com"])
        self.assertEqual(rt["sandbox"]["mode"], "none")
        self.assertEqual(rt["sandbox"]["memory_mb"], 2048)          # merged with defaults

    def test_unknown_and_invalid_keys_are_errors(self):
        self.write_policy({"exec_alow": ["*"]})                     # typo must not silently do nothing
        with self.assertRaises(cfg.ConfigError):
            cfg.load(self.ws)
        self.write_policy({"sandbox": {"mode": "yolo"}})
        with self.assertRaises(cfg.ConfigError):
            cfg.load(self.ws)

    def test_policy_reaches_the_broker(self):
        from agent_runtime import Runtime
        self.write_policy({"net_domains": ["example.org"]})
        rt = Runtime(self.ws, llm=None, judge=None, tools={})
        self.assertFalse(rt.broker("m").check("read", "https://evil.example.com/")[0])

    def test_agent_cannot_edit_its_policy(self):
        b = CapabilityBroker(self.ws, self.ws / "a.jsonl")
        self.assertFalse(b.check("write", ".agent/policy.json")[0])


class Approval(Base):
    def test_chain(self):
        req = {"action": "execute", "target": "make build", "tool": "shell.run", "why": "", "reason": "x"}
        self.assertFalse(cfg.make_approver()(req))                                        # default deny
        self.assertTrue(cfg.make_approver(yes=True)(req))
        self.assertTrue(cfg.make_approver([{"action": "execute", "pattern": "make *"}])(req))
        answers = iter(["a"])
        ap = cfg.make_approver(interactive=True, ask=lambda p: next(answers))
        self.assertTrue(ap(req))
        self.assertTrue(ap(req))                                   # "always" remembered; no second prompt
        self.assertFalse(cfg.make_approver(interactive=True, ask=lambda p: "")(req))      # Enter = No

    def test_broker_uses_approver_and_audits(self):
        b = CapabilityBroker(self.ws, self.ws / "a.jsonl", approver=cfg.make_approver(
            [{"action": "execute", "pattern": "make *"}]))
        b.require("agent", "m", "s", "shell.run", "execute", "make test")
        rec = json.loads((self.ws / "a.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["reason"], "approved interactively")
        with self.assertRaises(PermissionDenied):                  # file:// can never be approved
            CapabilityBroker(self.ws, self.ws / "b.jsonl", approver=lambda r: True).require(
                "agent", "m", "s", "web.fetch", "read", "file:///etc/passwd")


class Search(Base):
    def test_searxng_provider_through_broker(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(s):
                body = json.dumps({"results": [{"url": "https://qdrant.tech", "title": "Qdrant"},
                                               {"url": "ftp://bad", "title": "x"}]}).encode()
                s.send_response(200); s.end_headers(); s.wfile.write(body)
            def log_message(s, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_port}"
        self.write_policy({"search": {"provider": "searxng", "url": url}})
        from agent_runtime import Runtime
        rt = Runtime(self.ws, llm=None, judge=None)
        ctx = rt._ctx_factory("m")("agent", "s1")
        r = rt.tools["web.search"].call({"query": "vector db C:\\secret.txt"}, ctx)
        self.assertEqual([i["source"] for i in r["items"]], ["https://qdrant.tech"])
        audit = (self.ws / ".agent/missions/m/audit.jsonl").read_text()
        self.assertNotIn("secret", audit)                        # query privacy holds for every provider

    def test_brave_requires_key(self):
        tools_mod.SEARCH = {"provider": "brave", "api_key_env": "NO_SUCH_KEY_VAR"}
        ctx = ToolContext(self.ws, None, CapabilityBroker(self.ws, self.ws / "a.jsonl"), "m")
        with self.assertRaises(RuntimeError):
            tools_mod.web_search({"query": "x"}, ctx)


FAKE_MCP = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            r = {"protocolVersion": m["params"]["protocolVersion"], "capabilities": {"tools": {}},
                 "serverInfo": {"name": "fake", "version": "1"}}
        elif m["method"] == "tools/list":
            r = {"tools": [{"name": "echo", "description": "echo text",
                            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
        elif m["method"] == "tools/call":
            t = m["params"]["arguments"]["text"]
            r = {"content": [{"type": "text", "text": "echo:" + t}], "isError": t == "fail"}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "nope"}}), flush=True)
            continue
        print("log line that is not json", flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
''')


class MCP(Base):
    def setUp(self):
        super().setUp()
        (self.ws / "fake_mcp.py").write_text(FAKE_MCP)

    def test_tools_registered_gated_and_audited(self):
        self.write_policy({"mcp": {"fake": {"command": [sys.executable, str(self.ws / "fake_mcp.py")]}}})
        from agent_runtime import Runtime
        rt = Runtime(self.ws, llm=None, judge=None)
        self.addCleanup(rt.close)
        self.assertIn("mcp.fake.echo", rt.tools)
        t = rt.tools["mcp.fake.echo"]
        self.assertEqual(t.required, ("text",))
        ctx = rt._ctx_factory("m")("agent", "s1")
        with self.assertRaises(PermissionDenied):                  # not in mcp_allow, no approver
            t.call({"text": "hi"}, ctx)
        rt.broker("m").policy["mcp_allow"] = ["fake.*"]
        self.assertEqual(t.call({"text": "hi"}, ctx)["output"], "echo:hi")
        with self.assertRaises(RuntimeError):
            t.call({"text": "fail"}, ctx)
        audit = (self.ws / ".agent/missions/m/audit.jsonl").read_text()
        self.assertIn('"action": "mcp"', audit)

    def test_broken_server_is_reported_not_fatal(self):
        self.write_policy({"mcp": {"bad": {"command": ["definitely-not-a-real-binary-xyz"]}}})
        from agent_runtime import Runtime
        events = []
        Runtime(self.ws, llm=None, judge=None, on_event=events.append)
        self.assertTrue(any("failed to start" in e for e in events))


class Sandbox(Base):
    def test_limits_mode_memory_behavior_is_platform_aware(self):
        """Security contract for limits mode vs docker mode:
        - Linux + limits: Kernel enforces RLIMIT_AS -> child memory hog must terminate.
        - macOS + limits: CPU/file/core limits apply where supported, but Darwin XNU kernel
          does NOT enforce RLIMIT_AS for 64-bit address spaces. Verify process runs cleanly.
        - Windows + limits: No POSIX preexec limits available. Verify standard execution.
        - Docker mode: Cross-platform hard memory/CPU/network isolation boundary.
        """
        cfg_ = {"mode": "limits", "cpu_seconds": 10, "memory_mb": 256}

        if sys.platform.startswith("linux"):
            # Linux: Kernel enforces RLIMIT_AS -> process must terminate
            (self.ws / "hog.py").write_text("x = bytearray(600 * 1024 * 1024)\nprint('allocated')\n")
            r = run_command(f"{sys.executable} hog.py", self.ws, 30, cfg_)
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("allocated", r.stdout)

        elif sys.platform == "darwin":
            # macOS: Verify limits preexec hook runs without crashing
            # (Darwin XNU does not enforce RLIMIT_AS for 64-bit processes)
            (self.ws / "ok.py").write_text("print('ok')\n")
            r = run_command(f"{sys.executable} ok.py", self.ws, 30, cfg_)
            self.assertEqual(r.stdout.strip(), "ok")
            self.assertEqual(r.returncode, 0)

        elif os.name == "nt":
            # Windows: Verify safe fallback execution without POSIX preexec
            (self.ws / "ok.py").write_text("print('ok')\n")
            r = run_command(f"{sys.executable} ok.py", self.ws, 30, cfg_)
            self.assertEqual(r.stdout.strip(), "ok")
            self.assertEqual(r.returncode, 0)

    def test_docker_argv_is_locked_down(self):
        argv = tools_mod.sandbox_argv(["python", "-m", "pytest"], self.ws, {"memory_mb": 512, "image": "img"})
        for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "no-new-privileges", "512m"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-4:], ["img", "python", "-m", "pytest"])


class Schema(Base):
    def test_v1_checkpoint_migrates(self):
        v1 = {"mission": {"id": "x", "text": "t", "objective": "t", "kind": "general", "constraints": {},
                          "criteria": [], "created": 0}, "plan": {"version": 1, "steps": []}, "evidence": {},
              "replans": 0, "fixes": {}, "status": "failed", "report": None, "log": []}
        st = RunState.from_dict(v1)
        self.assertTrue(st.mission.constraints["verifiable"])
        self.assertEqual(st.to_dict()["schema"], 2)
        with self.assertRaises(ValueError):
            migrate(dict(v1, schema=99))


class CLI(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--workspace", str(self.ws), *args])
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes_policy_and_show(self):
        os.environ.pop("AGENT_LLM_BASE_URL", None)
        code, out, _ = self.run_cli("run", "Copy /etc/passwd into out.txt")
        self.assertEqual(code, 3)                                   # refused
        (self.ws / "d.csv").write_text("k,v\na,1\nb,2\n")
        code, out, _ = self.run_cli("run", "From d.csv compute the total v and write sum.md")
        self.assertEqual(code, 0, out)
        mid = next(l.split()[1].rstrip(":") for l in out.splitlines() if l.startswith("mission "))
        code, out, _ = self.run_cli("show", mid)
        self.assertIn("data_correct", out)
        self.assertIn("plan:", out)
        code, out, _ = self.run_cli("policy", "--init")
        self.assertTrue((self.ws / ".agent/policy.json").exists())
        code, out, _ = self.run_cli("policy")
        self.assertIn('"sandbox"', out)
        (self.ws / ".agent/policy.json").write_text('{"oops": 1}')
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("config error", err)


if __name__ == "__main__":
    unittest.main()
