from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any

from devagent.models import jsonable


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + token_hex(3)


class RunArtifacts:
    def __init__(self, repository_root: Path | str, run_id: str | None = None) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.run_id = run_id or new_run_id()
        self.root = self.repository_root / ".devagent" / "runs" / self.run_id
        self.backups = self.root / "backups"
        self.backups.mkdir(parents=True, exist_ok=False)
        self.observations = self.root / "observations.jsonl"
        self.verification = self.root / "verification.json"
        self.report = self.root / "report.json"

    def write_json(self, name: str, value: Any) -> None:
        target = self.root / name
        target.write_text(json.dumps(jsonable(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def record(self, event: str, **data: Any) -> None:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **jsonable(data)}
        with self.observations.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def backup(self, target: Path, *, relative_to: Path | None = None) -> Path | None:
        if not target.exists():
            return None
        relative = target.resolve().relative_to((relative_to or self.repository_root).resolve())
        destination = self.backups / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(target, destination)
            self.record("backup_created", path=relative.as_posix(), backup=str(destination))
        return destination
