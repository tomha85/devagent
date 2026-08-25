from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from devagent.autonomy import AgentTask, ParallelAgentCoordinator


_MIN_INTERVAL_SECONDS = 300
_MAX_AUTOMATIONS = 64


@dataclass(frozen=True)
class Automation:
    id: str
    requirement: str
    interval_seconds: int
    next_run_epoch: int
    enabled: bool = True
    last_outcome: str | None = None


class AutomationStore:
    def __init__(self, repository_root: Path | str) -> None:
        self.root = Path(repository_root).expanduser().resolve()
        self.path = self.root / ".devagent" / "automations.json"

    def load(self) -> tuple[Automation, ...]:
        if not self.path.is_file():
            return ()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not isinstance(payload.get("automations"), list):
            raise ValueError("invalid DevAgent automation store")
        items = tuple(Automation(**item) for item in payload["automations"])
        if len(items) > _MAX_AUTOMATIONS:
            raise ValueError("automation store exceeds bounded entry limit")
        return items

    def save(self, automations: tuple[Automation, ...]) -> None:
        if len(automations) > _MAX_AUTOMATIONS:
            raise ValueError("too many automations")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "automations": [asdict(item) for item in automations],
        }
        fd, temporary = tempfile.mkstemp(prefix="automations-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def upsert(self, automation: Automation) -> None:
        if not automation.id.strip() or "/" in automation.id or "\\" in automation.id:
            raise ValueError("automation id must be a simple non-empty identifier")
        if not automation.requirement.strip():
            raise ValueError("automation requirement must be non-empty")
        if automation.interval_seconds < _MIN_INTERVAL_SECONDS:
            raise ValueError(f"automation interval must be at least {_MIN_INTERVAL_SECONDS} seconds")
        items = {item.id: item for item in self.load()}
        items[automation.id] = automation
        self.save(tuple(items[key] for key in sorted(items)))

    def due(self, now_epoch: int | None = None) -> tuple[Automation, ...]:
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        return tuple(item for item in self.load() if item.enabled and item.next_run_epoch <= now)

    def record_outcomes(self, outcomes: dict[str, str], now_epoch: int | None = None) -> None:
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        updated: list[Automation] = []
        for item in self.load():
            if item.id not in outcomes:
                updated.append(item)
                continue
            updated.append(
                Automation(
                    id=item.id,
                    requirement=item.requirement,
                    interval_seconds=item.interval_seconds,
                    next_run_epoch=now + item.interval_seconds,
                    enabled=item.enabled,
                    last_outcome=outcomes[item.id],
                )
            )
        self.save(tuple(updated))


def run_due(repository_root: Path | str, *, max_parallel: int = 2, now_epoch: int | None = None) -> tuple[tuple[str, str], ...]:
    store = AutomationStore(repository_root)
    due = store.due(now_epoch)
    if not due:
        return ()
    coordinator = ParallelAgentCoordinator(repository_root, max_parallel=max_parallel)
    results = coordinator.run(AgentTask(item.id, item.requirement) for item in due)
    outcomes = {item.id: item.outcome for item in results}
    store.record_outcomes(outcomes, now_epoch)
    return tuple((item.id, item.outcome) for item in results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage explicit foreground DevAgent automations")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("--id", required=True)
    add.add_argument("--every-seconds", required=True, type=int)
    add.add_argument("--requirement", required=True)

    sub.add_parser("list")
    run = sub.add_parser("run-due")
    run.add_argument("--max-parallel", type=int, default=2)

    args = parser.parse_args(argv)
    store = AutomationStore(args.repo)
    try:
        if args.command == "add":
            now = int(time.time())
            store.upsert(Automation(args.id, args.requirement, args.every_seconds, now))
            print(f"Saved automation: {args.id}")
            return 0
        if args.command == "list":
            print(json.dumps([asdict(item) for item in store.load()], indent=2))
            return 0
        outcomes = run_due(args.repo, max_parallel=args.max_parallel)
        print(json.dumps([{"id": item, "outcome": outcome} for item, outcome in outcomes], indent=2))
        return 0 if all(outcome == "VERIFIED" for _, outcome in outcomes) else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"DevAgent automation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
