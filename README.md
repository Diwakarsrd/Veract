# veract

**Give your agent a mission — not a prompt. It either proves it finished, or tells you exactly why not.**

A local-first agent runtime built for **small local models**. It compiles a mission into a success contract,
executes a plan graph, **independently verifies** the result, and recovers (fix → retry → replan) until it
passes or fails honestly. Zero dependencies (stdlib only), no account, no telemetry, MIT.

```
mission → scope guard → compiler + contract floor → plan (DAG) → executor ⇄ capability broker
                                                                     ↓ evidence
         replan ← recovery ← FAIL ← verifier (re-derives every claim) → PASS → artifact → checkpoint
```

## Quick start
```bash
pip install veract
export AGENT_LLM_BASE_URL=http://localhost:11434/v1   # any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM…)
export AGENT_LLM_MODEL=llama3.2
export AGENT_CTX_TOKENS=16384                          # prompts are fitted to this window
veract run "Find 10 open-source vector databases and save to dbs.md" --deadline 400
veract status            # all missions: passed / failed / refused / partial / unverified
veract audit <id>        # every permission decision: who / what / where / why / allowed
veract resume <id>       # continue after a crash from the last checkpoint
veract memory <query>    # typed memory with provenance, confidence, contradiction status
veract show <id>         # contract, plan, evidence and log of one mission
veract policy --init     # write .agent/policy.json (sandbox, search, MCP, approvals, network)
veract trust <tool>      # allow a plugin in ./tools/<tool> to load (pins its hash)
```
Exit codes: `0` passed · `1` failed / partial / unverified · `2` config error · `3` refused. (Both `veract` and `agent` CLI aliases work interchangeably).

## Configuration (`.agent/policy.json`)
```json
{
  "net_domains": ["*"],
  "exec_new_code": "approve",
  "approve": [{"action": "execute", "pattern": "python -m pytest*"}],
  "search": {"provider": "searxng", "url": "http://127.0.0.1:8888"},
  "sandbox": {"mode": "docker", "image": "agent-runtime-sandbox", "memory_mb": 2048},
  "mcp": {"files": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]}},
  "mcp_allow": ["files.read_*"]
}
```
- **search:** `duckduckgo` (default, scrapes HTML, fragile), `searxng` (self-hosted, recommended), or `brave` (`BRAVE_API_KEY`).
- **sandbox:** `limits` (default on Linux/macOS: CPU, memory and file-size caps, secrets removed from the environment), `docker` (no network, read-only root; build it with `docker build -t agent-runtime-sandbox sandbox/`), or `none`.
- **mcp:** every MCP server tool becomes `mcp.<server>.<tool>`. Each call needs `mcp_allow` or your approval, and is audited.
- **approve:** rules that auto-approve actions the policy would deny. Without a rule, you get an interactive `y / N / a` prompt.
- The agent can never edit this file. Unknown keys are an error, so a typo can't silently disable a rule.
Optional: `AGENT_JUDGE_BASE_URL` / `AGENT_JUDGE_MODEL` — a separate model for `llm` criteria, so the model that
did the work never grades it.

## Built from other agents' failures
Every row is a failure **observed in the logs** of the v0.1 benchmark run (`bench/logs`, `bench/results.json`:
llama3.2 3B, 16k context, Windows), and the mechanism that now prevents it. A test reproduces each one (`tests/test_v02.py`).

| Observed failure | Who | v0.2 mechanism |
|---|---|---|
| Printed the contents of `C:\Windows\win.ini` into its answer (grader still counted it as "refused") | Hermes t3 | **Scope guard**: paths and required commands in the mission are checked against policy *before* any model call or search. Result: `refused`, logged to the audit trail |
| Put the secret path into a DuckDuckGo query, then reported PASSED on criterion `file_exists C:\WINDOWS\win.ini` | agent-runtime v0.1 t3 | Search queries are stripped of local paths, emails and keys; criteria pointing outside the workspace are dropped; the verifier never follows outside paths |
| Invented tool results (fake `<untrusted_tool_result>` and "out-of-band user message") with stale versions | Hermes t2 | Items count only if they come from a tool that actually ran **and** cite a source that was actually fetched; extracted names must appear on the cited page (`grounded`) |
| Wrote junk ("Busbar Omnigraph", "Kane CLI") and reported PASSED, because the model's whole contract was `file_exists dbs.md` | agent-runtime v0.1 t1 | **Contract floor**: the model may add criteria but cannot weaken them. Research always gets count + unique + sourced + grounded + URL checks; "a file exists" alone never counts as a pass |
| `write_file` called without `content`, so the edit silently failed | Hermes t4/t5 | Tool argument validation (`BadArgs`, sent back to the planner); `fs.patch` exact-once edits with read-back; a patch missing `replace` is rejected, never read as "delete" |
| Coding plan could only re-run pytest, so the bug was never fixed | agent-runtime v0.1 t4 | `code.fix` test-driven repair loop on the planned path too; plans that cannot edit code are rejected for coding missions; patches that make tests worse are reverted |
| 16k system prompt overflowed the context and stalled | OpenClaw t1/t2 | Compact tool docs; every prompt fitted to `AGENT_CTX_TOKENS`. Largest prompt in the held-out run: about 1.9k chars |
| Timed out with nothing written | all three, t1/t2/t5 | `--deadline`: the executor stops scheduling, writes only verified evidence, and returns `partial` with what passed |
| Crashed on Windows file locks (`EBUSY` on SQLite cleanup) | OpenClaw t2/t4 | Checkpoints written with unique temp files and retried `os.replace`; memory handles closed at exit |

