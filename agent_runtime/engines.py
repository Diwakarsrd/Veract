"""Deterministic engines: mission shapes Python can execute exactly, without an LLM.

The router (detect) claims only high-confidence shapes and never claims missions
that point outside the workspace (safety). Each engine produces machine-checkable
criteria; the normal Verifier still re-derives every claim from disk / live APIs
afterwards (engine output is evidence, never trust).

Engines: research (GitHub search + URL validation), live_api (PyPI/npm/crates.io/
GitHub releases/Hacker News), coding (pytest feedback loop with LLM patches),
data (CSV/JSON aggregation computed in Python).
"""
from __future__ import annotations
import csv as _csv
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from .models import Criterion
from .llm import extract_json


def drop_bytecode(path):
    from .tools import drop_bytecode as _d
    _d(path)

UA = {"User-Agent": "agent-runtime/0.1 (+local)"}


class Skip(Exception):
    """This engine cannot confidently handle the mission; fall back to the normal pipeline."""


def _http(url, ctx=None, limit=2_000_000, timeout=20):
    if ctx is not None:
        ctx.require("read", url)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def _json(url, ctx=None, limit=4_000_000, timeout=20):
    return json.loads(_http(url, ctx, limit, timeout).decode("utf-8", "replace"))


# --------------------------------------------------------------------------- router
_OUTSIDE = re.compile(r"[A-Za-z]:\\|\.\.[\\/]|/(?:etc|usr|var|root|proc|home|bin|lib|sys|opt|tmp)/")


def _deliverables(text):
    """Distinct md/txt files the mission asks to write (>1 means the engine can't own it)."""
    return set(re.findall(r"\b[\w-]+\.(?:md|txt)\b", text))


def detect(text):
    """Conservative intent router -> engine name or None. Safety: never claims
    missions that reference paths outside the workspace."""
    if _OUTSIDE.search(text):
        return None
    low = text.lower()
    n_out = len(_deliverables(text))
    if re.search(r"\b(fix|repair|debug)\b|\bfailing tests?\b|make .*tests? pass", low) and \
       re.search(r"\.py\b|tests?\b|pytest|unittest", low):
        return "coding"
    if "hacker news" in low and re.search(r"\btop\b|front|stor(y|ies)", low):
        return "live_api"
    if re.search(r"\bversions?\b|\breleases?\b", low) and \
       re.search(r"\bpackages?\b|\bnpm\b|\bpypi\b|\bcrates?\b|\brepositories\b|\blibraries\b|\bregistry\b", low):
        return "live_api"
    if re.search(r"\bcsvs?\b|\bjson\b|data files|data/", low) and \
       re.search(r"\btotal\b|\bsum\b|average|mean|\btop\b|count|number of rows|highest|lowest|"
                 r"minimum|maximum|one line per|corrupt|malformed", low) and \
       not ("csv" in low and "json" in low) and n_out <= 1:            # one deterministic deliverable
        return "data"
    if re.search(r"\b(research|find|list|identify|investigate|compile|look up)\b", low) and \
       re.search(r"\b\d{1,4}\b", text) and n_out <= 1 and \
       re.search(r"\.(?:md|txt)\b|markdown|one line per|entries|sources", low):
        return "research"
    return None


def run(name, text, workspace, ctx_factory, llm):
    ws = Path(workspace)
    ctx = ctx_factory("engine", "e1")
    if name == "research":
        return _run_research(text, ws, ctx)
    if name == "live_api":
        return _run_live(text, ws, ctx)
    if name == "coding":
        return _run_coding(text, ws, ctx, llm)
    if name == "data":
        return _run_data(text, ws, ctx)
    raise Skip(f"unknown engine {name}")


def _num(x):
    f = float(x)
    return int(f) if abs(f - round(f)) < 1e-9 else round(f, 2)


def _out_file(text, default):
    m = re.search(r"\b([\w-]+\.(?:md|txt))\b", text)
    return m.group(1) if m else default


