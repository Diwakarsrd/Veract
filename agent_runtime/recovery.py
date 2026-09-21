"""Failure classification + recovery decisions."""
from .capabilities import PermissionDenied
from .llm import BudgetExceeded

TRANSIENT_HINTS = ("timed out", "timeout", "temporarily", "connection", "reset", "503", "502", "429", "urlopen")


def classify(exc) -> str:
    if isinstance(exc, BudgetExceeded):
        return "budget"
    if isinstance(exc, PermissionDenied):
        return "permission"
    msg = str(exc).lower()
    if msg.startswith("no_llm"):
        return "no_llm"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(h in msg for h in TRANSIENT_HINTS):
        return "transient"
    if isinstance(exc, (KeyError, ValueError, TypeError)):
        return "bad_input"
    return "tool_error"


RETRYABLE = {"transient"}
ABORT = {"permission", "no_llm", "budget"}     # replanning cannot fix these; needs the human (or more time)


def decide(report, fixes):
    """Map failed criteria to deterministic fixes where possible, else replan."""
    new, replan, why = {}, False, []
    for r in report["results"]:
        if r["passed"]:
            continue
        if r["check"] == "unique_items" and not fixes.get("dedupe"):
            new["dedupe"] = True; why.append("dedupe items")
        elif r["check"] == "all_sourced" and not fixes.get("drop_unsourced"):
            new["drop_unsourced"] = True; why.append("drop unsourced/unretrieved items")
        else:
            replan = True; why.append(f"{r['check']} failed: {r['detail']}")
    return {"fixes": new, "replan": replan, "why": why}
