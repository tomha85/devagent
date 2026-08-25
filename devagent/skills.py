from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devagent.providers import ModelProvider


_MAX_SKILLS = 64
_MAX_SKILL_BYTES = 64 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str
    path: str


class SkillRegistry:
    def __init__(self, skills: tuple[Skill, ...] = ()) -> None:
        self.skills = skills

    @classmethod
    def discover(cls, repository_root: Path | str) -> "SkillRegistry":
        root = Path(repository_root).expanduser().resolve()
        base = root / ".devagent" / "skills"
        if not base.is_dir() or base.is_symlink():
            return cls()
        skills: list[Skill] = []
        for directory in sorted(base.iterdir(), key=lambda item: item.name):
            if len(skills) >= _MAX_SKILLS:
                break
            if directory.is_symlink() or not directory.is_dir() or not _NAME.fullmatch(directory.name):
                continue
            target = directory / "SKILL.md"
            if target.is_symlink() or not target.is_file():
                continue
            try:
                if target.stat().st_size > _MAX_SKILL_BYTES:
                    continue
                with target.open("rb") as handle:
                    data = handle.read(_MAX_SKILL_BYTES + 1)
            except OSError:
                continue
            if len(data) > _MAX_SKILL_BYTES or b"\x00" in data:
                continue
            try:
                content = data.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not content:
                continue
            first = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), directory.name)
            skills.append(
                Skill(
                    name=directory.name,
                    description=first[:240],
                    content=content,
                    path=target.relative_to(root).as_posix(),
                )
            )
        return cls(tuple(skills))

    def match(self, requirement: str, *, limit: int = 3) -> tuple[Skill, ...]:
        if limit < 1:
            return ()
        query = {token.lower() for token in _TOKEN.findall(requirement)}
        scored: list[tuple[int, str, Skill]] = []
        for skill in self.skills:
            haystack = f"{skill.name} {skill.description} {skill.content[:4096]}"
            tokens = {token.lower() for token in _TOKEN.findall(haystack)}
            overlap = len(query & tokens)
            if overlap:
                scored.append((overlap, skill.name, skill))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in scored[: min(limit, 8)])


class SkillAwareProvider:
    """Provider wrapper that injects only bounded, requirement-relevant repo skills."""

    def __init__(self, provider: ModelProvider, registry: SkillRegistry) -> None:
        self.provider = provider
        self.registry = registry

    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        requirement = str(payload.get("requirement") or payload.get("task") or payload.get("goal") or "")
        matched = self.registry.match(requirement)
        if not matched:
            return self.provider.request(role=role, payload=payload, schema=schema)
        enriched = dict(payload)
        enriched["repository_skills"] = [
            {
                "name": skill.name,
                "description": skill.description,
                "path": skill.path,
                "content": skill.content,
            }
            for skill in matched
        ]
        return self.provider.request(role=role, payload=enriched, schema=schema)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect bounded repository-local DevAgent skills")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--match", default=None, help="Show skills relevant to this requirement")
    args = parser.parse_args(argv)
    registry = SkillRegistry.discover(args.repo)
    skills = registry.match(args.match) if args.match else registry.skills
    print(json.dumps([{"name": item.name, "description": item.description, "path": item.path} for item in skills], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
