"""Typed, provenance-tracked memory with expiry and contradiction detection (SQLite, fully local)."""
import atexit
import sqlite3
import time
import uuid
from pathlib import Path

KINDS = {"fact", "event", "task", "decision", "procedure", "preference", "evidence"}

_OPEN: list = []            # every live handle, closed at exit so temp workspaces can be removed (Windows)


def _close_all():
    while _OPEN:
        _db, _p = _OPEN.pop()
        try:
            _db.close()
        except Exception:
            pass


atexit.register(_close_all)


class Memory:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        _OPEN.append((self.db, self.path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, kind TEXT, key TEXT, content TEXT,
            confidence REAL, provenance TEXT, ts REAL, expires REAL, status TEXT)""")
        self.db.commit()

    def close(self):
        db, self.db = getattr(self, "db", None), None
        if db is None:
            return
        try:
            _OPEN.remove((db, getattr(self, "path", "")))
        except ValueError:
            pass
        db.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def remember(self, kind, content, key=None, confidence=0.5, provenance="", ttl=None, status="unverified"):
        assert kind in KINDS, f"kind must be one of {sorted(KINDS)}"
        now, mid = time.time(), uuid.uuid4().hex[:10]
        if key:  # same key + different content => both become contested
            rows = self.db.execute("SELECT id FROM memories WHERE key=? AND content!=? AND status!='expired'", (key, content)).fetchall()
            if rows:
                status = "contested"
                self.db.executemany("UPDATE memories SET status='contested' WHERE id=?", rows)
        self.db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)",
                        (mid, kind, key, content, confidence, provenance, now, now + ttl if ttl else None, status))
        self.db.commit()
        return mid

    def recall(self, query="", kind=None, limit=10):
        now = time.time()
        words = [w for w in query.lower().split() if len(w) > 2]
        rows = self.db.execute("SELECT id,kind,key,content,confidence,provenance,ts,expires,status FROM memories "
                               "WHERE (expires IS NULL OR expires>?) ORDER BY ts DESC", (now,)).fetchall()
        out = []
        for r in rows:
            if kind and r[1] != kind:
                continue
            score = sum(w in (r[3] + " " + (r[2] or "")).lower() for w in words) if words else 1
            if score:
                out.append((score * r[4], r))
        out.sort(key=lambda x: -x[0])
        keys = ["id", "kind", "key", "content", "confidence", "provenance", "ts", "expires", "status"]
        return [dict(zip(keys, r, strict=True)) for _, r in out[:limit]]
