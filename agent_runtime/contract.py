"""Contract floor: the minimum success criteria a mission gets, whatever the model wrote.

In the v0.1 benchmark a 3B model compiled "research 10 vector databases into dbs.md" into a single
criterion — `file_exists dbs.md` — and the verifier then passed a file of junk. The model may ADD
criteria, but it can never weaken the floor below what the mission text itself demands:

  research  -> min_items n, unique, all_sourced, grounded (+ file lines/urls/unique when a file is promised)
  coding    -> the test command passes AND every pre-existing test file is byte-identical
  data      -> every requested figure is recomputed from the raw files by Python
  any file  -> the promised file exists inside the workspace

Criteria that point outside the workspace are dropped (a criterion like `file_exists C:\\WINDOWS\\win.ini`
let v0.1 mark a forbidden read as PASSED).
"""
import hashlib
import json
import re
from pathlib import Path
from .models import Criterion

EXT = r"(?:md|txt|json|csv|html|py|yaml|yml)"
DELIVER = re.compile(
    rf"(?:\b(?:write|save|create|output|produce|generate|export|store|put|record|list)\b[^.\n]{{0,80}}?"
    rf"|\b(?:to|into|as|named|called|in)\s+)`?([\w][\w./-]*\.{EXT})\b", re.I)
RESEARCH = re.compile(r"\b(research|find|list|identify|investigate|compile|look up|collect|gather|name)\b", re.I)
CODING = re.compile(r"\b(fix|repair|debug|implement|refactor|make .*tests? pass|failing tests?|bug)\b", re.I)
DATA = re.compile(r"\b(csv|json|data files?|spreadsheet|rows?)\b", re.I)
AGG = re.compile(r"\b(total|sum|average|mean|top|count|highest|lowest|minimum|maximum|min|max|one line per)\b", re.I)
URL_LINE = r"https?://\S+"
DETERMINISTIC = {"min_items", "unique_items", "all_sourced", "file_exists", "command_ok", "contains", "urls_ok",
                 "data_correct", "versions_correct", "file_hash", "file_lines", "file_unique", "grounded"}


def deliverables(text):
    out = []
    for m in DELIVER.finditer(text or ""):
        f = m.group(1).strip("`'\".,")
        if "://" not in f and not f.startswith(("/", "\\")) and ".." not in f:
            out.append(f)
    return list(dict.fromkeys(out))


def count_requested(text):
    m = re.search(r"\b(?:research|find|list|identify|investigate|compile|look up|collect|gather|name)\s+"
                  r"(?:the\s+|top\s+|at least\s+)?(\d{1,4})\b", text or "", re.I) or \
        re.search(r"\b(\d{1,4})\s+(?:[\w-]+\s+){0,3}?(?:projects?|tools?|items?|companies|libraries|databases|"
                  r"frameworks|sources|papers|articles|repos(?:itories)?|websites|products|startups|entries)\b",
                  text or "", re.I)
    return int(m.group(1)) if m else None


def _outside(path):
    p = str(path)
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]|~)", p)) or ".." in Path(p).parts or "://" in p


def _test_files(ws):
    if not ws:
        return []
    ws = Path(ws)
    return sorted(set(ws.glob("test*.py")) | set(ws.glob("*_test.py")) | set(ws.glob("tests/test*.py")))


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def classify(text, ws=None):
    low = (text or "").lower()
    if CODING.search(low) and (re.search(r"\.py\b|tests?\b|pytest|unittest", low) or _test_files(ws)):
        return "coding"
    if DATA.search(low) and AGG.search(low):
        return "data"
    if RESEARCH.search(low) and (count_requested(text) or re.search(r"\bsources?\b|\burls?\b|\blinks?\b", low)):
        return "research"
    return "general"


def test_command(text, ws=None):
    from .capabilities import plausible_test_command
    for c in re.findall(r"`((?:python3?|py|pytest)[^`]*)`", text or ""):
        if plausible_test_command(c.strip()):
            return c.strip()
    return "python -m pytest -q"


def floor(text, ws=None, kind=None):
    """Return (kind, [Criterion]) the mission must satisfy at minimum."""
    k = classify(text, ws)
    if k == "general" and kind in ("research", "coding", "data"):
        k = kind
    crit, files = [], deliverables(text)
    add = lambda cid, desc, check, params=None: crit.append(Criterion(cid, desc, check, params or {}))
    for i, f in enumerate(files, 1):
        add(f"F{i}", f"file {f} exists in the workspace", "file_exists", {"path": f})
    if k == "research":
        n = count_requested(text) or 1
        add("R1", f"at least {n} item(s)", "min_items", {"n": n})
        add("R2", "no duplicate items", "unique_items")
        add("R3", "every item cites a source that was actually retrieved", "all_sourced")
        add("R4", "every item is grounded in the page it cites", "grounded")
        for i, f in enumerate([f for f in files if f.endswith((".md", ".txt"))], 1):
            add(f"R5.{i}", f"{f} has at least {n} lines with a URL", "file_lines", {"path": f, "n": n, "pattern": URL_LINE})
            add(f"R6.{i}", f"{f} has no duplicate entries", "file_unique", {"path": f})
            add(f"R7.{i}", f"every URL in {f} resolves", "urls_ok", {"path": f})
    elif k == "coding":
        cmd = test_command(text, ws)
        add("C1", f"`{cmd}` passes", "command_ok", {"command": cmd})
        for i, t in enumerate(_test_files(ws), 1):
            rel = str(Path(t).relative_to(ws)).replace("\\", "/")
            add(f"C{i + 1}", f"test file {rel} untouched", "file_hash", {"path": rel, "sha256": _sha(t)})
    elif k == "data" and ws:
        try:
            from .engines import data_expectations
            exp = data_expectations(text, Path(ws))
        except Exception:
            exp = None
        if exp:
            exp.pop("lines", None)
            out = exp["path"]
            if out not in files:
                add("F0", f"file {out} exists in the workspace", "file_exists", {"path": out})
            add("D1", "every requested figure re-derived from the raw files", "data_correct", exp)
    return k, crit


def merge(model_crit, floor_crit):
    """Model criteria first (minus anything pointing outside the workspace), then the floor; dedupe by check+params."""
    seen, out = set(), []
    for c in list(model_crit) + list(floor_crit):
        p = c.params or {}
        if c.check in ("file_exists", "contains", "file_lines", "file_unique", "urls_ok", "file_hash",
                       "data_correct", "versions_correct") and _outside(p.get("path", "")):
            continue
        key = (c.check, json.dumps(p, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    ids = set()
    for c in out:                       # ids must be unique for reports
        while c.id in ids:
            c.id += "'"
        ids.add(c.id)
    return out


CONTENT = DETERMINISTIC - {"file_exists"}


def verifiable(criteria):
    """A pass needs at least one deterministic check of the CONTENT; "a file exists" proves nothing."""
    return any(c.check in CONTENT for c in criteria)
