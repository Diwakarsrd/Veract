# Contributing

```bash
pip install -e . pytest ruff
python -m unittest discover -s tests      # 65 tests, offline, ~10 s
ruff check .
python bench/heldout.py --impl . --set 2 --seeds 3
```

Rules that keep the project honest:
1. **No false passes.** A change that makes any held-out run report `passed` where the grader fails is a regression, even if the solved count goes up.
2. **Every tool calls `ctx.require(action, target)`** before touching files, network, processes or MCP.
3. **New behaviour comes with a test** that reproduces the failure it prevents.
4. **Benchmarks report every run.** Never report only the best or the earliest run. If you tune against a task set, say so, and validate on a set written afterwards.
5. Keep the core stdlib-only. Optional integrations go behind config.
