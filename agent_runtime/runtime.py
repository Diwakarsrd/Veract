"""Mission loop: scope-check -> compile -> plan -> execute -> verify -> (fix | replan) -> checkpoint.

Final statuses are honest by construction:
  passed      every criterion re-derived by the verifier, and at least one is deterministic
  failed      verification failed and the replan budget is spent (artifact says exactly why)
  refused     the mission asks for something the policy forbids; nothing was planned or fetched
  partial     the wall-clock budget ran out; whatever was verified so far is written, clearly marked
  unverified  only an LLM opinion could judge it; never reported as passed
"""
import json
import time
from pathlib import Path
from .capabilities import CapabilityBroker
from .checkpoint import CheckpointStore
from .compiler import compile_mission
from . import contract, engines
from .executor import Executor
from .guard import scope_check, redact
from .memory import Memory
from .models import Mission, Plan, RunState, Step, Criterion
from .planner import Planner
from .recovery import decide, ABORT
from .tools import ToolContext, default_registry
from .verifier import Verifier, assemble_items
from . import llm as llm_mod
from . import config as config_mod, tools as tools_mod


class Runtime:
    def __init__(self, workspace=".", llm="env", tools=None, policy=None, approver=None,
                 max_replans=2, base_delay=0.5, plugin_dirs=None, on_event=None, use_engines=True,
                 deadline=None, judge="env", config=None):
        self.workspace = Path(workspace).resolve()
        self.home = self.workspace / ".agent"
        self.on_event = on_event or (lambda m: None)
        file_policy, self.config = config_mod.load(self.workspace) if config is None else ({}, config)
        self.config = {**config_mod.DEFAULTS, **(self.config or {})}
        policy = {**file_policy, **(policy or {})}
        tools_mod.SANDBOX = dict(config_mod.DEFAULTS["sandbox"], **self.config.get("sandbox", {}))
        tools_mod.SEARCH = dict(self.config.get("search") or {"provider": "duckduckgo"})
        if tools_mod.SEARCH.get("provider") == "searxng":
            from urllib.parse import urlparse
            host = urlparse(str(tools_mod.SEARCH.get("url", "http://127.0.0.1:8888"))).hostname
            policy["private_allow"] = list(policy.get("private_allow", [])) + [host]
        self.raw_llm = llm_mod.from_env() if llm == "env" else llm
        self.judge = llm_mod.from_env("AGENT_JUDGE") if judge == "env" else judge
        self.tools = tools if tools is not None else default_registry(
            plugin_dirs or [self.workspace / "tools"], self.workspace, self.on_event)
        if self.config.get("mcp") and tools is None:
            from .mcp import load_servers
            self.mcp_clients = load_servers(self.config["mcp"], self.tools, self.workspace, self.on_event)
        self.store = CheckpointStore(self.home / "missions")
        self.memory = Memory(self.home / "memory.db")
        self.policy, self.approver, self.max_replans = policy, approver, max_replans
        self.base_delay, self.use_engines, self.deadline = base_delay, use_engines, deadline
        self._brokers = {}
        self._new_budget()

    def close(self):
        if hasattr(self, "memory") and self.memory:
            try:
                self.memory.close()
            except Exception:
                pass
        for client in getattr(self, "mcp_clients", []):
            try:
                client.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ plumbing
    def _new_budget(self):
        self.budget = llm_mod.Budget(self.deadline)
        self.llm = llm_mod.Metered(self.raw_llm, self.budget) if self.raw_llm else None

    def broker(self, mid):
        if mid not in self._brokers:
            self._brokers[mid] = CapabilityBroker(self.workspace, self.home / "missions" / mid / "audit.jsonl",
                                                  self.policy, self.approver)
        return self._brokers[mid]

    def _ctx_factory(self, mid, protected=()):
        def f(who, step):
            return ToolContext(self.workspace, self.llm, self.broker(mid), mid, step, who=who,
                               protected=tuple(protected), budget=self.budget)
        return f

    @staticmethod
    def _protected(mission):
        return [c.params["path"] for c in mission.criteria if c.check == "file_hash"]

    def _hints(self, text):
        mem = self.memory.recall(text, limit=5)
        return ("Relevant memory: " + json.dumps([m["content"] for m in mem])) if mem else ""

    # ------------------------------------------------------------------ entry points
    def start(self, text):
        self._new_budget()
        mid = Mission.new_id()
        denied = scope_check(text, self.broker(mid))
        if denied:
            return self._refuse(mid, text, denied)
        eng = None
        try:
            eng = engines.detect(text) or self._engine_for(contract.classify(text, self.workspace), text)
        except Exception:
            eng = None
        if eng and self.use_engines:
            try:
                return self._run_engine(mid, text, eng)
            except engines.Skip as e:
                self.on_event(f"engine {eng} declined ({e}); using planner")
            except llm_mod.BudgetExceeded:
                pass
            except Exception as e:
                self.on_event(f"engine {eng} failed ({type(e).__name__}: {e}); falling back to plan loop")
        mission = compile_mission(text, self.llm, self._hints(text), self.workspace)
        mission.id = mid
        plan = Planner(self.tools, self.llm).plan(mission, self._hints(text))
        state = RunState(mission, plan)
        state.note(f"compiled mission {mission.id} kind={mission.kind} criteria={[c.id for c in mission.criteria]}")
        self.store.save(state)
        return self.drive(state)

    @staticmethod
    def _engine_for(kind, text):
        if kind == "coding":
            return "coding"
        if kind == "data":
            return "data"
        if kind == "research" and len(contract.deliverables(text)) <= 1 and contract.count_requested(text):
            return "research"
        return None

    def _refuse(self, mid, text, denied):
        br = self.broker(mid)
        for path, action, reason in denied:
            br.audit("scope-guard", mid, "-", "scope", action, path, "mission references this path", False, reason)
        why = "; ".join(f"{a} {p!r}: {r}" for p, a, r in denied)
        crit = [Criterion("S1", "mission stays inside the workspace policy", "scope", {"denied": why})]
        mission = Mission(mid, text, text.strip(), "refused", {"verifiable": True}, crit)
        state = RunState(mission, Plan([]))
        state.report = {"passed": False, "n_items": 0,
                        "results": [{"id": "S1", "check": "scope", "description": crit[0].description,
                                     "passed": False, "detail": "refused before planning: " + why}]}
        self.on_event("refused: " + why)
        return self._finish(state, "refused", "refused before planning (no model call, no network): " + why)

    def _run_engine(self, mid, text, eng):
        """Engine-first path: deterministic plan + evidence, then the SAME verifier gate + contract floor."""
        res = engines.run(eng, text, self.workspace, self._ctx_factory(mid), self.llm)
        _, fl = contract.floor(text, self.workspace, eng if eng != "live_api" else None)
        crit = contract.merge(res["criteria"], fl)
        mission = Mission(mid, text, text.strip().rstrip("."), eng, {"verifiable": contract.verifiable(crit)}, crit)
        step = Step("e1", "engine", {"engine": eng}, [], f"deterministic engine: {eng}", result=True)
        step.status = "done"
        state = RunState(mission, Plan([step]))
        state.evidence["e1"] = {"step_id": "e1", "tool": "engine", "ts": time.time(),
                                "output": res["output"], "items": res["items"],
                                "sources": res["sources"], "files": res["files"]}
        state.note(f"engine '{eng}': {res['output']}")
        self.on_event(f"engine '{eng}': {res['output']}")
        self.store.save(state)
        return self.drive(state)

    def resume(self, mission_id):
        self._new_budget()
        state = self.store.load(mission_id)
        if state.status in ("passed", "refused"):
            return state
        state.status = "running"
        for s in state.plan.steps:          # steps interrupted mid-flight are retried
            if s.status in ("pending", "running"):
                s.status = "pending"
        state.note("resumed from checkpoint")
        return self.drive(state)

    # ------------------------------------------------------------------ loop
    def drive(self, state):
        mid = state.mission.id
        ctxf = self._ctx_factory(mid, self._protected(state.mission))
        ex = Executor(self.tools, ctxf, self.store, base_delay=self.base_delay, budget=self.budget)
        ver = Verifier(ctxf, self.llm, self.judge)
        planner = Planner(self.tools, self.llm)
        while True:
            ex.run(state)
            if self.budget.expired():
                return self._out_of_time(state, ver)
            failed = [s for s in state.plan.steps if s.status in ("failed", "blocked")]
            if failed:
                root = [s for s in failed if s.status == "failed"]
                kinds = {s.error_kind for s in root}
                self.on_event(f"failed steps: {[(s.id, s.error_kind) for s in root]}")
                if "budget" in kinds:
                    return self._out_of_time(state, ver)
                if kinds & ABORT:
                    state.report = ver.verify(state)
                    return self._finish(state, "failed", "needs human: " + "; ".join(f"{s.id}: {s.error}" for s in root))
                if state.replans >= self.max_replans:
                    state.report = ver.verify(state)
                    return self._finish(state, "failed", "replan budget exhausted after step failures")
                state.replans += 1
                info = [(s.id, s.tool, s.error_kind, (s.error or "")[:300]) for s in root]
                for s in failed:
                    s.status = "superseded"
                if not self._replan(planner, state, info):
                    state.report = ver.verify(state)
                    return self._finish(state, "failed", "no alternative plan available")
                state.note(f"replanned (step failures): {info}")
                continue
            while True:                       # verify, apply deterministic fixes, re-verify
                state.report = ver.verify(state)
                self.on_event(f"verification: {'PASS' if state.report['passed'] else 'FAIL'} "
                              f"{[r['id'] for r in state.report['results'] if not r['passed']]}")
                self.store.save(state)
                if state.report["passed"]:
                    return self._finish(state, "passed")
                wrote = self._write_missing(state)     # deliverable exists in evidence, not yet on disk
                if wrote:
                    state.fixes.setdefault("written", []).extend(wrote)
                    state.note("recovery: wrote promised file(s) from verified evidence: " + ", ".join(wrote))
                    self.on_event("recovery: wrote " + ", ".join(wrote))
                    continue
                d = decide(state.report, state.fixes)
                state.fixes.update(d["fixes"])
                state.note("recovery: " + "; ".join(d["why"]))
                if not d["fixes"]:
                    break
                self._rewrite_deliverables(state)      # fixes (dedupe/drop) must reach the file too
            if self.budget.expired():
                return self._out_of_time(state, ver)
            if state.replans >= self.max_replans:
                return self._finish(state, "failed", "verification failed; replan budget exhausted")
            state.replans += 1
            failures = [(r["id"], r["check"], r["detail"][:200]) for r in state.report["results"] if not r["passed"]]
            if not self._replan(planner, state, failures):
                return self._finish(state, "failed", "no alternative plan available")
            state.note(f"replanned (verification): {failures}")

    def _replan(self, planner, state, info):
        try:
            return planner.replan(state.mission, state.plan, info, state.replans)
        except llm_mod.BudgetExceeded:
            return False

    def _out_of_time(self, state, ver):
        """Deadline: never time out with nothing. Write what evidence supports, verify it, report honestly."""
        try:
            self._write_missing(state)
        except Exception:
            pass
        try:
            state.report = ver.verify(state)
        except Exception:
            pass
        if state.report and state.report.get("passed"):
            return self._finish(state, "passed")
        done = sum(1 for r in (state.report or {}).get("results", []) if r["passed"])
        total = len((state.report or {}).get("results", []))
        return self._finish(state, "partial", f"time budget exhausted: {done}/{total} criteria verified; "
                                              "deliverable (if any) contains only verified evidence")

    # ------------------------------------------------------------------ deliverables
    def _deliverable_lines(self, items):
        out = []
        for i in items:
            name = " ".join(str(i.get("title") or i.get("value") or "").split())[:120]
            if not name:
                continue
            src = str(i.get("source", ""))
            out.append(f"- {name} - {src}" if src.startswith("http") else f"- {name}")
        return out

    def _write_missing(self, state):
        """Deterministic recovery: a mission promising a file gets it written from assembled evidence."""
        items = assemble_items(state)
        if not items or not state.report:
            return []
        crit = {c.id: c for c in state.mission.criteria}
        already = set(state.fixes.get("written", []))
        promised = set(contract.deliverables(state.mission.text))   # only files the USER asked for, never model-invented paths
        wrote = []
        for r in state.report["results"]:
            if r["passed"] or r["check"] != "file_exists":
                continue
            rel = str(crit[r["id"]].params.get("path", ""))
            if not rel or "://" in rel or rel in already or rel not in promised or not rel.endswith((".md", ".txt")):
                continue
            ok, _ = self.broker(state.mission.id).check("write", rel)
            if not ok:
                continue
            f = self.workspace / rel
            try:
                if f.exists() and f.stat().st_size:
                    continue
                lines = self._deliverable_lines(items)
                if not lines:
                    continue
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(redact("\n".join(lines) + "\n"), encoding="utf-8")
                wrote.append(rel)
            except OSError:
                continue
        return wrote

    def _rewrite_deliverables(self, state):
        """Files this runtime wrote from evidence are regenerated after dedupe/drop fixes."""
        items = assemble_items(state)
        for rel in state.fixes.get("written", []):
            f = self.workspace / rel
            try:
                f.write_text(redact("\n".join(self._deliverable_lines(items)) + "\n"), encoding="utf-8")
            except OSError:
                pass

    def _finish(self, state, status, reason=""):
        if status == "passed" and not state.mission.constraints.get("verifiable", True):
            status, reason = "unverified", "only an LLM opinion supports this result; not reported as passed"
        state.status = status
        if reason:
            state.note(reason)
        m = state.mission
        d = self.store.dir(m.id)
        stats = {"llm_calls": getattr(self.llm, "calls", 0),
                 "max_prompt_tokens": getattr(self.llm, "max_prompt_tokens", 0),
                 "replans": state.replans}
        art = {"schema": config_mod.SCHEMA, "mission": m.text, "status": status, "reason": reason, "items": assemble_items(state),
               "report": state.report, "stats": stats}
        (d / "artifact.json").write_text(redact(json.dumps(art, indent=1, default=str)), encoding="utf-8")
        self.memory.remember("event", f"mission '{m.text[:80]}' {status}" + (f": {reason[:160]}" if reason else ""),
                             confidence=0.9, provenance=m.id, status="verified" if status == "passed" else "unverified")
        self.store.save(state)
        return state
