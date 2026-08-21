import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


class AgentMemory:
    """Small local memory stored under .devagent/ in the target repository."""

    def __init__(self, repo_root: str) -> None:
        self.root = Path(repo_root).expanduser().resolve() / ".devagent" / "memory"
        self.root.mkdir(parents=True, exist_ok=True)
        self.attempts_file = self.root / "attempts.jsonl"
        self.lessons_file = self.root / "lessons.md"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_attempt(
        self,
        *,
        task: str,
        step: int,
        action: Dict[str, Any],
        result: str,
    ) -> None:
        entry = {
            "timestamp": self._now(),
            "task": task,
            "step": step,
            "action": action,
            "result": result[-8000:],
        }
        with self.attempts_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent_attempts(self, limit: int = 6) -> List[Dict[str, Any]]:
        if not self.attempts_file.exists():
            return []
        lines = self.attempts_file.read_text(encoding="utf-8").splitlines()
        items: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items

    def record_lesson(self, task: str, lesson: str) -> None:
        lesson = lesson.strip()
        if not lesson:
            return
        with self.lessons_file.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {self._now()}\n")
            handle.write(f"Task: {task}\n\n")
            handle.write(lesson + "\n")

    def lessons(self, max_chars: int = 8000) -> str:
        if not self.lessons_file.exists():
            return ""
        text = self.lessons_file.read_text(encoding="utf-8")
        return text[-max_chars:]

    def context(self) -> str:
        attempts = self.recent_attempts()
        attempts_text = "\n".join(
            f"- step={item.get('step')} action={item.get('action')} result={str(item.get('result', ''))[:800]}"
            for item in attempts
        )
        lessons = self.lessons()
        parts = []
        if lessons:
            parts.append("LESSONS:\n" + lessons)
        if attempts_text:
            parts.append("RECENT ATTEMPTS:\n" + attempts_text)
        return "\n\n".join(parts)
