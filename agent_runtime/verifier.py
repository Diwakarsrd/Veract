"""Independent verifier. It never trusts executor claims: it re-derives facts from evidence, disk and re-execution."""
import re
from pathlib import Path


def norm(s):
    return re.sub(r"\W+", " ", str(s).lower()).strip()


def assemble_items(state):
    items = []
    for s in state.plan.steps:
        if s.result and s.status == "done":
            items += state.evidence.get(s.id, {}).get("items", [])
    if state.fixes.get("drop_unsourced"):
        items = [i for i in items if i.get("source")]
    if state.fixes.get("dedupe"):
        seen, out = set(), []
        for i in items:
            k = norm(i.get("key") or i.get("value"))
            if k not in seen:
                seen.add(k)
                out.append(i)
        items = out
    return items


class Verifier:
    """Re-derives every claim from disk, re-execution and live APIs. File checks never leave the workspace,
    and an `llm` criterion is answered by a separate judge model when one is configured (AGENT_JUDGE_*)."""

    def __init__(self, ctx_factory, llm=None, judge=None):
        self.ctx_factory, self.llm, self.judge = ctx_factory, llm, judge

    def verify(self, state):
        items = assemble_items(state)
        retrieved = {src for e in state.evidence.values() for src in e.get("sources", [])}
        results = []
        for c in state.mission.criteria:
            try:
                ok, detail = self._check(c, items, retrieved, state)
            except Exception as e:
                ok, detail = False, f"verifier error: {type(e).__name__}: {e}"
            results.append({"id": c.id, "check": c.check, "description": c.description, "passed": bool(ok),
                            "detail": str(detail)[:300]})
        return {"passed": bool(results) and all(r["passed"] for r in results), "results": results,
                "n_items": len(items)}

    def _file(self, rel):
        """Resolve a criterion path strictly inside the workspace (never follows absolute/outside paths)."""
        ws = Path(state_ws(self)).resolve()
        f = (ws / str(rel)).resolve()
        if Path(str(rel)).is_absolute() or not f.is_relative_to(ws):
            raise PermissionError(f"criterion path {rel!r} is outside the workspace")
        return f

    def _check(self, c, items, retrieved, state):
        p = c.params or {}
        if c.check == "min_items":
            return len(items) >= int(p["n"]), f"{len(items)}/{p['n']} items"
        if c.check == "unique_items":
            keys = [norm(i.get("key") or i.get("value")) for i in items]
            d = len(keys) - len(set(keys))
            return d == 0 and bool(items), f"{d} duplicate(s)" if items else "no items"
        if c.check == "all_sourced":
            bad = [i for i in items if not i.get("source") or (i["source"] not in retrieved)]
            return not bad and bool(items), f"{len(bad)} item(s) with missing/unretrieved source"
        if c.check == "grounded":
            bad = [i.get("value") for i in items if i.get("grounded") is False]
            return not bad and bool(items), (f"{len(bad)} item(s) not found in their cited page: {bad[:3]}"
                                             if bad else f"{len(items)} item(s) grounded or engine-derived")
        if c.check == "file_exists":
            f = self._file(p["path"])
            return f.is_file() and f.stat().st_size > 0, f"{p['path']} {'ok' if f.is_file() else 'missing'}"
        if c.check == "file_lines":
            f = self._file(p["path"])
            if not f.is_file():
                return False, "missing file"
            rx = re.compile(p.get("pattern") or r"\S")
            n = sum(1 for ln in f.read_text(errors="replace").splitlines() if ln.strip() and rx.search(ln))
            return n >= int(p["n"]), f"{n}/{p['n']} matching lines"
        if c.check == "file_unique":
            f = self._file(p["path"])
            if not f.is_file():
                return False, "missing file"
            lines = [ln.strip() for ln in f.read_text(errors="replace").splitlines() if ln.strip()]
            urls = [u for ln in lines for u in {x.rstrip("/").lower() for x in re.findall(r"https?://[^\s)\]>]+", ln)}]
            names = []
            for ln in lines:                       # "- Name - https://url": the name is everything before the last " - url"
                parts = re.split(r"\s+-\s+(?=https?://)", ln.lstrip("-*0123456789. "))
                n = norm(" - ".join(parts[:-1])) if len(parts) > 1 else ""
                if n:
                    names.append(n)
            dup = (len(urls) - len(set(urls))) + (len(names) - len(set(names)))
            return dup == 0 and bool(lines), f"{dup} duplicate(s) across {len(lines)} lines"
        if c.check == "command_ok":
            ctx = self.ctx_factory("verifier", c.id)
            from .tools import run_command
            ctx.require("execute", p["command"])
            r = run_command(p["command"], ctx.workspace, 300)
            return r.returncode == 0, f"exit {r.returncode}: {(r.stdout + r.stderr)[-200:]}"
        if c.check == "contains":
            t = self._file(p["path"]).read_text(errors="replace")
            return p["text"].lower() in t.lower(), f"looking for {p['text']!r}"
        if c.check == "llm":
            judge = self.judge or self.llm
            if not judge:
                return False, "unverifiable: no LLM judge configured (never auto-passed)"
            ans = judge.complete("You are a strict verifier. Answer strictly YES or NO.",
                                 f"{p.get('question', c.description)}\nItems: {items[:30]}")
            return ans.strip().upper().startswith("YES"), ("judge: " if self.judge else "self-judge: ") + ans[:100]
        if c.check == "urls_ok":
            f = self._file(p["path"])
            if not f.is_file():
                return False, "missing file"
            return URL_CHECKER(f.read_text(errors="replace"))
        if c.check == "versions_correct":
            f = self._file(p["path"])
            if not f.is_file():
                return False, "missing file"
            return _check_versions(f.read_text(errors="replace"), p)
        if c.check == "data_correct":
            f = self._file(p["path"])
            if not f.is_file():
                return False, "missing file"
            return _check_data(state_ws(self), f.read_text(errors="replace"), p)
        if c.check == "file_hash":
            import hashlib
            f = self._file(p["path"])
            if not f.is_file():
                return False, "missing file"
            h = hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            return h == p.get("sha256"), f"{h[:12]} vs expected {str(p.get('sha256'))[:12]}"
        return False, "unknown check"


