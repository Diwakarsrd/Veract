"""Executes the plan DAG in parallel waves, checkpointing after every wave."""
import dataclasses
import time
from concurrent.futures import ThreadPoolExecutor
from .recovery import classify, RETRYABLE


def resolve(v, evidence):
    if isinstance(v, str) and v.startswith("$"):
        sid, *path = v[1:].split(".")
        if sid not in evidence:
            raise ValueError(f"unresolved reference {v}")
        cur = evidence[sid]
        for p in path:
            cur = cur[int(p)] if isinstance(cur, list) else cur[p]
        return cur
    if isinstance(v, dict):
        return {k: resolve(x, evidence) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, evidence) for x in v]
    return v


def _clip(x, n=20000):
    return x[:n] if isinstance(x, str) else x


class Executor:
    def __init__(self, tools, ctx_factory, store, workers=4, retries=2, base_delay=0.5, budget=None):
        self.tools, self.ctx_factory, self.store = tools, ctx_factory, store
        self.workers, self.retries, self.base_delay, self.budget = workers, retries, base_delay, budget

    def _run_step(self, step, state):
        tool = self.tools.get(step.tool)
        if tool is None:
            step.status, step.error, step.error_kind = "failed", f"unknown tool {step.tool}", "bad_input"
            return
        ctx = dataclasses.replace(self.ctx_factory("agent", step.id), tool=step.tool, why=step.description)
        for attempt in range(self.retries + 1):
            step.attempts += 1
            try:
                args = resolve(step.args, state.evidence)
                r = tool.call(args, ctx) if hasattr(tool, "call") else tool.fn(args, ctx)
                if not isinstance(r, dict) or "output" not in r:
                    raise TypeError(f"{step.tool} returned {type(r).__name__}, expected a result dict")
                state.evidence[step.id] = {"step_id": step.id, "tool": step.tool, "ts": time.time(),
                                           "output": _clip(r["output"]), "items": r.get("items") or [],
                                           "sources": r.get("sources") or [], "files": r.get("files") or []}
                step.status, step.error, step.error_kind = "done", None, None
                return
            except Exception as e:
                kind = classify(e)
                step.error, step.error_kind = f"{type(e).__name__}: {e}", kind
                if kind in RETRYABLE and attempt < self.retries and not (self.budget and self.budget.expired()):
                    time.sleep(self.base_delay * 2 ** attempt)
                    continue
                step.status = "failed"
                return

    def run(self, state, max_waves=None):
        waves = 0
        while max_waves is None or waves < max_waves:
            pending = [s for s in state.plan.steps if s.status == "pending"]
            if not pending:
                break
            if self.budget and self.budget.expired():
                break                     # leave pending: the runtime reports a partial result
            ready = [s for s in pending if all(state.plan.get(d).status == "done" for d in s.deps)]
            if not ready:
                for s in pending:
                    s.status, s.error_kind = "blocked", "blocked"
                break
            with ThreadPoolExecutor(self.workers) as pool:
                list(pool.map(lambda s: self._run_step(s, state), ready))
            waves += 1
            self.store.save(state)       # checkpoint: a crash after this point loses nothing
        self.store.save(state)
