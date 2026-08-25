from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from secrets import token_hex
from typing import Iterator

from devagent.autonomy import AgentTask, AutonomyError, ParallelAgentCoordinator


_MIN_INTERVAL_SECONDS = 300
_MAX_AUTOMATIONS = 64
_CLAIM_LEASE_SECONDS = 3600
_LOCK_WAIT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.02


@dataclass(frozen=True)
class Automation:
    id: str
    requirement: str
    interval_seconds: int
    next_run_epoch: int
    enabled: bool = True
    last_outcome: str | None = None
    claim_id: str | None = None
    claim_until_epoch: int | None = None


class AutomationStore:
    def __init__(self, repository_root: Path | str) -> None:
        self.root = Path(repository_root).expanduser().resolve()
        self.path = self.root / ".devagent" / "automations.json"
        self.lock_path = self.root / ".devagent" / "automations.lock"

    def _load_unlocked(self) -> tuple[Automation, ...]:
        if not self.path.is_file():
            return ()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not isinstance(payload.get("automations"), list):
            raise ValueError("invalid DevAgent automation store")
        try:
            items = tuple(Automation(**item) for item in payload["automations"])
        except TypeError as exc:
            raise ValueError("invalid DevAgent automation entry") from exc
        if len(items) > _MAX_AUTOMATIONS:
            raise ValueError("automation store exceeds bounded entry limit")
        return items

    def load(self) -> tuple[Automation, ...]:
        # Writers publish with os.replace(), so an unlocked reader sees either the
        # complete previous document or the complete new document.
        return self._load_unlocked()

    def _save_unlocked(self, automations: tuple[Automation, ...]) -> None:
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

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Kernel-backed interprocess lock; automatically releases after crashes."""

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            if os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
            deadline = time.monotonic() + _LOCK_WAIT_SECONDS
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ValueError("automation store is busy with another scheduler") from exc
                    time.sleep(_LOCK_RETRY_SECONDS)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, automations: tuple[Automation, ...]) -> None:
        with self._exclusive_lock():
            self._save_unlocked(automations)

    def upsert(self, automation: Automation) -> None:
        if not automation.id.strip() or "/" in automation.id or "\\" in automation.id:
            raise ValueError("automation id must be a simple non-empty identifier")
        if not automation.requirement.strip():
            raise ValueError("automation requirement must be non-empty")
        if automation.interval_seconds < _MIN_INTERVAL_SECONDS:
            raise ValueError(f"automation interval must be at least {_MIN_INTERVAL_SECONDS} seconds")
        with self._exclusive_lock():
            items = {item.id: item for item in self._load_unlocked()}
            items[automation.id] = automation
            self._save_unlocked(tuple(items[key] for key in sorted(items)))

    @staticmethod
    def _claim_available(item: Automation, now: int) -> bool:
        return item.claim_until_epoch is None or item.claim_until_epoch <= now

    def due(self, now_epoch: int | None = None) -> tuple[Automation, ...]:
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        return tuple(
            item
            for item in self.load()
            if item.enabled and item.next_run_epoch <= now and self._claim_available(item, now)
        )

    def claim_due(self, now_epoch: int | None = None) -> tuple[Automation, ...]:
        """Atomically reserve due entries before any provider/agent work starts."""

        now = int(time.time()) if now_epoch is None else int(now_epoch)
        claimed: list[Automation] = []
        with self._exclusive_lock():
            updated: list[Automation] = []
            for item in self._load_unlocked():
                if item.enabled and item.next_run_epoch <= now and self._claim_available(item, now):
                    reserved = replace(
                        item,
                        claim_id=token_hex(12),
                        claim_until_epoch=now + max(_CLAIM_LEASE_SECONDS, item.interval_seconds),
                    )
                    claimed.append(reserved)
                    updated.append(reserved)
                else:
                    updated.append(item)
            if claimed:
                self._save_unlocked(tuple(updated))
        return tuple(claimed)

    def record_outcomes(
        self,
        outcomes: dict[str, str],
        *,
        claims: dict[str, str],
        now_epoch: int | None = None,
    ) -> None:
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        with self._exclusive_lock():
            updated: list[Automation] = []
            changed = False
            for item in self._load_unlocked():
                outcome = outcomes.get(item.id)
                claim = claims.get(item.id)
                if outcome is None or claim is None or item.claim_id != claim:
                    updated.append(item)
                    continue
                updated.append(
                    replace(
                        item,
                        next_run_epoch=now + item.interval_seconds,
                        last_outcome=outcome,
                        claim_id=None,
                        claim_until_epoch=None,
                    )
                )
                changed = True
            if changed:
                self._save_unlocked(tuple(updated))


def run_due(repository_root: Path | str, *, max_parallel: int = 2, now_epoch: int | None = None) -> tuple[tuple[str, str], ...]:
    store = AutomationStore(repository_root)
    claimed = store.claim_due(now_epoch)
    if not claimed:
        return ()
    coordinator = ParallelAgentCoordinator(repository_root, max_parallel=max_parallel)
    results = coordinator.run(AgentTask(item.id, item.requirement) for item in claimed)
    outcomes = {item.id: item.outcome for item in results}
    claims = {item.id: item.claim_id for item in claimed if item.claim_id is not None}
    store.record_outcomes(outcomes, claims=claims, now_epoch=now_epoch)
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
    except (AutonomyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"DevAgent automation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
