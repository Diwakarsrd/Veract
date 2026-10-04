# Security

## Reporting
Report vulnerabilities privately to **gsunilkumar6018@gmail.com** with "agent-runtime security" in the subject.
Please include the mission text, policy file and `agent audit <id>` output. Expect a reply within 7 days.
Do not open public issues for exploitable bugs.

## Threat model
**Assets:** files outside the workspace, credentials in the environment, the host machine, the user's network,
the integrity of the runtime's own audit log and checkpoints, and the honesty of the reported status.

**Untrusted inputs:**
- the mission text, when it is pasted from elsewhere
- every model output
- every fetched web page
- every file in the workspace (an untrusted repo)
- MCP tool output
- plugins (until trusted)

| Layer | What it enforces | Known limits |
|---|---|---|
| Scope guard (`guard.py`) | Refuses missions naming out-of-workspace paths or *required* forbidden commands, before any model call | Regex-based, so it can be bypassed by paraphrase. It is a fast-fail, **not** the security boundary |
| Capability broker (`capabilities.py`) | **The boundary.** Every read, write, fetch, command and MCP call is checked and audited. Paths are resolved (symlinks followed) and must stay inside allowed roots; `.agent/` and `.git/` can't be written; http(s) only; private/loopback/link-local IPs blocked; commands matched on argv; agent-created code needs approval | Time-of-check vs time-of-use: a path can change between the check and the open. DNS rebinding isn't handled |
| Command sandbox (`tools.run_command`) | `limits` (default, POSIX): CPU, memory, file-size and core rlimits, own process group, secrets removed from the environment. `docker`: no network, read-only root, dropped capabilities, pids/memory/CPU caps | `limits` mode has **no filesystem or network isolation**, and on Windows only the timeout applies. Use `docker` mode for untrusted repos |
| Verifier (`verifier.py`) | Never follows paths outside the workspace; URL checks refuse private hosts | `llm` criteria are only as good as the judge. Use a separate `AGENT_JUDGE_*` model |
| Plugins | Load only with a pinned hash (`agent trust`) | A trusted plugin runs with full user privileges |
| MCP servers | Each call gated by `mcp_allow` or approval, and audited | The server process runs with user privileges, outside the sandbox |
| Redaction (`guard.redact`) | Masks common key/token formats in files and artifacts | Pattern-based; it won't catch every secret format |

## Recommended settings for untrusted repositories
```json
{ "sandbox": {"mode": "docker", "image": "agent-runtime-sandbox"},
  "net_domains": ["api.github.com", "pypi.org"],
  "exec_new_code": "deny" }
```
