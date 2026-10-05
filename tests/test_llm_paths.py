import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_runtime import Runtime, tools as T
from agent_runtime.compiler import compile_mission
from agent_runtime.llm import OpenAICompatLLM, LLM, extract_json
from agent_runtime.planner import Planner
from agent_runtime.tools import Tool, default_registry

PAGES = {"/a": "<html><script>x=1</script><body><h1>Acme AI</h1> hiring interns</body></html>",
         "/b": "<html><body>Beta Labs &amp; Co is hiring</body></html>",
         "/c": "<html><body>Gamma Systems</body></html>"}


class Web(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404); self.end_headers()
        self.wfile.write((body or "nope").encode())
    def log_message(self, *a): pass


class Scripted(LLM):
    """Role-aware fake model; returns fenced / chatty JSON like real models do."""
    def __init__(self, base): self.base, self.calls = base, []
    def complete(self, system, prompt, max_tokens=2000):
        self.calls.append(system[:20])
        if "compile" in system:
            return "Sure! ```json\n" + json.dumps({"objective": "AI companies", "kind": "research", "constraints": {},
                "criteria": [{"id": "c1", "description": "3 items", "check": "min_items", "params": {"n": 3}},
                             {"id": "c2", "description": "unique", "check": "unique_items", "params": {}},
                             {"id": "c3", "description": "sourced", "check": "all_sourced", "params": {}},
                             {"id": "c4", "description": "bogus", "check": "not_a_check", "params": {}}]}) + "\n```"
        if "Plan as a DAG" in system:
            return json.dumps([{"id": "a", "tool": "web.search", "args": {"query": "x"}, "deps": []},
                               {"id": "b", "tool": "web.fetch_many", "args": {"items": "$a.items"}, "deps": ["a"]},
                               {"id": "c", "tool": "llm.extract_items", "args": {"docs": "$b.items", "instruction": "names"}, "deps": ["b"], "result": True}])
        if "Extract items" in system:
            return json.dumps([{"value": "Acme AI", "source": self.base + "/a"}, {"value": "Beta Labs", "source": self.base + "/b"},
                               {"value": "Gamma Systems", "source": self.base + "/c"},
                               {"value": "Hallucinated Inc", "source": "https://invented.example"}])
        return "YES"


class T1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Web)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        from agent_runtime.memory import _close_all
        _close_all()
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def registry(self):
        reg = default_registry([])
        def search(a, c):
            items = [{"value": k, "source": self.base + k, "key": k} for k in PAGES]
            return {"output": "", "items": items, "sources": [i["source"] for i in items], "files": []}
        reg["web.search"] = Tool("web.search", "", "", search)
        return reg

    def test_end_to_end_with_llm_real_http_and_hallucination_filter(self):
        llm = Scripted(self.base)
        rt = Runtime(self.ws, llm=llm, tools=self.registry(), policy={"block_private_net": False}, base_delay=0, use_engines=False)
        s = rt.start("Find 3 AI companies")
        self.assertEqual(s.status, "passed", s.log)
        vals = [i["value"] for i in json.loads((rt.store.dir(s.mission.id) / "artifact.json").read_text())["items"]]
        self.assertEqual(sorted(vals), ["Acme AI", "Beta Labs", "Gamma Systems"])   # hallucinated one dropped
        self.assertNotIn("not_a_check", {c.check for c in s.mission.criteria})            # bogus check dropped
        self.assertIn("grounded", {c.check for c in s.mission.criteria})            # contract floor added
        self.assertEqual(s.plan.get("c").status, "done")

    def test_private_net_blocked_by_default(self):
        rt = Runtime(self.ws, llm=Scripted(self.base), tools=self.registry(), base_delay=0, max_replans=0, use_engines=False)
        s = rt.start("Find 3 AI companies")
        self.assertEqual(s.status, "failed")
        self.assertIn("needs human", s.log[-1]["msg"])

    def test_html_text_extraction(self):
        t = T._text(PAGES["/a"] + PAGES["/b"])
        self.assertIn("Acme AI", t); self.assertNotIn("x=1", t); self.assertIn("Beta Labs & Co", t)

    def test_search_parser(self):
        html = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.com%2Fp&rut=1">Ex <b>Page</b></a>'
                '<a class="result__a" href="https://direct.com/x">Direct</a>')
        orig = T._http_get
        T._http_get = lambda url, ctx, limit=0: html
        try:
            r = T.web_search({"query": "q"}, None)
        finally:
            T._http_get = orig
        self.assertEqual([i["source"] for i in r["items"]], ["https://ex.com/p", "https://direct.com/x"])

    def test_invalid_llm_plan_falls_back(self):
        class Bad(LLM):
            def complete(self, s, p, m=2000): return json.dumps([{"id": "a", "tool": "nope", "args": {}, "deps": []}])
        m = compile_mission("Find 3 AI companies")
        plan = Planner(default_registry([]), Bad()).plan(m)
        self.assertEqual(plan.steps[0].tool, "web.search")

    def test_llm_replan_prefixes_refs(self):
        llm = Scripted(self.base)
        reg = self.registry(); m = compile_mission("Find 3 AI companies")
        p = Planner(reg, llm); plan = p.plan(m)
        p.replan(m, plan, [("c1", "min_items", "0/3")], 1)
        new = [s for s in plan.steps if s.id.startswith("r1_")]
        self.assertEqual(new[1].args["items"], "$r1_a.items"); self.assertEqual(new[1].deps, ["r1_a"])

    def test_openai_compat_http_layer(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(s):
                body = json.loads(s.rfile.read(int(s.headers["Content-Length"])))
                assert body["messages"][0]["role"] == "system" and s.headers["Authorization"] == "Bearer k"
                s.send_response(200); s.end_headers()
                s.wfile.write(json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode())
            def log_message(s, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertEqual(OpenAICompatLLM(f"http://127.0.0.1:{srv.server_port}/v1", "m", "k").complete("s", "p"), "pong")
        finally: srv.shutdown()

    def test_extract_json_variants(self):
        self.assertEqual(extract_json('text {"a": 1} tail'), {"a": 1})
        self.assertEqual(extract_json('```json\n[1,2]\n```'), [1, 2])

if __name__ == "__main__":
    unittest.main()
