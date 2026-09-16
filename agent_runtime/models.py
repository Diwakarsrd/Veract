from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field, asdict

SCHEMA = 2
CHECKS = {"min_items", "unique_items", "all_sourced", "file_exists", "command_ok", "contains", "llm",
          "urls_ok", "data_correct", "versions_correct", "file_hash", "file_lines", "file_unique", "grounded"}


@dataclass
class Criterion:
    id: str
    description: str
    check: str
    params: dict = field(default_factory=dict)


@dataclass
class Mission:
    id: str
    text: str
    objective: str
    kind: str
    constraints: dict
    criteria: list
    created: float = field(default_factory=time.time)

    @staticmethod
    def new_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["criteria"] = [Criterion(**c) for c in d["criteria"]]
        return cls(**d)


@dataclass
class Step:
    id: str
    tool: str
    args: dict
    deps: list = field(default_factory=list)
    description: str = ""
    result: bool = False          # contributes items to the mission artifact
    status: str = "pending"       # pending|done|failed|blocked|superseded
    attempts: int = 0
    error: str | None = None
    error_kind: str | None = None


@dataclass
class Plan:
    steps: list
    version: int = 1

    def to_dict(self):
        return {"version": self.version, "steps": [asdict(s) for s in self.steps]}

    @classmethod
    def from_dict(cls, d):
        return cls([Step(**s) for s in d["steps"]], d.get("version", 1))

    def get(self, sid):
        return next(s for s in self.steps if s.id == sid)


@dataclass
class RunState:
    mission: Mission
    plan: Plan
    evidence: dict = field(default_factory=dict)   # step_id -> evidence dict
    replans: int = 0
    fixes: dict = field(default_factory=dict)
    status: str = "running"                        # running|passed|failed
    report: dict | None = None
    log: list = field(default_factory=list)

    def note(self, msg):
        self.log.append({"ts": time.time(), "msg": msg})

    def to_dict(self):
        return {"schema": SCHEMA, "mission": self.mission.to_dict(), "plan": self.plan.to_dict(), "evidence": self.evidence,
                "replans": self.replans, "fixes": self.fixes, "status": self.status,
                "report": self.report, "log": self.log}

    @classmethod
    def from_dict(cls, d):
        d = migrate(d)
        return cls(Mission.from_dict(d["mission"]), Plan.from_dict(d["plan"]), d["evidence"], d["replans"],
                   d["fixes"], d["status"], d["report"], d["log"])


def migrate(d):
    """Upgrade older checkpoints so `agent resume` keeps working across versions."""
    v = d.get("schema", 1)
    if v > SCHEMA:
        raise ValueError(f"checkpoint schema {v} is newer than this runtime ({SCHEMA}); upgrade agent-runtime")
    if v < 2:                                   # v1 (0.1.x): no verifiable flag, no schema field
        d = dict(d)
        d["mission"] = dict(d["mission"])
        d["mission"]["constraints"] = dict(d["mission"].get("constraints") or {})
        d["mission"]["constraints"].setdefault("verifiable", True)
        d["schema"] = 2
    return d
