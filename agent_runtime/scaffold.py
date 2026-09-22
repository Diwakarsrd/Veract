import json
import tarfile
from pathlib import Path

TOOL_PY = '''def run(args, ctx):
    """Entry point. Call ctx.require(action, target) before reading/writing/executing anything."""
    text = args.get("text", "")
    return {"output": text.upper(), "items": [{"value": text.upper(), "source": "tool:%s", "key": text}], "sources": [], "files": []}
'''
TEST_PY = '''import unittest, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import tool

class T(unittest.TestCase):
    def test_runs(self):
        self.assertEqual(tool.run({"text": "hi"}, None)["output"], "HI")

if __name__ == "__main__":
    unittest.main()
'''


def create_tool(name, root="tools"):
    d = Path(root) / name
    if d.exists():
        raise SystemExit(f"{d} already exists")
    (d / "tests").mkdir(parents=True)
    (d / "examples").mkdir()
    (d / "manifest.json").write_text(json.dumps({"name": name, "version": "0.1.0", "description": "TODO",
                                                 "entry": "tool.py", "args": {"text": "str"}, "required": ["text"],
                                                 "permissions": []}, indent=2))
    (d / "tool.py").write_text(TOOL_PY % name)
    (d / "tests" / "test_tool.py").write_text(TEST_PY)
    (d / "README.md").write_text(f"# {name}\n\nDescribe what this tool does and which capabilities it needs.\n")
    (d / "examples" / "example.json").write_text(json.dumps({"text": "hello"}))
    return d   # not loaded until trusted: `agent trust <name>`


def publish_tool(name, root="tools", out="dist"):
    d = Path(root) / name
    Path(out).mkdir(exist_ok=True)
    pkg = Path(out) / f"{name}-{json.loads((d / 'manifest.json').read_text())['version']}.tar.gz"
    with tarfile.open(pkg, "w:gz") as t:
        t.add(d, arcname=name)
    return pkg
