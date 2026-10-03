"""User configuration: `.agent/policy.json` in the workspace (the agent itself can never write `.agent/`).

{
  "net_domains": ["*"],                 # broker keys (see capabilities.DEFAULT_POLICY)
  "exec_new_code": "approve",
  "approve": [{"action": "execute", "pattern": "python -m pytest*"}],   # auto-approved when policy denies
  "search": {"provider": "duckduckgo"},  # duckduckgo | searxng {url} | brave {api_key_env}
  "sandbox": {"mode": "limits", "cpu_seconds": 120, "memory_mb": 1024},   # none | limits | docker {image}
  "mcp": {"files": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]}}
}
"""
import fnmatch
import json
import sys
from pathlib import Path
from .capabilities import DEFAULT_POLICY

SCHEMA = 2
RUNTIME_KEYS = {"approve", "search", "sandbox", "mcp"}
DEFAULTS = {
    "approve": [],
    "search": {"provider": "duckduckgo"},
    "sandbox": {"mode": "limits", "cpu_seconds": 300, "memory_mb": 2048, "image": "python:3.12-slim"},
    "mcp": {},
}


class ConfigError(ValueError):
    pass


def policy_path(workspace):
    return Path(workspace) / ".agent" / "policy.json"


def load(workspace):
    """Return (broker_policy, runtime_config). Unknown keys are an error, not silently ignored."""
    f = policy_path(workspace)
    raw = {}
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ConfigError(f"{f}: invalid JSON ({e})") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{f}: must be a JSON object")
    unknown = set(raw) - set(DEFAULT_POLICY) - RUNTIME_KEYS
    if unknown:
        raise ConfigError(f"{f}: unknown key(s) {sorted(unknown)}; allowed: {sorted(set(DEFAULT_POLICY) | RUNTIME_KEYS)}")
    broker = {k: v for k, v in raw.items() if k in DEFAULT_POLICY}
    rt = {k: (dict(DEFAULTS[k], **raw[k]) if isinstance(DEFAULTS[k], dict) and isinstance(raw.get(k), dict)
              else raw.get(k, DEFAULTS[k])) for k in RUNTIME_KEYS}
    if rt["sandbox"].get("mode") not in ("none", "limits", "docker"):
        raise ConfigError("sandbox.mode must be none | limits | docker")
    if rt["search"].get("provider") not in ("duckduckgo", "searxng", "brave"):
        raise ConfigError("search.provider must be duckduckgo | searxng | brave")
    for rule in rt["approve"]:
        if not isinstance(rule, dict) or not {"action", "pattern"} <= set(rule):
            raise ConfigError("approve rules look like {\"action\": \"execute\", \"pattern\": \"python -m pytest*\"}")
    return broker, rt


def effective(workspace):
    broker, rt = load(workspace)
    out = dict(DEFAULT_POLICY)
    out.update(broker)
    out.update(rt)
    return out


def init(workspace):
    f = policy_path(workspace)
    if f.exists():
        raise ConfigError(f"{f} already exists")
    f.parent.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in DEFAULT_POLICY.items()}
    body.update({k: v for k, v in DEFAULTS.items()})
    f.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return f


def make_approver(rules=(), interactive=False, yes=False, ask=None, out=sys.stderr):
    """Approval chain: --yes > policy rules > interactive prompt (y / N / a = always this session) > deny."""
    session = []

    def approve(req):
        target, action = str(req.get("target", "")), req.get("action")
        if yes:
            return True
        for r in list(rules) + session:
            if r["action"] == action and fnmatch.fnmatch(target, r["pattern"]):
                return True
        if not interactive:
            return False
        prompt = (f"\n[approval needed] {req.get('tool')} wants to {action} {target!r}\n"
                  f"  why: {req.get('why') or '-'}\n  policy said: {req.get('reason', '-')}\n  allow? [y]es / [N]o / [a]lways this session: ")
        try:
            ans = (ask or input)(prompt).strip().lower()
        except EOFError:
            return False
        if ans == "a":
            session.append({"action": action, "pattern": target})
            return True
        return ans in ("y", "yes")
    return approve
