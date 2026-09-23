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
