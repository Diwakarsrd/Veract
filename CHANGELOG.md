# Changelog

## 0.3.0 — 2026-10-04
- `.agent/policy.json` configuration (validated: unknown keys are errors), plus `agent policy [--init]`
- Approval chain: `--yes` > policy `approve` rules > interactive prompt (`y` / `N` / `a` = always this session) > deny
- Pluggable search: DuckDuckGo (default), self-hosted SearXNG, Brave API
- MCP client (stdio): servers from the policy file become `mcp.<server>.<tool>` tools, gated by `mcp_allow` or approval
- Command sandbox: `limits` (rlimits, process group, scrubbed env) by default; `docker` mode with no network, read-only root, dropped caps (`sandbox/Dockerfile`)
- `agent show <id>`; CLI exit codes (0 passed, 1 failed/partial/unverified, 2 config error, 3 refused)
- Versioned checkpoints and artifacts (`schema: 2`) with migration from 0.1 checkpoints
- Fixed: `bench/bench.py` crashed on import (`t25_grader` used before definition)
- CI on Linux/Windows/macOS × Python 3.10–3.13, ruff, build; SECURITY.md, CONTRIBUTING.md, issue templates

## 0.2.0 — 2026-10-03
- Scope guard, contract floor, honest statuses (`refused`, `partial`, `unverified`), deadline budget, context fitting
- argv command rules, http(s)-only network, protected `.agent/` and `.git/`, trusted plugins, secret redaction
- `fs.patch`, `code.fix` on the planned path, tool argument validation, regression revert
- Fixed: stale bytecode on same-size edits, infinite-timeout crash, missing `replace` treated as delete
- Held-out offline benchmark (`bench/heldout.py`)

## 0.1.0
- Initial mission runtime: compiler, planner, executor, verifier, recovery, broker, memory, engines
