"""Safety guards shared by tools, compiler and runtime.

* scope_check  — local paths a mission references are checked against the broker BEFORE any planning,
                 so an out-of-workspace request is refused up front (no LLM call, no web search, no leak).
* sanitize_query — local paths, emails and secret-looking strings never leave the machine in a search query.
* scan_injection — fetched pages are flagged when they contain instruction-like text aimed at the agent.
* redact        — secret-looking strings are masked before anything is written to disk or the artifact.
"""
import re

_WIN = r"[A-Za-z]:[\\/][^\s'\"`<>|]*"
_POSIX = r"(?<![\w.])/(?:etc|usr|var|root|proc|home|bin|lib|sys|opt|tmp|private|Users|Library|dev|mnt|srv|boot)(?:/[^\s'\"`<>|]*)?"
_HOME = r"(?<![\w])~[\\/][^\s'\"`<>|]*"
_UP = r"(?<![\w])\.\.[\\/][^\s'\"`<>|]*"
LOCAL_PATH = re.compile("|".join([_WIN, _POSIX, _HOME, _UP]))
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                              # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),                     # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),                          # OpenAI/Anthropic-style keys
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b"),                    # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                          # Google API key
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
]

INJECTION = re.compile(
    r"(?i)(ignore (all |any |the )?(previous|prior|above) (instructions|prompts?)|disregard (the|your) "
    r"(instructions|system prompt)|you are now|new instructions:|system prompt|out-of-band user message|"
    r"<\s*/?\s*(system|assistant|tool_result|untrusted_tool_result)\b|exfiltrat|send (the|your) (api key|password|"
    r"credentials)|run the following command|curl\s+https?://\S+\s*\|\s*(sh|bash))")


def local_paths(text):
    return [m.group(0).rstrip(".,;:)") for m in LOCAL_PATH.finditer(text or "")]


def scope_check(text, broker):
    """Return a list of (path, action, reason) the policy would deny for this mission's text."""
    low = (text or "").lower()
    writes = bool(re.search(r"\b(write|save|delete|remove|overwrite|modify|edit|append|copy .* to|move)\b", low))
    denied = []
    for p in dict.fromkeys(local_paths(text)):
        for action in (("read", "write") if writes else ("read",)):
            ok, reason = broker.check(action, p)
            if not ok and "outside" in reason:
                denied.append((p, action, reason))
                break
    return denied + command_scope(text, broker)


COMMAND = re.compile(r"`([^`\n]{2,300})`")
PROGRAMS = {"python", "python3", "py", "pytest", "bash", "sh", "zsh", "cmd", "powershell", "pwsh", "cat", "type",
            "rm", "del", "curl", "wget", "node", "npm", "npx", "pip", "pip3", "git", "ls", "dir", "chmod", "sudo",
            "perl", "ruby", "nc", "ssh", "scp", "docker", "kubectl"}


def command_scope(text, broker):
    """A command the mission REQUIRES running ("Run `...`") is checked up front, like paths.
    Optional suggestions ("you may run `...`") are left to the broker at execution time."""
    out = []
    for m in COMMAND.finditer(text or ""):
        cmd = m.group(1).strip()
        prog = re.split(r"[\s]", cmd, maxsplit=1)[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if prog not in PROGRAMS:
            continue
        before = (text[max(0, m.start() - 40):m.start()]).lower()
        if not re.search(r"\b(run|execute|exec|launch|invoke)\s*$", before) or \
                re.search(r"\b(may|can|could|optionally|if you (?:want|like))\b", before):
            continue
        ok, reason = broker.check("execute", cmd)
        if not ok and "needs approval" not in reason:
            out.append((cmd, "execute", reason))
    return out


def sanitize_query(q):
    q = LOCAL_PATH.sub(" ", str(q or ""))
    q = EMAIL.sub(" ", q)
    for rx in SECRETS:
        q = rx.sub(" ", q)
    return re.sub(r"\s+", " ", q).strip()[:300]


def scan_injection(text):
    return sorted({m.group(0).lower()[:60] for m in INJECTION.finditer(text or "")})[:5]


def redact(text):
    if not isinstance(text, str):
        return text
    for rx in SECRETS:
        text = rx.sub("[REDACTED]", text)
    return text