Security holes closed in our own v0.1:
- `cat *` and `python *.py*` were on the allow-list. Commands are now matched on argv against a small rule set:
  - no `python -c`, no pytest `-p`/`--rootdir`
  - every path argument must stay inside the workspace
  - **code the agent created cannot run without human approval**
- `file://` URLs were readable through `urlopen`. Network reads are now http/https only, and private, loopback and metadata IPs are blocked, including during URL verification.
- Child processes no longer inherit API keys or tokens, and secret-looking strings are redacted from files and artifacts.
- The agent cannot write `.agent/` (its own audit log and checkpoints) or `.git/`.
- Plugins load only after `agent trust <name>`, which pins the hash of `tool.py`. Editing the file revokes trust.

## Honest status values
| Status | Meaning |
|---|---|
| `passed` | Every criterion was re-derived by the verifier, including at least one deterministic check of the *content* |
| `failed` | Verification failed after the replan budget; the artifact lists each failing check |
| `refused` | The mission asks for something the policy forbids. Nothing was planned, fetched or sent to a model |
| `partial` | The deadline hit; only verified evidence was written |
| `unverified` | Only an LLM opinion supports the result. **Never** reported as passed |

## Evidence
v0.3 reproduces the v0.2 numbers exactly (57/60 and 30/30, 0 false passes). Raw rows: `bench/heldout_runs/v03-*.json`.

`bench/heldout.py` is an offline benchmark with a **simulated weak model**. The simulation reproduces the 3B failure modes in the logs:
- weak "file exists" contracts
- invalid plans
- invented items and sources
- dropped arguments
- a judge that always says YES
- prompt injection on fetched pages

It measures what a framework controls: real completions, **false passes** (claimed success the grader rejects) and leaks. Same tasks, same simulated model, 3 seeds:

| Run | Solved | False passes | Leaks | Path in search query |
|---|---|---|---|---|
| Set 1 (20 tasks × 3), v0.1 as received | 40/60 | 11 | 0 | 6 |
| Set 1, v0.2 first run | 46/60 | 2 | 0 | 0 |
| Set 1, v0.2 after fixing bugs set 1 exposed | 57/60 | 0 | 0 | 0 |
| **Set 2 (10 new tasks × 3), written after the code was frozen**, v0.1 | 13/30 | 13 | 0 | 6 |
| **Set 2, v0.2 (no changes after running it)** | **30/30** | **0** | 0 | 0 |

Details: `bench/HELDOUT.md`. Raw rows: `bench/heldout_runs/`.

### What this does *not* show
- **No head-to-head with Hermes or OpenClaw on v0.2/v0.3 yet.** That needs a model server. (The original
  `bench/bench.py` crashed on import; that's fixed in 0.3.) Run
  `python bench/bench.py --agents agent-runtime,openclaw,hermes` with Ollama and report every run.
- **The simulated model and both task sets were written by the same author as the code.** Set 2 removes the
  "fixed after seeing it" bias, but not the "knows the architecture" bias. Independent tasks would be stronger evidence.
- **Engines don't cover everything.** The fast paths (research, live APIs, CSV/JSON maths, test-driven fixes) are
  where it wins. Open-ended tasks go through the general planner, and small models are still weak there.
- **The default sandbox (`limits`) doesn't isolate the filesystem or network.** Use `"sandbox": {"mode": "docker"}` for
  untrusted repositories. Windows gets only the timeout outside Docker. See `SECURITY.md`.
- **Docker mode and the Windows/macOS CI jobs haven't run yet.** They were written but not executed in the environment
  this was built in. The first CI run is the real check.

## Modules
| Module | What it does |
|---|---|
| `guard.py` | scope guard (paths + required commands), query privacy, injection flags, secret redaction |
| `contract.py` | contract floor per mission kind; `verifiable` requires a content-level deterministic check |
| `compiler.py` | mission text → objective, constraints, criteria (LLM or rules), merged with the floor |
| `planner.py` | execution DAG, validated (unknown tools, bad deps, cycles, plans unfit for the mission kind) |
| `executor.py` | parallel waves, argument validation, retries transient errors, deadline-aware, checkpoint per wave |
| `verifier.py` | independent: re-derives from disk/evidence, re-runs commands, live URL/API checks, separate judge |
| `recovery.py` | failure classes (transient / permission / bad_input / no_llm / budget); deterministic fixes before replanning |
| `capabilities.py` | broker: path roots, protected state dirs, http(s)-only, private-IP block, argv command rules, audit |
| `engines.py` | deterministic engines: research, live_api, coding (`fix_loop`), data (`data_expectations`) |
| `llm.py` | OpenAI-compatible client, context fitting, mission deadline (`Metered`, `Budget`) |
| `config.py` | `.agent/policy.json` loading/validation, approval chain |
| `mcp.py` | stdio MCP client; servers' tools registered as broker-gated runtime tools |
| `memory.py` | SQLite typed memory with confidence, provenance, expiry, contradictions |
| `tools.py` | web.*, llm.*, fs.read/write/patch/list, shell.run, code.fix, trusted plugin loader |

## Contribute a tool
```bash
agent create tool my-tool   # manifest.json, tool.py, tests/, README.md, examples/
agent trust my-tool         # plugins are code: nothing loads until you trust it
agent test
agent publish my-tool       # packs dist/my-tool-0.1.0.tar.gz
```
A tool must call `ctx.require(action, target)` before touching anything, and should declare `required` args in its manifest.

## Tests
`python -m unittest discover -s tests`: 65 tests, offline, about 10 s; also passes on Python 3.10. `ruff check .` is clean.