def _check_urls(text, limit=12):
    """Every URL in the deliverable must actually resolve (404/DNS = fabricated)."""
    import socket
    import urllib.error
    import urllib.request
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s)\]>'\"`]+", text)))[:limit]
    if not urls:
        return False, "no urls in deliverable"
    dead, unknown = [], []
    for u in urls:
        if _private(u):                             # never probe internal hosts from a deliverable (SSRF)
            dead.append(u)
            continue
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 agent-runtime/0.1"})
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.status in (404, 410, 451):
                    dead.append(u)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 451):
                dead.append(u)
        except urllib.error.URLError as e:
            (dead if isinstance(e.reason, socket.gaierror) else unknown).append(u)
        except Exception:
            unknown.append(u)                       # timeout: could be real but bot-blocked, could be dead
    ok = not dead and len(unknown) <= max(1, len(urls) // 4)
    return ok, (f"{len(urls) - len(dead) - len(unknown)}/{len(urls)} resolve"
                + (f"; dead: {dead[:3]}" if dead else "") + (f"; unreachable: {len(unknown)}" if unknown else ""))


def _check_versions(text, p):
    """Re-query the live API for every package and compare against the file."""
    from .engines import fetch_version
    found = {}
    for line in text.splitlines():
        m = re.match(r"\s*([\w.@/-]+)\s*==\s*(\S+)", line.strip())
        if m:
            found[m.group(1)] = m.group(2)
    src = p.get("source", "pypi")
    if src == "hn":
        ids = re.findall(r"id=(\d+)", text)
        n = int(p.get("n", 5))
        bad = []
        for i in ids[:n]:
            try:
                from .engines import _get_json
                it = _get_json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
                if not it or it.get("title", "") not in text:
                    bad.append(f"{i}: title mismatch")
            except Exception as e:
                bad.append(f"{i}: {e}")
        ok = len(ids) >= n and not bad
        return ok, f"{len(ids)} ids, bad={bad[:3]}"
    bad, missing = [], []
    for pkg in p["packages"]:
        try:
            want = fetch_version(pkg, src)
        except Exception as e:
            missing.append(f"{pkg}: {e}")
            continue
        got = found.get(pkg)
        if got != want:
            bad.append(f"{pkg}: got {got!r} want {want!r}")
    total = len(p["packages"])
    ok = not bad and not missing
    return ok, f"{total - len(bad) - len(missing)}/{total} live-verified; bad={bad[:3]} missing={missing[:2]}"


def _check_data(ws, text, p):
    """Recompute every fact from the raw data files, then check engine claim AND file content."""
    from .engines import recompute
    low = text.lower()
    try:
        facts, groups = recompute(Path(ws), p)
    except Exception as e:
        return False, f"recompute error: {e}"
    problems = []
    for label, want in (p.get("expect") or {}).items():
        have = facts.get(label, groups.get(label))
        if have is None:
            problems.append(f"{label}: not derivable from data")
            continue
        if isinstance(want, (int, float)) and isinstance(have, (int, float)):
            if abs(float(want) - float(have)) > 0.01:
                problems.append(f"{label}: claim {want} vs data {have}")
        elif str(want) != str(have):
            problems.append(f"{label}: claim {want!r} vs data {have!r}")
        if label.lower() not in low or str(want).lower() not in low:
            problems.append(f"{label}: '{label}: {want}' not found in deliverable")
    return not problems, (f"{len(p.get('expect') or {})} fact(s) re-derived" if not problems else "; ".join(problems[:4]))


def _private(url):
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        for info in socket.getaddrinfo(urlparse(url).hostname or "", None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except (socket.gaierror, ValueError, UnicodeError):
        return False
    return False


URL_CHECKER = _check_urls            # swappable for offline tests


def state_ws(v):
    return v.ctx_factory("verifier", "").workspace
