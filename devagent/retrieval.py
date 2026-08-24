from __future__ import annotations

import re
from pathlib import Path

from devagent.models import RepositoryModel
from devagent.workspace import Workspace
from devagent.safety import SafetyError


_STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "when", "where", "into", "add", "fix", "bug", "test", "tests",
}


def task_terms(task: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", task)
    return list(dict.fromkeys(token.casefold() for token in tokens if token.casefold() not in _STOP_WORDS))[:12]


def retrieve_context(workspace: Workspace, repository: RepositoryModel, task: str, max_chars: int = 24_000) -> dict[str, object]:
    terms = task_terms(task)
    ranked: dict[str, int] = {}
    matches: list[str] = []
    for term in terms:
        for match in workspace.search_text(term, limit=35):
            path = match.split(":", 1)[0]
            ranked[path] = ranked.get(path, 0) + 1
            matches.append(match)
    for path in workspace.list_files(limit=600):
        stem_tokens = set(re.findall(r"[a-z0-9]+", Path(path).stem.casefold()))
        ranked[path] = ranked.get(path, 0) + len(stem_tokens.intersection(terms)) * 2
    selected = [path for path, score in sorted(ranked.items(), key=lambda item: (-item[1], item[0])) if score > 0][:12]
    snippets: dict[str, str] = {}
    remaining = max_chars
    for path in selected:
        if remaining <= 0:
            break
        try:
            content = workspace.read_file(path, max_chars=min(6000, remaining))
        except (OSError, UnicodeError, SafetyError):
            continue
        snippets[path] = content
        remaining -= len(content)
    return {
        "terms": terms,
        "ranked_paths": selected,
        "matches": matches[:100],
        "snippets": snippets,
        "repo_map": [
            {
                "path": component.path,
                "languages": component.languages,
                "frameworks": component.frameworks,
                "manifests": component.manifests,
                "tests": component.test_locations,
            }
            for component in repository.components
        ],
    }
