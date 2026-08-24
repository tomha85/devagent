from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from devagent.discovery import facts_are_current
from devagent.models import RepositoryFact, jsonable


class RepositoryMemory:
    """Bounded, evidence-invalidated local repository and strategy memory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / ".devagent" / "memory"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.facts_file = self.directory / "repository.json"
        self.strategies_file = self.directory / "strategies.json"

    def store_facts(self, facts: Iterable[RepositoryFact]) -> None:
        bounded = list(facts)[-250:]
        self.facts_file.write_text(json.dumps(jsonable(bounded), indent=2) + "\n", encoding="utf-8")

    def load_facts(self) -> list[RepositoryFact]:
        if not self.facts_file.is_file():
            return []
        try:
            values = json.loads(self.facts_file.read_text(encoding="utf-8"))
            facts = [RepositoryFact(item["fact"], item["confidence"], tuple(item["evidence"]), item["fingerprints"], item["learned_at"]) for item in values]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return []
        if not facts_are_current(self.root, facts):
            self.facts_file.unlink(missing_ok=True)
            return []
        return facts

    def store_strategy(self, statement: str, evidence: list[str]) -> None:
        if not statement.strip() or not evidence:
            return
        values: list[dict[str, object]] = []
        if self.strategies_file.is_file():
            try:
                values = json.loads(self.strategies_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                values = []
        entry = {"statement": statement.strip(), "evidence": evidence[:10]}
        if entry not in values:
            values.append(entry)
        self.strategies_file.write_text(json.dumps(values[-100:], indent=2) + "\n", encoding="utf-8")

