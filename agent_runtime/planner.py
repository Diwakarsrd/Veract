"""Planner: Mission -> execution graph (DAG). LLM-driven when available, rule-based fallback otherwise."""
from .llm import extract_json
from .guard import sanitize_query
from .models import Plan, Step

VARIANTS = ["list", "directory", "top", "latest 2026", "examples", "database"]


EDIT_TOOLS = {"code.fix", "fs.patch", "fs.write"}


def _fit_for(mission, steps):
    """Reject plans that structurally cannot satisfy the mission (v0.1 t4: a coding plan that only ran pytest)."""
    if mission.kind == "coding" and not any(s.tool in EDIT_TOOLS for s in steps):
        raise ValueError("coding plan has no step that can change code")
    if not steps:
        raise ValueError("empty plan")


def _validate(steps, tools):
    ids = {s.id for s in steps}
    if len(ids) != len(steps):
        raise ValueError("duplicate step ids")
    for s in steps:
        if s.tool not in tools:
            raise ValueError(f"unknown tool {s.tool}")
        if any(d not in ids for d in s.deps):
            raise ValueError("unknown dependency")
    seen, left = set(), list(steps)
    while left:                                   # cycle check
        ready = [s for s in left if all(d in seen for d in s.deps)]
        if not ready:
            raise ValueError("cyclic plan")
        seen |= {s.id for s in ready}
        left = [s for s in left if s.id not in seen]


class Planner:
    def __init__(self, tools, llm=None):
        self.tools, self.llm = tools, llm

    def _tool_doc(self):
        # compact on purpose: a 3B model with a 16k window must have room left for the task itself
        return "\n".join(f"- {t.name} {t.args_hint}" for t in self.tools.values())

    def _llm_steps(self, prompt, prefix=""):
        sys = ("Plan as a DAG. Reply ONLY with JSON list of {\"id\",\"tool\",\"args\",\"deps\",\"description\",\"result\"}. "
               "Use '$<step_id>.items' or '$<step_id>.output' to pass data between steps; deps must list referenced steps. "
               "Mark steps whose items form the final deliverable with result=true. Tools:\n" + self._tool_doc())
        steps = [Step(prefix + s["id"], s["tool"], s.get("args", {}), [prefix + d for d in s.get("deps", [])],
                      s.get("description", ""), bool(s.get("result")))
                 for s in extract_json(self.llm.complete(sys, prompt, 1200))]
        for s in steps:  # keep $refs consistent with prefix
            s.args = {k: (v.replace("$", "$" + prefix, 1) if isinstance(v, str) and v.startswith("$") and prefix else v)
                      for k, v in s.args.items()}
        return steps

    def plan(self, mission, memory_hints=""):
        if self.llm:
            try:
                steps = self._llm_steps(f"Mission: {mission.objective}\nKind: {mission.kind}\nCriteria: "
                                        f"{[c.description for c in mission.criteria]}\n{memory_hints}")
                _validate(steps, self.tools)
                _fit_for(mission, steps)
                return Plan(steps)
            except Exception:
                pass
        return Plan(self._fallback(mission))

    def _fallback(self, mission, prefix="", variant=""):
        has = lambda t: t in self.tools
        if mission.kind == "coding":
            cmd = next((c.params["command"] for c in mission.criteria if c.check == "command_ok"), "python -m pytest -q")
            if self.llm and has("code.fix"):          # v0.1 only re-ran the tests: it could never fix anything
                return [Step(prefix + "s1", "code.fix", {"command": cmd}, [], "test-driven repair loop", True)]
            if has("shell.run"):
                return [Step(prefix + "s1", "shell.run", {"command": cmd}, [], "run test suite", True)]
        if has("web.search") and mission.kind != "data":
            n = next((c.params["n"] for c in mission.criteria if c.check == "min_items"), 10)
            steps = [Step(prefix + "s1", "web.search", {"query": sanitize_query(f"{mission.objective} {variant}"), "limit": 10}, [], "search"),
                     Step(prefix + "s2", "web.fetch_many", {"items": f"${prefix}s1.items", "limit": 8}, [prefix + "s1"], "fetch pages")]
            if self.llm and has("llm.extract_items"):
                steps.append(Step(prefix + "s3", "llm.extract_items", {"docs": f"${prefix}s2.items", "instruction": mission.objective,
                                                                       "limit": min(max(n, 12), 15)}, [prefix + "s2"], "extract items", True))
            else:
                steps[1].result = True      # no model: deliverable is the set of sourced documents
            return steps
        return [Step(prefix + "s1", "fs.list", {"path": "."}, [], "inspect workspace", True)]

    def replan(self, mission, plan, failures, attempt):
        prefix = f"r{attempt}_"
        if self.llm:
            try:
                steps = self._llm_steps(f"Mission: {mission.objective}\nPrevious attempt failed: {failures}\n"
                                        f"Propose NEW steps (no repeats of failed approach).", prefix)
                _validate(steps, self.tools)
                _fit_for(mission, steps)
                plan.steps += steps
                plan.version += 1
                return True
            except Exception:
                pass
        steps = self._fallback(mission, prefix, VARIANTS[(attempt - 1) % len(VARIANTS)])
        if steps[0].tool == "code.fix":
            if sum(1 for s in plan.steps if s.tool == "code.fix") >= 2:
                return False                      # two full repair loops already ran
        elif steps[0].tool in ("fs.list", "shell.run") and mission.kind != "research":
            return False                          # nothing new to try
        plan.steps += steps
        plan.version += 1
        return True
