# Veract

### Give your agent a mission — not a prompt.

An open-source, local-first runtime for autonomous AI agents that **plan, execute, verify, recover, and deliver**.

> *Agents shouldn't decide whether they succeeded.*  
> *The runtime should prove it.*

[Website](https://verdant-fairy-ecae2c.netlify.app/) &nbsp;·&nbsp; [Quick Start](#quick-start) &nbsp;·&nbsp; [Why Veract?](#why-veract) &nbsp;·&nbsp; [Architecture](#architecture) &nbsp;·&nbsp; [Benchmarks](#benchmarks) &nbsp;·&nbsp; [Security](#security-by-default) &nbsp;·&nbsp; [Documentation](#cli-reference)

[![Website](https://img.shields.io/badge/website-live-3ddc97.svg)](https://verdant-fairy-ecae2c.netlify.app/)
[![PyPI version](https://img.shields.io/badge/pypi-v0.3.2-blue.svg)](https://pypi.org/project/veract/)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![CI](https://github.com/Diwakarsrd/Veract/actions/workflows/ci.yml/badge.svg)](https://github.com/Diwakarsrd/Veract/actions)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-65%20passed-success.svg)](tests/)

---

## 30-Second Demo

```bash
pip install veract
export AGENT_LLM_BASE_URL=http://localhost:11434/v1   # Ollama, vLLM, LM Studio, or OpenAI
export AGENT_LLM_MODEL=llama3.2

veract run "Research the 10 best open-source vector databases and save a sourced comparison to dbs.md"
```

```text
✓ Mission compiled: "10 best open-source vector databases"
✓ Success contract created: [min_items: 10, unique: true, sourced: true, live_urls: true]
✓ Execution DAG generated: 4 parallel waves
✓ 10/10 vector databases extracted & grounded to source text
✓ Independent verification: 10/10 URLs resolve, 0 hallucinations
✓ Artifact sealed: dbs.md (SHA-256 verified)
✓ Evidence checkpoint saved: .agent/missions/20261006-033120-0db756

MISSION PASSED (0 replans, 13.3s wall time)
```

Veract doesn't ask an LLM if it finished. It requires independent, reproducible proof against an explicit contract floor.

---

## Why Veract?

Most agent frameworks optimize for **getting an answer**.  
Veract optimizes for **proving the answer is correct**.

| Traditional Agent | Veract |
|---|---|
| **Prompt → response** | **Mission → explicit success contract** |
| Model decides whether it succeeded | Independent verifier validates proof from disk and live APIs |
| Blind tool execution | Capability-brokered tools with audit logs and approval gates |
| Infinite unguided retry loops | Failure-classified recovery (*fix → retry → replan*) |
| Lossy conversational history | SQLite typed memory with provenance, confidence decay, and contradictions |
| *"Looks complete to me"* | Cryptographically hashed evidence checkpoint |
| Silent hallucinated success | Honest terminal states: `passed`, `failed`, `refused`, `partial` |

---

## What Veract Can Do

- **Research** — Search candidate pages, fetch sources, extract grounded entities, verify live registry URLs, and format citations.
- **Coding** — Run tests, isolate failures, patch files with exact-once verification, re-test, and rollback if tests degrade.
- **Data Extraction** — Ingest structured files (CSV, JSON), calculate aggregations, validate anomalies, and verify against truth data.
- **Security Guardrails** — Prevent prompt injection, block out-of-workspace file traversal, deny private IP SSRF, and redact credentials.
- **Self-Healing Recovery** — Classify failures into transient, permission, syntax, or logic errors and apply deterministic fixes before replanning.
- **Long-Term Memory** — Store verified facts with provenance tags, confidence scores, and automatic contradiction invalidation.

---

## Architecture

Veract separates execution from evaluation. The agent never grades its own work:

```text
                        ┌─────────────┐
                        │   MISSION   │
                        └──────┬──────┘
                               ↓
                    ┌────────────────────┐
                    │  INTENT COMPILER   │
                    └─────────┬──────────┘
                              ↓
                    ┌────────────────────┐
                    │ SUCCESS CONTRACT   │
                    └─────────┬──────────┘
                              ↓
                    ┌────────────────────┐
                    │    PLAN / DAG      │
                    └─────────┬──────────┘
                              ↓
               ┌─────────────────────────────┐
               │    BROKERED EXECUTOR        │ ⇄ [ Capability Broker ]
               └──────────────┬──────────────┘
                              ↓
                       ┌────────────┐
                       │  EVIDENCE  │
                       └─────┬──────┘
                             ↓
                    ┌─────────────────┐
                    │   VERIFIER      │ ⇄ [ Independent Checkers / Live APIs ]
                    └───────┬─────────┘
                            │
                     ┌──────┴──────┐
                     │             │
                   PASS          FAIL
                     │             │
                     ↓             ↓
                  DELIVER       RECOVERY
                                   │
                                   ↓
                                REPLAN
```

### The Execution Model

```text
        PROMPT-DRIVEN AGENTS                        VERACT RUNTIME
        
               Prompt                                   Mission
                 ↓                                         ↓
               Model                                 Success Contract
                 ↓                                         ↓
              Tool Call                                Execution DAG
                 ↓                                         ↓
              Answer                                   Sandboxed Execution
                 ↓                                         ↓
         (Self-Judged: "Looks good")                   Evidence Collection
                                                           ↓
                                                      Independent Verification
                                                           ↓
                                                    ┌──────┴──────┐
                                                  PASS          FAIL
                                                    ↓             ↓
                                                 Deliver       Recover → Replan
```

---

## Security by Default

Untrusted models and third-party tools cannot be given raw system access. Veract implements least privilege across the entire lifecycle:

```text
┌──────────────────────────────────────────────────────────────┐
│                        VERACT RUNTIME                        │
│                                                              │
│  Mission Input                                               │
│     ↓                                                        │
│  Scope Guard           (Refuses out-of-workspace paths)      │
│     ↓                                                        │
│  Capability Broker     (Enforces filesystem & network ACLs)  │
│     ↓                                                        │
│  Approval Engine       (Interactive prompts for new code)    │
│     ↓                                                        │
│  Container Sandbox     (Read-only root, memory/CPU caps)     │
│     ↓                                                        │
│  Audit Trail           (Append-only JSONL event log)         │
└──────────────────────────────────────────────────────────────┘
```

1. **Scope Guard**: Any mission referencing paths outside the workspace (e.g. `C:\Windows\win.ini` or `/etc/passwd`) is refused immediately before calling the LLM or touching tools.
2. **Capability Broker**: Filesystem reads and writes are restricted to workspace roots. `.agent/` and `.git/` are immutable to the agent. Outbound network traffic is limited to HTTP/HTTPS, blocking private subnets (`127.0.0.1`, `10.0.0.0/8`, `169.254.0.0/16`).
3. **Execution Rules**: The agent cannot execute arbitrary shell scripts or code it just wrote without human approval. Banned flags (`python -c`, `pytest -p evil_plugin`) are blocked at the argv parser.
4. **Secret Scrubbing**: API keys (`sk-...`, `AKIA...`, `ghp_...`) and email addresses are automatically stripped from web search queries and redacted from disk deliverables.
5. **Container Sandboxing**: For untrusted code, Docker mode enforces `--network none`, `--read-only` root, and hard memory/CPU limits.

---

## Benchmarks

Veract has been evaluated across 240 controlled offline trials and live head-to-head runs on small local models (`llama3.2-3B`, 16k context):

### 1. Controlled Held-Out Evaluations (Offline)
Tested against a simulated weak model reproducing small-model failure modes (dropped args, hallucinated citations, prompt injection, and weak contracts):

| Metric | Set 1 (Unhardened Baseline) | Set 1 (Veract Hardened) | Set 2 (Frozen Held-Out) | 5 Repeated Seeds (150 trials) |
|---|:---:|:---:|:---:|:---:|
| **Solved Tasks** | 40 / 60 | **57 / 60** | **30 / 30** | **147 / 150 (98.0%)** |
| **False Passes** | 11 | **0** | **0** | **0 (0.0%)** |
| **Canary / Secret Leaks** | 0 | **0** | **0** | **0 (0.0%)** |
| **Path Leaks in Search** | 6 | **0** | **0** | **0 (0.0%)** |
| **Runtime Crashes** | 0 | **0** | **0** | **0 (0.0%)** |

> *Results are empirical measurements from our reproducible test harness (`bench/heldout.py`). Full methodology: [`bench/HELDOUT.md`](bench/HELDOUT.md). Raw data: [`bench/heldout_runs/all-5seeds.json`](bench/heldout_runs/all-5seeds.json).*

### 2. Live Head-to-Head Comparison (Ollama `llama3.2-16k`)
Evaluated across 5 complex real-world tasks (PyPI registry verification, math debugging, data aggregation, multi-source research) against leading agent runtimes under a 180s–400s deadline:

| Agent Runtime | Tasks Passed | False Passes | Average Time | Failure Mode |
|---|:---:|:---:|:---:|---|
| **Veract** | **4 / 5** | **0** | **~13.3s** | Clean, verified deliverables; 1 honest timeout on dead upstream API |
| **Hermes Agent** | 0 / 5 | 0 | 180s+ (Timeout) | Stalled on 16k system prompt context; dropped tool arguments |
| **OpenClaw** | 0 / 5 | 0 | 300s+ (Timeout) | Context overflow on large prompts; crashed on Windows file locks |

*Raw execution logs: [`bench/logs/`](bench/logs/) &nbsp;·&nbsp; Detailed report: [`bench/REPORT.md`](bench/REPORT.md)*

---

## Engineering Principles

Veract's architecture was shaped by analyzing empirical failure logs in existing agents:

- **Independent Contract Floor**: The agent cannot negotiate away its success criteria. A research task must prove entity count, uniqueness, source citation, and live URL resolution. The presence of an empty file will never satisfy the contract.
- **Query Sanitization**: Agents frequently leak private local filesystem paths into public search engines when trying to understand a user request. Veract strips paths, emails, and credentials prior to sending web search requests.
- **Exact-Once Patching**: Code edits require verified single-instance matches with immediate read-back verification. Ambiguous edits or partial deletes are aborted and sent back to the recovery engine.
- **Deadline-Aware Checkpoints**: If an agent runs out of time, Veract saves partial verified progress, stores an inspectable audit trace, and returns an honest `partial` state rather than hanging indefinitely.

---

## Quick Start

### Installation

```bash
pip install veract
```

### Environment Configuration

Veract works with any OpenAI-compatible server:

```bash
# Local Ollama (Recommended)
export AGENT_LLM_BASE_URL=http://localhost:11434/v1
export AGENT_LLM_MODEL=llama3.2
export AGENT_CTX_TOKENS=16384

# Or vLLM / LM Studio / OpenAI
export AGENT_LLM_BASE_URL=https://api.openai.com/v1
export AGENT_LLM_API_KEY=sk-...
export AGENT_LLM_MODEL=gpt-4o-mini
```

### Basic Commands

```bash
# Execute a mission
veract run "Fix failing tests in tests/test_calc.py without modifying the test file"

# Check mission status across workspace
veract status

# Inspect full capability audit log
veract audit <mission-id>

# Resume an interrupted mission from last checkpoint
veract resume <mission-id>

# Query long-term memory
veract memory "vector databases"

# Inspect mission evidence and contract
veract show <mission-id>
```

---

## Configuration (`.agent/policy.json`)

Configure permissions, sandboxing, and MCP servers per workspace:

```json
{
  "net_domains": ["*"],
  "exec_new_code": "approve",
  "approve": [
    {"action": "execute", "pattern": "python -m pytest*"}
  ],
  "search": {
    "provider": "searxng",
    "url": "http://127.0.0.1:8888"
  },
  "sandbox": {
    "mode": "docker",
    "image": "agent-runtime-sandbox",
    "memory_mb": 2048
  },
  "mcp": {
    "filesystem": {
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  },
  "mcp_allow": ["filesystem.read_*"]
}
```

- **Sandbox Modes**:
  - `docker` *(Recommended for untrusted code)*: Network-isolated container with read-only root and memory/CPU limits.
  - `limits` *(POSIX environments)*: Resource limits via `setrlimit` (CPU, memory, file size).
  - `none`: Direct host execution with broker auditing.
- **Model Context Protocol (MCP)**: Native stdio client. Tools are registered as `mcp.<server>.<tool>` and subject to capability gating.
- **Interactive Approval**: When an agent attempts an ungranted sensitive action, you receive an interactive `y / N / a` (always) terminal prompt.

---

## Honest Status Values

Veract enforces rigorous status distinctions:

| Status | Meaning |
|---|---|
| `passed` | Every criterion was independently verified with reproducible proof. |
| `failed` | Mission could not be satisfied within the replan/retry budget. Failing checks are detailed in the artifact. |
| `refused` | The mission violates policy (e.g., path traversal). Refused before LLM invocation or tool execution. |
| `partial` | Mission was terminated by deadline; only verified evidence was committed. |
| `unverified` | Supported only by model self-judgment. **Never reported as passed**. |

---

## Technical Internals

For contributors and developers building on Veract:

| Subsystem | File | Responsibility |
|---|---|---|
| **Scope Guard** | [`guard.py`](agent_runtime/guard.py) | Pre-execution path inspection, query sanitization, and secret redaction. |
| **Contract Engine** | [`contract.py`](agent_runtime/contract.py) | Success contract floor and deterministic verification rules. |
| **Compiler** | [`compiler.py`](agent_runtime/compiler.py) | Converts natural language requests into objectives and criteria graphs. |
| **Planner** | [`planner.py`](agent_runtime/planner.py) | Generates dependency-aware DAG execution plans. |
| **Executor** | [`executor.py`](agent_runtime/executor.py) | Wave-based concurrent tool execution and deadline management. |
| **Verifier** | [`verifier.py`](agent_runtime/verifier.py) | Independent claim verification, content re-derivation, and live checks. |
| **Recovery** | [`recovery.py`](agent_runtime/recovery.py) | Failure classification and self-healing repair strategies. |
| **Capability Broker** | [`capabilities.py`](agent_runtime/capabilities.py) | Permission enforcement, filesystem isolation, and JSONL audit logging. |
| **Engines** | [`engines.py`](agent_runtime/engines.py) | Deterministic fast-paths for research, coding, and tabular data. |
| **Memory** | [`memory.py`](agent_runtime/memory.py) | SQLite-backed episodic and semantic memory with confidence decay. |
| **MCP Client** | [`mcp.py`](agent_runtime/mcp.py) | Model Context Protocol client with broker gating. |

---

## What Veract Is Not

- **Not a hosted SaaS**: Veract is a local-first Python library and CLI. Your data, code, and keys stay on your machine.
- **Not locked to proprietary models**: Designed specifically to make small, local open-weights models (3B–14B) reliable.
- **Not an unconstrained auto-coder**: Veract does not rewrite its own codebase or bypass approval boundaries.
- **Not a replacement for virtualization**: In `limits` mode, resource limits apply, but full system isolation requires `mode: "docker"`.

---

## Contributing

We welcome contributions to Veract!

```bash
git clone https://github.com/Diwakarsrd/Veract.git
cd Veract
pip install -e .
python -m unittest discover -s tests -v
ruff check .
```

Please review [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md) before submitting pull requests.

---

## License

Licensed under the [MIT License](LICENSE).
