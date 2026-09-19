"""Mission compiler: free text -> objective + constraints + machine-checkable success criteria."""
import json
import re
from .llm import extract_json
from .models import Mission, Criterion, CHECKS
from . import contract

RESEARCH = ("find", "research", "list", "compare", "investigate", "who ", "which ", "companies", "sources")
CODING = ("implement", "fix", "refactor", "bug", "function", "write code", "unit test", "repo")
SYSTEM = ("You compile a user's mission into JSON: {\"objective\": str, \"kind\": \"research|coding|general\", "
          "\"constraints\": {}, \"criteria\": [{\"id\": str, \"description\": str, \"check\": one of "
          "min_items|unique_items|all_sourced|file_exists|command_ok|contains|llm, \"params\": {}}]}. "
          "params: min_items {n}, file_exists {path}, command_ok {command}, contains {path,text}, llm {question}. "
          "Criteria must be objectively verifiable. Reply with JSON only.")


def _heuristic(text, mid):
    low = text.lower()
    kind = "coding" if any(k in low for k in CODING) else "research" if any(k in low for k in RESEARCH) else "general"
    crit, cid = [], iter(range(1, 99))
    n = re.search(r"\b(\d{1,5})\s+(?:\w+\s+){0,2}?[a-z]{3,}", text)
    if kind == "research":
        crit.append(Criterion(f"c{next(cid)}", f"at least {n.group(1) if n else 1} item(s)", "min_items", {"n": int(n.group(1)) if n else 1}))
        crit.append(Criterion(f"c{next(cid)}", "no duplicate items", "unique_items"))
        crit.append(Criterion(f"c{next(cid)}", "every item cites a source that was actually retrieved", "all_sourced"))
    f = re.search(r"(?:to|into|as|in)\s+([\w./-]+\.(?:md|txt|json|csv|py|html))\b", text)
    if f:
        crit.append(Criterion(f"c{next(cid)}", f"file {f.group(1)} exists", "file_exists", {"path": f.group(1)}))
    if kind == "coding" and "test" in low:
        crit.append(Criterion(f"c{next(cid)}", "test suite passes", "command_ok", {"command": "python -m pytest -q"}))
    return Mission(mid, text, text.strip().rstrip("."), kind, {}, crit)


def valid_criteria(crits):
    """Reject criteria that can never be satisfied (a URL is not a file path) and drop duplicates."""
    seen, out = set(), []
    for c in crits:
        p = c.params or {}
        if c.check in ("file_exists", "contains"):
            path = str(p.get("path", ""))
            if not path or "://" in path or path.startswith("mailto:"):
                continue
        elif c.check == "min_items":
            try:
                if int(p.get("n", 0)) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
        elif c.check == "command_ok" and not str(p.get("command", "")).strip():
            continue
        elif c.check == "llm" and not str(p.get("question", "")).strip() and not c.description:
            continue
        key = (c.check, json.dumps(p, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def compile_mission(text, llm=None, memory_hints="", workspace=None):
    """LLM (or rules) proposes criteria; the contract floor then guarantees the minimum the text demands."""
    mid = Mission.new_id()
    base = None
    if llm:
        try:
            d = extract_json(llm.complete(SYSTEM, f"Mission: {text}\n{memory_hints}"))
            crit = valid_criteria([Criterion(str(c["id"]), str(c.get("description", c["check"])), c["check"],
                                             c.get("params") or {})
                                   for c in d["criteria"] if isinstance(c, dict) and c.get("check") in CHECKS])
            base = Mission(mid, text, str(d.get("objective") or text), str(d.get("kind") or "general"),
                           d.get("constraints") if isinstance(d.get("constraints"), dict) else {}, crit)
        except Exception:
            base = None  # fall back to rules
    if base is None:
        base = _heuristic(text, mid)
    kind, fl = contract.floor(text, workspace, base.kind)
    base.kind = kind if kind != "general" else base.kind
    base.criteria = contract.merge(base.criteria, fl)
    base.constraints = dict(base.constraints or {})
    base.constraints["verifiable"] = contract.verifiable(base.criteria)
    return base