def _write(ws, ctx, rel, body):
    p = ws / rel
    ctx.require("write", rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- research
def _research_params(text):
    m = re.search(r"\b(research|find|list|identify|investigate|compile|look up|collect|gather|name)\b"
                  r"(?:\s+(?:a\s+list\s+of|the\s+top|the|top|at\s+least|me))*\s+(\d{1,4})\s+(.*)$",
                  text, re.I | re.S)
    if not m:
        raise Skip("no count in mission")
    n = int(m.group(2))
    if not 1 <= n <= 50:
        raise Skip(f"count {n} out of range")
    rest = re.split(r"\s+and write\b|\.\s*Write\b|\s+write\s+|,\s*then\b|\.\s*Then\b|\s+into\s+|"
                    r"\s+with\s+(?:their|the|its)\b|\s+and\s+save\b|\s+\(|;|\s+in\s+[\w-]+\.(?:md|txt)\b|\.\s",
                    m.group(3), maxsplit=1, flags=re.I)[0]
    rest = re.sub(r"^(?:the|top|best|open[- ]source|of|a|an|some|leading|current)\s+", "", rest.strip(), flags=re.I)
    rest = re.sub(r"\s+(?:projects|tools|frameworks|databases|libraries|systems|engines|solutions|websites)\s*$",
                  "", rest, flags=re.I)
    topic = rest.strip(" .,-")
    return n, (topic or "open source software"), _out_file(text, "research.md")


def _gh_search(topic, n, ctx):
    q = urllib.parse.quote(topic)
    url = (f"https://api.github.com/search/repositories?q={q}"
           f"&sort=stars&order=desc&per_page={max(n * 2, n + 10)}")
    data = _json(url, ctx, limit=8_000_000, timeout=25)
    items = []
    for r in data.get("items", []):
        if r.get("fork") or r.get("archived") or not r.get("html_url"):
            continue
        items.append({"value": r.get("full_name") or r.get("name", ""),
                      "source": r["html_url"], "key": r["html_url"]})
    return items


def _ddg_candidates(topic, n, ctx):
    """Fallback/supplement: DuckDuckGo results through the audited search tool path."""
    try:
        from .tools import web_search
        res = web_search({"query": f"open source {topic}", "limit": n * 2 + 5}, ctx)
        return [{"value": (i.get("value") or "").split(" - ")[0].strip()[:60] or i["source"],
                 "source": i["source"], "key": i["source"]} for i in res["items"]]
    except Exception:
        return []


def _alive(url):
    try:
        _http(url, None, limit=4096, timeout=8)
        return True
    except urllib.error.HTTPError as e:
        return e.code not in (404, 410, 451)     # 403/429/5xx: real site, bot-blocked
    except Exception:
        return False                              # DNS/timeout to dead host


def _run_research(text, ws, ctx):
    n, topic, out = _research_params(text)
    items, note = [], ""
    try:
        items = _gh_search(topic, n, ctx)
    except Exception as e:
        note = f"gh search failed ({e}); "
    if len(items) < n:                            # supplement from web search
        for cand in _ddg_candidates(topic, n, ctx):
            if cand["source"] in {i["source"] for i in items}:
                continue
            if _alive(cand["source"]):
                items.append(cand)
            if len(items) >= n * 2 + 5:
                break
    seen_url, seen_val, uniq = set(), set(), []
    for i in items:                               # dedupe by url and by name
        k_u, k_v = i["source"].lower(), i["value"].lower()
        if k_u in seen_url or k_v in seen_val:
            continue
        seen_url.add(k_u)
        seen_val.add(k_v)
        uniq.append(i)
    items = uniq[: max(n * 2, n + 4)]             # headroom: verifier checks the file, not this list
    if not items:
        raise Skip("no candidates found")
    lines = [f"- {i['value']} - {i['source']}" for i in items]
    _write(ws, ctx, out, "\n".join(lines) + "\n")
    crit = [
        Criterion("c1", f"file {out} exists", "file_exists", {"path": out}),
        Criterion("c2", f"at least {n} entries", "min_items", {"n": n}),
        Criterion("c3", "no duplicate entries", "unique_items", {}),
        Criterion("c4", "every url resolves (no fabricated links)", "urls_ok", {"path": out}),
        Criterion("c5", "every entry cites a retrieved source", "all_sourced", {}),
    ]
    return {"criteria": crit, "items": items, "sources": [i["source"] for i in items],
            "files": [out], "output": note + f"research '{topic}': {len(items)} candidates -> {out}"}


# --------------------------------------------------------------------------- live api
def fetch_version(pkg, source):
    if source == "pypi":
        return _json(f"https://pypi.org/pypi/{pkg}/json", limit=6_000_000)["info"]["version"]
    if source == "npm":
        return _json(f"https://registry.npmjs.org/{pkg}/latest")["version"]
    if source == "crates":
        d = _json(f"https://crates.io/api/v1/crates/{pkg}")["crate"]
        return d.get("max_stable_version") or d.get("max_version")
    if source == "gh":
        try:
            return _json(f"https://api.github.com/repos/{pkg}/releases/latest")["tag_name"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            tags = _json(f"https://api.github.com/repos/{pkg}/tags")
            if not tags:
                raise
            return tags[0]["name"]
    raise ValueError(f"unknown source {source}")


def _packages(text, limit=12):
    m = re.search(r":\s*([^\n]+?)(?:\.\s|$)", text)
    seg = m.group(1) if m else text
    stop = {"the", "latest", "current", "recent", "of", "each", "these", "following", "python",
            "packages", "package", "npm", "crates", "crates.io", "github", "repositories",
            "repository", "libraries", "library", "and", "write", "save", "into", "file"}
    out = []
    for tok in re.split(r",|\band\b", seg):
        tok = re.sub(r"[^a-z0-9_@/.\-]", "", tok.strip().lower())
        if tok and len(tok) >= 2 and tok not in stop and re.match(r"^[a-z0-9]", tok):
            out.append(tok)
    return list(dict.fromkeys(out))[:limit]


def _run_live(text, ws, ctx):
    low = text.lower()
    out = _out_file(text, "versions.md")
    if "hacker news" in low:
        n_m = re.search(r"\b(\d{1,3})\b", text)
        n = int(n_m.group(1)) if n_m else 5
        n = min(max(n, 1), 10)
        ids = _json("https://hacker-news.firebaseio.com/v0/topstories.json", ctx)[: n + 3]
        items = []
        for i in ids:
            try:
                it = _json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", ctx)
            except Exception:
                continue
            if it and it.get("title"):
                url = f"https://news.ycombinator.com/item?id={i}"
                items.append({"value": it["title"], "source": url, "key": str(i)})
            if len(items) >= n:
                break
        if len(items) < n:
            raise Skip(f"only {len(items)} stories")
        _write(ws, ctx, out, "\n".join(f"- {i['value']} - {i['source']}" for i in items) + "\n")
        crit = [Criterion("c1", f"file {out} exists", "file_exists", {"path": out}),
                Criterion("c2", f"at least {n} stories", "min_items", {"n": n}),
                Criterion("c3", "story ids + titles match live HN", "versions_correct",
                          {"path": out, "source": "hn", "n": n})]
        return {"criteria": crit, "items": items, "sources": [i["source"] for i in items],
                "files": [out], "output": f"hn top {n} -> {out}"}

    source = ("npm" if "npm" in low else
              "crates" if re.search(r"crates?\.io|\bcrates\b", low) else
              "gh" if re.search(r"github|repositor", low) else "pypi")
    pkgs = _packages(text)
    if not pkgs:
        raise Skip("no packages found in mission")
    items, failures = [], []
    for pkg in pkgs:
        ver = None
        for attempt in (0, 1):
            try:
                ver = fetch_version(pkg, source)
                break
            except Exception:
                if attempt:
                    failures.append(pkg)
                time.sleep(1.0)
        if ver is None:
            continue
        api = {"pypi": f"https://pypi.org/pypi/{pkg}/json",
               "npm": f"https://registry.npmjs.org/{pkg}/latest",
               "crates": f"https://crates.io/api/v1/crates/{pkg}",
               "gh": f"https://api.github.com/repos/{pkg}/releases/latest"}[source]
        items.append({"value": f"{pkg}=={ver}", "source": api, "key": pkg})
    if not items:
        raise Skip(f"all lookups failed: {failures}")
    _write(ws, ctx, out, "\n".join(i["value"] for i in items) + "\n")
    crit = [Criterion("c1", f"file {out} exists", "file_exists", {"path": out}),
            Criterion("c2", f"{len(pkgs)} packages listed", "min_items", {"n": len(pkgs)}),
            Criterion("c3", "versions match live registry APIs", "versions_correct",
                      {"path": out, "packages": pkgs, "source": source})]
    return {"criteria": crit, "items": items, "sources": [i["source"] for i in items],
            "files": [out],
            "output": f"{source}: {len(items)}/{len(pkgs)} versions -> {out}"
                      + (f"; failed: {failures}" if failures else "")}


# --------------------------------------------------------------------------- coding
def _test_command(text, ws):
    from .contract import test_command
    cmd = test_command(text, ws)
    if cmd != "python -m pytest -q" or "`python -m pytest -q`" in text:
        return cmd
    if list(ws.glob("test*.py")) or list(ws.glob("*_test.py")):
        return "python -m pytest -q"
    raise Skip("no test command and no test files")


def _ask_patch(llm, ws, out, sources, tests):
    files = []
    for p in sources + tests:
        try:
            body = p.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            continue
        files.append(f"### {p.name}\n{body}")
    system = ('You repair code. Reply ONLY with JSON: {"file": "<name.py>", "find": "<exact existing '
              'substring occurring exactly once>", "replace": "<replacement>"}. Use find:"" to CREATE '
              'a new file (only if it does not exist). Never modify test files. No explanations, no markdown.')
    prompt = (f"Failing command output (tail):\n{out[-3500:]}\n\n" + "\n\n".join(files))
    raw = llm.complete(system, prompt, 1500)
    d = extract_json(raw)
    if not isinstance(d, dict):
        raise ValueError("patch must be a JSON object")
    missing = [k for k in ("file", "find", "replace") if not isinstance(d.get(k), str)]
    if missing:                       # a dropped arg must never be read as "replace with nothing"
        raise ValueError(f"patch missing required field(s) {missing}")
    return d["file"], d["find"], d["replace"]


def _health(out, rc):
    """Higher is better: passing tests count, collection/syntax errors are heavily penalised."""
    if rc == 0:
        return 10 ** 6
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", out))
    errors = sum(int(n) for n in re.findall(r"(\d+) errors?\b", out)) + \
        len(re.findall(r"SyntaxError|IndentationError|ImportError|ModuleNotFoundError", out))
    return passed - 1000 * errors


def _run_coding(text, ws, ctx, llm):
    if llm is None:
        raise Skip("coding engine needs an LLM for patches")
    cmd = _test_command(text, ws)
    return fix_loop(cmd, ws, ctx, llm)


def fix_loop(cmd, ws, ctx, llm, max_attempts=3):
    """Run tests -> ask for ONE exact patch -> apply via exact-match -> re-run. Test files are immutable
    (snapshotted before, restored after; tampering counts as failure). Shared by the engine and code.fix."""
    ws = Path(ws)
    if llm is None:
        raise RuntimeError("no_llm: code.fix needs a model for patches")
    cmd = cmd or "python -m pytest -q"
    tests = list(dict.fromkeys(sorted(ws.glob("test*.py")) + sorted(ws.glob("*_test.py"))
                               + sorted(ws.glob("tests/test*.py"))))
    sources = [p for p in sorted(ws.glob("*.py")) if p not in tests and p.name != "conftest.py"]
    if not sources:
        raise Skip("no source files")
    if not tests and "pytest" in cmd:
        raise Skip("pytest command but no test files")

    def run_tests():
        from .tools import run_command
        ctx.require("execute", cmd)
        r = run_command(cmd, ws, 180)
        return r.returncode, (r.stdout + r.stderr)[-4500:]

    orig = {t: t.read_bytes() for t in tests}     # snapshot BEFORE patching
    rc, out = run_tests()
    attempts, log = 0, []
    while rc != 0 and attempts < max_attempts:
        attempts += 1
        try:
            fname, find, repl = _ask_patch(llm, ws, out, sources, tests)
        except Exception as e:
            out += f"\n[patch parse failed: {e}]"
            log.append("parse-failed")
            continue
        if not fname or ".." in fname or "/" in fname or "\\" in fname:
            out += "\n[patch rejected: bad filename]"
            log.append("bad-filename")
            continue
        if fname.startswith("test_") or fname.endswith("_test.py") or fname == "conftest.py" \
                or any(fname == t.name for t in tests):
            out += "\n[patch refused: test files are immutable]"
            log.append("refused-test-edit")
            continue
        target = ws / fname
        ctx.require("write", fname)
        if find:
            if not target.exists():
                out += "\n[patch rejected: file does not exist]"
                log.append("missing-file")
                continue
            cur = target.read_text(encoding="utf-8", errors="replace")
            if cur.count(find) != 1:
                out += f"\n[patch rejected: find occurs {cur.count(find)}x, want exactly 1]"
                log.append("ambiguous-find")
                continue
            new = cur.replace(find, repl)
            target.write_text(new, encoding="utf-8")
        else:
            if target.exists():
                out += "\n[patch rejected: cannot create existing file]"
                log.append("create-existing")
                continue
            new = repl
            target.write_text(repl, encoding="utf-8")
            if hasattr(ctx.broker, "note_created"):
                ctx.broker.note_created(fname)
        drop_bytecode(target)
        if target.read_text(encoding="utf-8", errors="replace") != new:   # read-back: the edit really landed
            out += "\n[patch did not land on disk]"
            log.append("readback-mismatch")
            continue
        before_rc, before_out = rc, out
        rc, out = run_tests()
        if _health(out, rc) < _health(before_out, before_rc):      # patch made things worse: undo it
            if find:
                target.write_text(cur, encoding="utf-8")
            else:
                target.unlink()
            drop_bytecode(target)
            log.append(f"reverted {fname} (regression)")
            rc, out = before_rc, before_out + "\n[previous patch made tests worse and was reverted]"
            continue
        log.append(f"patched {fname}")

    passed = rc == 0
    for t in tests:                                # defense-in-depth: restore any touched test file
        if t.read_bytes() != orig[t]:
            t.write_bytes(orig[t])
            passed = False                        # count tampering as failure even if tests then pass
    hashes = {str(t.relative_to(ws)).replace("\\", "/"): hashlib.sha256(d.replace(b"\r\n", b"\n")).hexdigest()
              for t, d in orig.items()}
    crit = [Criterion("c1", f"`{cmd}` passes", "command_ok", {"command": cmd})]
    for i, (name, h) in enumerate(hashes.items()):
        crit.append(Criterion(f"h{i + 1}", f"test file {name} untouched", "file_hash",
                              {"path": name, "sha256": h}))
    items = [{"value": f"{cmd}: {'passed' if passed else 'still failing'} after {attempts} patch(es)",
              "source": "shell:" + cmd, "key": "tests"}]
    return {"criteria": crit, "items": items, "sources": ["shell:" + cmd], "files": [],
            "output": f"{cmd} {'PASS' if passed else 'FAIL'} (patches: {attempts}; {log}); tail: {out[-400:]}"}


# --------------------------------------------------------------------------- data
def _load_rows(files):
    rows = []
    for p in files:
        if p.suffix == ".json":
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, list):
                rows += [r for r in data if isinstance(r, dict)]
            elif isinstance(data, dict):
                rows.append({k: v for k, v in data.items()})
        else:
            with open(p, newline="", encoding="utf-8-sig", errors="replace") as fh:
                rows += [r for r in _csv.DictReader(fh) if r]
    return rows


def _isnum(v):
    try:
        float(str(v).strip().replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _data_files(ws, text):
    named = re.search(r"\b([\w-]+\.csv)\b", text, re.I)
    if named and (ws / named.group(1)).exists():
        return [ws / named.group(1)]
    files = [p for p in sorted(ws.rglob("*.csv")) if ".agent" not in p.parts and "__pycache__" not in p.parts]
    if files:
        return files
    return [p for p in sorted(ws.rglob("*.json")) if ".agent" not in p.parts and "__pycache__" not in p.parts]


def _pick_cols(rows, text):
    cols = list(rows[0].keys())
    low = text.lower()
    nums, cats = [], []
    for c in cols:
        vals = [str(r.get(c, "")).strip() for r in rows if str(r.get(c, "")).strip()]
        frac = (sum(1 for v in vals if _isnum(v)) / len(vals)) if vals else 0.0
        (nums if vals and frac >= 0.6 else cats).append(c)   # tolerant: a few corrupt cells OK
    value = None
    m = re.search(r"(?:sum|total|average|mean|add(?:ed)? up|highest|lowest) (?:of |all )?(?:the )?(\w+)", low)
    if m and m.group(1) in cols and m.group(1) in nums:
        value = m.group(1)
    if value is None and nums:
        preferred = [c for c in nums
                     if c.lower() in ("sales", "amount", "total", "value", "price", "revenue",
                                      "qty", "quantity", "count", "temperature", "units")]
        value = preferred[0] if preferred else nums[0]
    group = None
    m2 = re.search(r"(\w+) with the (?:highest|most)|one line per (\w+)|\bby (\w+)\b|per (\w+)\b", low)
    if m2:
        cand = next((g for g in m2.groups() if g), None)
        if cand in cols:
            group = cand
    if group is None and cats:
        group = cats[0]
    if value is None:
        raise Skip("no numeric column detected")
    return value, group


def _facts(rows, value_col, group_col, low):
    total, vals, skipped, groups = 0.0, [], 0, {}
    for r in rows:
        v = str(r.get(value_col, "")).strip().replace(",", "")
        if not _isnum(v):
            skipped += 1
            continue
        num = float(v)
        vals.append(num)
        total += num
        if group_col:
            key = str(r.get(group_col, "")).strip() or "(blank)"
            groups[key] = groups.get(key, 0.0) + num
    facts = {"total": _num(total), "count": len(vals), "skipped": skipped}
    if vals:
        facts["average"] = _num(total / len(vals))
        facts["min"] = _num(min(vals))
        facts["max"] = _num(max(vals))
    if groups:
        facts["top"] = max(groups, key=lambda k: groups[k])
    return facts, {k: _num(v) for k, v in groups.items()}


def recompute(ws, p):
    """Independent re-derivation used by the verifier: raw files -> facts + group sums."""
    files = [Path(ws) / rel for rel in p["files"]]
    rows = _load_rows(files)
    if not rows:
        raise ValueError("no rows in data files")
    return _facts(rows, p["value_col"], p.get("group_col"), "")


def data_expectations(text, ws):
    """Compute, in Python, every figure the mission asks for. Used by the data engine AND by the
    contract floor, so even a model-planned data mission is verified against recomputed numbers."""
    files = _data_files(ws, text)
    if not files:
        raise Skip("no csv/json data files found")
    rows = _load_rows(files)
    if not rows:
        raise Skip("data files empty")
    low = text.lower()
    value_col, group_col = _pick_cols(rows, text)
    facts, groups = _facts(rows, value_col, group_col, low)
    scalars = []
    if re.search(r"\btotal\b|\bsum\b", low):
        scalars.append("total")
    if re.search(r"\btop\b|highest combined|best\b|most \w+", low):
        scalars.append("top")
    if re.search(r"\baverage\b|\bmean\b", low):
        scalars.append("average")
    if re.search(r"\bcount\b|number of (?:rows|records|entries)|how many rows", low):
        scalars.append("count")
    if re.search(r"\bmin\b|minimum|lowest", low):
        scalars.append("min")
    if re.search(r"\bmax\b|maximum|largest", low):
        scalars.append("max")
    if re.search(r"corrupt|malformed|skip", low):
        scalars.append("skipped")
    mode2 = bool(re.search(r"one line per (\w+)", low))
    if not scalars and not mode2:
        scalars = ["total", "top"]
    expect, lines = {}, []
    for label in scalars:
        if label not in facts:
            raise Skip(f"fact {label} not derivable")
        expect[label] = facts[label]
        lines.append(f"{label}: {facts[label]}")
    if mode2:
        if not groups:
            raise Skip("no group column for one-line-per mode")
        for k in sorted(groups):
            expect[k] = groups[k]
            lines.append(f"{k}: {groups[k]}")
    rels = [str(p.relative_to(ws)).replace("\\", "/") for p in files]
    return {"path": _out_file(text, "summary.md"), "files": rels, "value_col": value_col,
            "group_col": group_col, "expect": expect, "lines": lines}


def _run_data(text, ws, ctx):
    p = data_expectations(text, ws)
    out, lines, expect, rels = p["path"], p.pop("lines"), p["expect"], p["files"]
    _write(ws, ctx, out, "\n".join(lines) + "\n")
    crit = [Criterion("c1", f"file {out} exists", "file_exists", {"path": out}),
            Criterion("c2", "every reported figure re-derived from the raw files", "data_correct", p)]
    items = [{"value": f"{k}: {v}", "source": rels[0], "key": k} for k, v in expect.items()]
    return {"criteria": crit, "items": items, "sources": rels, "files": [out],
            "output": f"computed {len(expect)} fact(s) from {rels} "
                      f"(value={p['value_col']}, group={p['group_col']}) -> {out}"}
