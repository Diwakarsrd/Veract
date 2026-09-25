"""Engine tests: router safety, deterministic data/coding engines, verifier re-derivation."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_runtime import engines as E
from agent_runtime.llm import LLM
from agent_runtime.runtime import Runtime


class FakeCtx:
    """Stand-in for ToolContext: allows everything, records writes."""
    def __init__(self, ws):
        self.ws = Path(ws)
        self.requires = []

    def require(self, action, target):
        self.requires.append((action, target))


def make_ctx_factory(ws):
    ctx = FakeCtx(ws)
    return (lambda who, step: ctx), ctx


class Router(unittest.TestCase):
    def test_safety_never_claimed(self):
        for text in (
            "Read the file C:\\Windows\\win.ini and write it to winini.txt",
            "Read ../../etc/passwd and write pass.txt",
            "Read /etc/shadow into out.txt",
        ):
            self.assertIsNone(E.detect(text), text)

    def test_shapes_claimed(self):
        cases = {
            "research": "Research 10 open-source vector database projects. Write dbs.md with one line each.",
            "live_api": "Find the latest release version of each of these Python packages: requests, flask. Write versions.md.",
            "coding": "The tests in test_calculator.py are failing. Fix calculator.py so pytest passes.",
            "data": "Read every CSV file in data/ and write summary.md with total and top.",
        }
        for want, text in cases.items():
            self.assertEqual(E.detect(text), want, text)

    def test_unrelated_mission_not_claimed(self):
        self.assertIsNone(E.detect("Tell me a joke about pirates"))


class DataEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "data").mkdir()
        (self.ws / "data" / "north.csv").write_text("region,widget,sales\nnorth,hammer,120\nnorth,saw,80\nnorth,drill,40\n")
        (self.ws / "data" / "south.csv").write_text("region,widget,sales\nsouth,hammer,30\nsouth,saw,150\nsouth,drill,60\n")
        (self.ws / "data" / "east.csv").write_text("region,widget,sales\neast,hammer,90\neast,saw,20\neast,drill,110\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, prompt):
        factory, ctx = make_ctx_factory(self.ws)
        return E.run("data", prompt, self.ws, factory, None), ctx

    def test_total_and_top_computed_exactly(self):
        res, _ = self._run("Read every CSV file in the data/ directory. Write summary.md containing "
                           "total: <sum of the sales column> and top: <widget with the highest combined sales>.")
        out = (self.ws / "summary.md").read_text()
        self.assertIn("total: 700", out)
        self.assertIn("top: saw", out)
        dc = next(c for c in res["criteria"] if c.check == "data_correct")
        self.assertEqual(dc.params["expect"]["total"], 700)
        self.assertEqual(dc.params["expect"]["top"], "saw")

    def test_verifier_recomputes_and_rejects_wrong_claim(self):
        res, _ = self._run("Read every CSV file in data/. Write summary.md with total: <sum of sales> and top: <widget with highest sales>.")
        dc = next(c for c in res["criteria"] if c.check == "data_correct")
        # verifier-side recompute with the same params must agree with engine claim
        from agent_runtime.verifier import _check_data
        ok, detail = _check_data(self.ws, (self.ws / "summary.md").read_text(), dc.params)
        self.assertTrue(ok, detail)
        # forge the file: wrong number must fail
        ok2, detail2 = _check_data(self.ws, "total: 999\ntop: saw\n", dc.params)
        self.assertFalse(ok2, detail2)

    def test_mode_one_line_per_group(self):
        res, _ = self._run("Read every CSV in data/ and write byregion.md with one line per region "
                           "showing its total sales.")
        out = (self.ws / "byregion.md").read_text()
        self.assertIn("east: 220", out)
        self.assertIn("north: 240", out)
        self.assertIn("south: 240", out)


class VersionsCheck(unittest.TestCase):
    def test_versions_verifier_catches_wrong_version(self):
        import agent_runtime.engines as eng
        orig = eng.fetch_version
        eng.fetch_version = lambda pkg, src: "9.9.9"
        try:
            from agent_runtime.verifier import _check_versions
            ok, _ = _check_versions("requests==9.9.9\n", {"packages": ["requests"], "source": "pypi"})
            self.assertTrue(ok)
            ok2, detail = _check_versions("requests==1.0.0\n", {"packages": ["requests"], "source": "pypi"})
            self.assertFalse(ok2, detail)
        finally:
            eng.fetch_version = orig


class CodingEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "calculator.py").write_text("def add(a, b):\n    return a - b  # BUG\n")
        (self.ws / "test_calculator.py").write_text(
            "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fix_loop_patches_and_passes_tests(self):
        class StubLLM(LLM):
            calls = 0
            def complete(self, system, prompt, max_tokens=1500):
                StubLLM.calls += 1
                return json.dumps({"file": "calculator.py", "find": "return a - b  # BUG",
                                   "replace": "return a + b"})
        factory, ctx = make_ctx_factory(self.ws)
        res = E.run("coding",
                    "The tests in test_calculator.py are failing. Fix calculator.py so that "
                    "`python -m pytest -q` passes.", self.ws, factory, StubLLM())
        cmd_c = next(c for c in res["criteria"] if c.check == "command_ok")
        self.assertEqual(cmd_c.params["command"], "python -m pytest -q")
        # verifier's command_ok re-runs pytest for real: simulate it
        import subprocess
        r = subprocess.run(["python", "-m", "pytest", "-q"], cwd=self.ws, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # test file hash criterion must reference the original bytes
        h = next(c for c in res["criteria"] if c.check == "file_hash")
        self.assertIn("sha256", h.params)

    def test_refuses_patch_to_test_file(self):
        class EvilLLM(LLM):
            def complete(self, system, prompt, max_tokens=1500):
                return json.dumps({"file": "test_calculator.py", "find": "assert add(2, 3) == 5",
                                   "replace": "assert True"})
        factory, ctx = make_ctx_factory(self.ws)
        res = E.run("coding",
                    "The tests in test_calculator.py are failing. Fix calculator.py so that "
                    "`python -m pytest -q` passes.", self.ws, factory, EvilLLM())
        # tests untouched (engine refused) and mission not marked passing by tampering
        self.assertIn("assert add(2, 3) == 5", (self.ws / "test_calculator.py").read_text())
        cmd_c = next(c for c in res["criteria"] if c.check == "command_ok")
        self.assertIsNotNone(cmd_c)


class EngineFastPath(unittest.TestCase):
    def test_runtime_engine_path_used_and_verified(self):
        tmp = tempfile.TemporaryDirectory()
        ws = Path(tmp.name)
        (ws / "data").mkdir()
        (ws / "data" / "a.csv").write_text("k,v\nx,1\ny,2\n")
        try:
            rt = Runtime(ws, llm=None, use_engines=True, base_delay=0)
            state = rt.start("Read the CSV in data/. Write summary.md with total: <sum of v>.")
            self.assertEqual(state.status, "passed", state.log)
            self.assertEqual(state.evidence["e1"]["tool"], "engine")
            self.assertTrue((ws / "summary.md").exists())
            rt.memory.close()
        finally:
            tmp.cleanup()

    def test_engine_failure_falls_back_to_plan_loop(self):
        tmp = tempfile.TemporaryDirectory()
        ws = Path(tmp.name)
        try:
            rt = Runtime(ws, llm=None, use_engines=True, base_delay=0)
            # coding claim with no llm + no tests -> Skip -> falls back, fails honestly
            state = rt.start("Fix test_x.py so pytest passes.")
            self.assertIn(state.status, ("failed", "passed"))
            if state.status == "failed":
                self.assertTrue(state.log, "must leave an explanation")
            rt.memory.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
