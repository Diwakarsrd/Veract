import argparse
import json
import subprocess
import sys
from pathlib import Path
from .runtime import Runtime
from .scaffold import create_tool, publish_tool
from .tools import trust_plugin
from . import config as cfg


def _rt(a, interactive=False):
    _, rt = cfg.load(a.workspace)
    yes = getattr(a, "yes", False)
    if yes:
        print("  ! --yes: every action the policy denies will be approved automatically", file=sys.stderr)
    appr = cfg.make_approver(rt["approve"], interactive=interactive and sys.stdin.isatty(), yes=yes)
    return Runtime(a.workspace, approver=appr, max_replans=getattr(a, "max_replans", 2),
                   deadline=getattr(a, "deadline", None), on_event=lambda m: print("  ·", m))


EXIT = {"passed": 0, "failed": 1, "partial": 1, "unverified": 1, "refused": 3}   # scriptable in CI


def _show_mission(state, store):
    m = state.mission
    print(f"mission {m.id}  [{state.status}]  kind={m.kind}  replans={state.replans}")
    print(f"  text: {m.text}")
    print("  contract:")
    res = {r["id"]: r for r in (state.report or {}).get("results", [])}
    for c in m.criteria:
        r = res.get(c.id)
        mark = "·" if r is None else ("✓" if r["passed"] else "✗")
        print(f"    [{mark}] {c.id:6} {c.check:16} {c.description}" + (f"  — {r['detail'][:90]}" if r else ""))
    print("  plan:")
    for s in state.plan.steps:
        ev = state.evidence.get(s.id, {})
        print(f"    {s.id:10} {s.tool:18} {s.status:10} items={len(ev.get('items', []))}"
              + (f"  error: {s.error[:80]}" if s.error else ""))
    print("  log:")
    for e in state.log[-12:]:
        print(f"    - {e['msg'][:140]}")
    art = store.dir(m.id) / "artifact.json"
    if art.exists():
        import json as _j
        st = _j.loads(art.read_text(encoding="utf-8")).get("stats", {})
        print(f"  stats: {st}")


def _show(state):
    print(f"\nmission {state.mission.id}: {state.status.upper()}  (replans: {state.replans})")
    for r in (state.report or {}).get("results", []):
        print(f"  [{'✓' if r['passed'] else '✗'}] {r['description']} — {r['detail']}")
    if state.status != "passed" and state.log:
        print("  reason:", state.log[-1]["msg"])
    if state.status in ("partial", "unverified"):
        print("  note: NOT a pass — only verified evidence was written")
    print(f"  artifact: .agent/missions/{state.mission.id}/artifact.json")


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent", description="Give your agent a mission, not a prompt.")
    p.add_argument("--workspace", default=".")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("mission"); r.add_argument("--yes", action="store_true", help="auto-approve ungranted actions"); r.add_argument("--max-replans", type=int, default=2)
    r.add_argument("--deadline", type=float, default=None, help="wall-clock budget in seconds; on expiry an honest partial result is written")
    s = sub.add_parser("resume"); s.add_argument("id"); s.add_argument("--yes", action="store_true"); s.add_argument("--deadline", type=float, default=None)
    po = sub.add_parser("policy", help="show the effective policy (or --init to write .agent/policy.json)")
    po.add_argument("--init", action="store_true")
    sh = sub.add_parser("show", help="contract, plan, evidence and log of one mission"); sh.add_argument("id")
    tr = sub.add_parser("trust", help="allow a plugin in ./tools/<name> to load (pins its tool.py hash)"); tr.add_argument("name")
    st = sub.add_parser("status"); st.add_argument("id", nargs="?")
    au = sub.add_parser("audit"); au.add_argument("id")
    me = sub.add_parser("memory"); me.add_argument("query", nargs="?", default="")
    cr = sub.add_parser("create"); cr.add_argument("what", choices=["tool"]); cr.add_argument("name")
    sub.add_parser("test")
    pu = sub.add_parser("publish"); pu.add_argument("name")
    a = p.parse_args(argv)
    try:
        if not (a.cmd == "policy" and a.init):
            cfg.load(a.workspace)                   # a broken policy.json is reported by every command
        return _dispatch(a)
    except cfg.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:                 # `agent show <id> | head` — exit quietly
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def _dispatch(a):

    if a.cmd in ("run", "resume"):
        rt = _rt(a, True)
        st = rt.start(a.mission) if a.cmd == "run" else rt.resume(a.id)
        _show(st)
        return EXIT.get(st.status, 1)
    elif a.cmd == "status":
        rt = Runtime(a.workspace, llm=None, judge=None, tools={}, config={})
        for mid in ([a.id] if a.id else rt.store.list()):
            s = rt.store.load(mid)
            print(f"{mid}  {s.status:10} {s.mission.text[:70]}")
    elif a.cmd == "audit":
        for line in (Path(a.workspace) / ".agent/missions" / a.id / "audit.jsonl").read_text().splitlines():
            e = json.loads(line)
            print(f"{'ALLOW' if e['allowed'] else 'DENY '} {e['who']:8} {e['tool']:16} {e['action']:8} {e['target'][:60]}  ← {e['why']} ({e['reason']})")
    elif a.cmd == "memory":
        for m in Runtime(a.workspace, llm=None, judge=None, tools={}, config={}).memory.recall(a.query):
            print(f"[{m['kind']}/{m['status']} c={m['confidence']}] {m['content']}  (src: {m['provenance']})")
    elif a.cmd == "policy":
        if a.init:
            print("wrote", cfg.init(a.workspace))
        else:
            print(json.dumps(cfg.effective(a.workspace), indent=2))
    elif a.cmd == "show":
        rt = Runtime(a.workspace, llm=None, judge=None, tools={}, config={})
        _show_mission(rt.store.load(a.id), rt.store)
    elif a.cmd == "trust":
        name, h = trust_plugin(a.workspace, a.name)
        print(f"trusted {name} (sha256 {h[:12]}…) — re-run trust after editing its tool.py")
    elif a.cmd == "create":
        print("created", create_tool(a.name, Path(a.workspace) / "tools"))
    elif a.cmd == "test":
        sys.exit(max((subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=d)
                      for d in (Path(a.workspace) / "tools").glob("*/")), default=0))
    elif a.cmd == "publish":
        print("packed", publish_tool(a.name, Path(a.workspace) / "tools", Path(a.workspace) / "dist"))


if __name__ == "__main__":
    sys.exit(main())
