import json
import os
import time
import uuid
from pathlib import Path
from .models import RunState


class CheckpointStore:
    def __init__(self, root):
        self.root = Path(root)

    def dir(self, mission_id):
        d = self.root / mission_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, state: RunState):
        d = self.dir(state.mission.id)
        tmp = d / f"state.json.{uuid.uuid4().hex[:6]}.tmp"
        tmp.write_text(json.dumps(state.to_dict(), indent=1, default=str), encoding="utf-8")
        for attempt in range(8):                    # atomic; retried because Windows AV/indexers briefly lock files
            try:
                os.replace(tmp, d / "state.json")
                return
            except PermissionError:
                time.sleep(0.05 * 2 ** attempt)
        os.replace(tmp, d / "state.json")

    def load(self, mission_id) -> RunState:
        return RunState.from_dict(json.loads((self.root / mission_id / "state.json").read_text(encoding="utf-8")))

    def list(self):
        return sorted(p.parent.name for p in self.root.glob("*/state.json")) if self.root.exists() else []
