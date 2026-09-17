"""Capability broker: every action is checked against policy and written to an inspectable audit log.

Design rules (each one closes a hole seen in the v0.1 benchmark run):
* Network reads are http(s) only. ``file://`` and other schemes are denied (no local-file exfil via urlopen).
* File reads/writes must resolve inside the allowed roots; the runtime's own state (``.agent/``) and ``.git/``
  are never writable by the agent, so it cannot rewrite its audit log, checkpoints or history.
* Commands are matched on argv, not on a shell glob: a small set of programs and sub-commands, every
  path-like argument must stay inside the workspace, ``python -c`` / pytest plugins are refused, and code the
  agent created during the mission cannot be executed without a human approving it.
* Every decision — allowed or denied — is appended to the mission's JSONL audit log.
"""
import fnmatch
import ipaddress
import json
import os
import re
import shlex
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_POLICY = {
    "fs_read": ["{workspace}"],
    "fs_write": ["{workspace}"],
    "fs_protected": [".agent", ".git"],          # never writable by the agent (relative to workspace)
    "net_domains": ["*"],
    "block_private_net": True,
    "private_allow": [],                           # hosts exempt from the private-net block (e.g. self-hosted SearXNG)
    "mcp_allow": [],                               # "server.tool" patterns callable without approval
    "exec_allow": [],                              # extra user globs; still subject to the argv/path rules
    "exec_new_code": "approve",                    # approve|deny|allow: running code the agent itself created
}

# program -> allowed first arguments (None = any args, still path-checked)
EXEC_RULES = {
    "python": {"-m": {"pytest", "unittest"}},
    "python3": {"-m": {"pytest", "unittest"}},
    "py": {"-m": {"pytest", "unittest"}},
    "pytest": None,
    "git": {"status", "diff", "log", "show"},
    "ls": None,
    "dir": None,
}
BANNED_ARGS = {"-c", "--rootdir", "-p", "--pyargs", "--confcutdir", "--import-mode", "-o", "--override-ini"}
NET_SCHEMES = ("http", "https")


def plausible_test_command(cmd):
    """Static pre-check (no workspace needed): python -m pytest|unittest or pytest, no banned/absolute args."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if not argv:
        return False
    prog = Path(argv[0]).name.lower().removesuffix(".exe")
    if prog in ("python", "python3", "py"):
        if argv[1:3] not in (["-m", "pytest"], ["-m", "unittest"]):
            return False
    elif prog != "pytest":
        return False
    return not any(a.split("=", 1)[0] in BANNED_ARGS or re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]|~|\.\.)", a.split("=", 1)[-1])
                   for a in argv[1:])


class PermissionDenied(Exception):
    pass


def _is_pathlike(tok):
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]|~|\.\.(?:[\\/]|$))", tok)) or "/" in tok or "\\" in tok


class CapabilityBroker:
    def __init__(self, workspace, audit_path, policy=None, approver=None):
        self.workspace = Path(workspace).resolve()
        self.audit_path = Path(audit_path)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        p = dict(DEFAULT_POLICY)
        p.update(policy or {})
        self.policy = p
        self.approver = approver      # callable(request_dict) -> bool, for interactive approval
        self.grants = []              # (action, pattern, expires)
        self.created = set()          # files the agent created during this mission (resolved paths)
        self._lock = threading.Lock()

    def grant(self, action, pattern, ttl=3600):
        self.grants.append((action, pattern, time.time() + ttl))

    def _roots(self, key):
        return [Path(r.replace("{workspace}", str(self.workspace))).expanduser().resolve() for r in self.policy[key]]

    def path(self, target):
        t = str(target)
        if os.name != "nt":
            if re.match(r"^[A-Za-z]:[\\/]", t):          # a Windows drive path is never inside a POSIX workspace
                return Path("/__windows_drive__") / t[0]
            t = t.replace("\\", "/")                    # "..\\secret" must not become a harmless filename
        p = Path(t).expanduser()
        return (p if p.is_absolute() else self.workspace / p).resolve()

    def inside(self, target, key="fs_read"):
        try:
            rp = self.path(target)
        except (OSError, ValueError):
            return False
        return any(rp.is_relative_to(r) for r in self._roots(key))

    def _protected(self, rp):
        return any(rp.is_relative_to((self.workspace / d).resolve()) for d in self.policy["fs_protected"])

    # ------------------------------------------------------------------ exec
    def _eval_exec(self, cmd):
        try:
            argv = shlex.split(cmd, posix=os.name != "nt")
        except ValueError as e:
            return False, f"unparseable command: {e}"
        if not argv:
            return False, "empty command"
        prog = Path(argv[0]).name.lower()
        prog = prog[:-4] if prog.endswith(".exe") else prog
        user_ok = any(fnmatch.fnmatch(cmd, pat) for pat in self.policy["exec_allow"])
        if prog not in EXEC_RULES and not user_ok:
            return False, f"program '{prog}' not allow-listed"
        rule = EXEC_RULES.get(prog)
        args = argv[1:]
        if isinstance(rule, dict):                                  # python -m pytest|unittest
            if len(args) < 2 or args[0] not in rule or args[1] not in rule[args[0]]:
                if not user_ok:
                    return False, f"only '{prog} -m pytest|unittest' allowed"
        elif isinstance(rule, set):                                 # git sub-commands
            if not args or args[0] not in rule:
                if not user_ok:
                    return False, f"'{prog} {args[0] if args else ''}' not allowed"
        for a in args:
            key = a.split("=", 1)[0]
            if key in BANNED_ARGS:
                return False, f"argument '{key}' not allowed"
            if _is_pathlike(a) and not self.inside(a.split("=", 1)[-1] if "=" in a else a):
                return False, f"argument '{a}' points outside the workspace"
        new_code = sorted(str(p.relative_to(self.workspace)) for p in self.created
                          if p.suffix in (".py", ".pyc", ".pth", ".so", ".dll") and p.exists())
        if new_code:
            mode = self.policy["exec_new_code"]
            if mode == "deny":
                return False, f"workspace contains code created by the agent: {new_code}"
            if mode == "approve":
                return False, f"needs approval: would execute code created by the agent: {new_code}"
        return True, "command allowed (argv rules)"

    # ------------------------------------------------------------------ evaluate
    def _evaluate(self, action, target):
        for a, pat, exp in self.grants:
            if a == action and exp > time.time() and fnmatch.fnmatch(target, pat):
                return True, "temporary grant", exp
        if action == "read" and "://" in str(target):
            u = urlparse(target)
            if u.scheme.lower() not in NET_SCHEMES:
                return False, f"scheme '{u.scheme}' not allowed (http/https only)", None
            host = u.hostname or ""
            if not host:
                return False, "url without host", None
            if not any(fnmatch.fnmatch(host, d) for d in self.policy["net_domains"]):
                return False, f"domain {host} not allowed", None
            if self.policy["block_private_net"] and host not in self.policy.get("private_allow", []):
                try:
                    for info in socket.getaddrinfo(host, None):
                        ip = ipaddress.ip_address(info[4][0])
                        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                            return False, "private/loopback address blocked", None
                except socket.gaierror:
                    pass
            return True, "domain allowed", None
        if action in ("read", "write"):
            if "://" in str(target):
                return False, "urls cannot be written", None
            try:
                rp = self.path(target)
            except (OSError, ValueError) as e:
                return False, f"bad path: {e}", None
            ok = any(rp.is_relative_to(r) for r in self._roots("fs_read" if action == "read" else "fs_write"))
            if not ok:
                return False, "path outside allowed roots", None
            if action == "write" and self._protected(rp):
                return False, "runtime state (.agent/.git) is not writable by the agent", None
            return True, "inside allowed roots", None
        if action == "execute":
            ok, why = self._eval_exec(target)
            return ok, why, None
        if action == "mcp":
            if any(fnmatch.fnmatch(target, p) for p in self.policy.get("mcp_allow", [])):
                return True, "mcp tool allow-listed", None
            return False, "needs approval: mcp tool not in mcp_allow", None
        return False, "unknown action", None

    def check(self, action, target):
        """Dry-run evaluation (no audit, no approval prompt)."""
        return self._evaluate(action, target)[:2]

    def note_created(self, target):
        rp = self.path(target)
        self.created.add(rp)

    def require(self, who, mission, step, tool, action, target, why=""):
        ok, reason, exp = self._evaluate(action, target)
        if not ok and self.approver and not reason.startswith(("scheme", "runtime state")):
            req = dict(who=who, tool=tool, action=action, target=target, why=why, reason=reason)
            if self.approver(req):
                ok, reason = True, "approved interactively"
        self.audit(who, mission, step, tool, action, target, why, ok, reason, exp)
        if not ok:
            raise PermissionDenied(f"{action} {target!r} denied: {reason}")
        return {"allowed": ok, "reason": reason}

    def audit(self, who, mission, step, tool, action, target, why, allowed, reason, exp=None):
        rec = {"ts": time.time(), "mission": mission, "step": step, "who": who, "tool": tool, "action": action,
               "target": target, "why": why, "allowed": allowed, "reason": reason, "expires": exp}
        with self._lock, open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
