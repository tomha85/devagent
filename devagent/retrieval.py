from __future__ import annotations

import ast
import re
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from devagent.models import RepositoryModel
from devagent.safety import SafetyError
from devagent.workspace import Workspace


_STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "when", "where", "into",
    "add", "fix", "bug", "test", "tests", "application", "without", "changing",
}
_CONCEPT_ALIASES = {
    "authentication": ("auth", "authenticate"),
    "authorization": ("authz", "authorize"),
    "configuration": ("config", "configure"),
    "reconnection": ("reconnect",),
}
_SOURCE_EXTENSIONS = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs",
        ".java", ".kt", ".kts", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb",
        ".php", ".swift", ".scala", ".sh", ".sql", ".vue", ".svelte",
    }
)
_CONFIG_EXTENSIONS = frozenset({".toml", ".yaml", ".yml"})
_CONFIG_NAMES = frozenset(
    {
        "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "tox.ini",
        "pytest.ini", "package.json", "tsconfig.json", "cargo.toml", "go.mod", "go.sum",
        "pom.xml", "build.gradle", "gradlew", "cmakelists.txt", "makefile", "meson.build",
        "dockerfile", "jenkinsfile", ".gitlab-ci.yml", "azure-pipelines.yml",
        "readme", "readme.md", "readme.rst", "contributing.md", "contributing.rst",
    }
)
_GENERATED_NAMES = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
        "poetry.lock", "uv.lock", "pipfile.lock", "cargo.lock",
    }
)
_GENERATED_PARTS = frozenset(
    {
        "vendor", "vendors", "coverage", "htmlcov", ".tox", ".nox", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".next", ".nuxt", "out", "site-packages",
    }
)


@dataclass(frozen=True)
class RetrievalBudget:
    max_files: int = 20
    max_chars: int = 24_000
    max_per_file_chars: int = 6_000
    max_fallback_files: int = 6
    small_repository_max_files: int = 20
    inventory_max_files: int = 12_000
    max_scan_chars: int = 12_000_000
    max_scan_files: int = 1_200
    max_git_grep_files: int = 400
    git_grep_timeout_seconds: int = 8
    max_relationship_files: int = 500


def _split_identifier(value: str) -> list[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", separated)
    return [token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", separated)]


def _normalized_forms(token: str) -> tuple[str, ...]:
    token = token.casefold()
    forms = [token]
    forms.extend(_CONCEPT_ALIASES.get(token, ()))
    if len(token) > 5 and token.endswith("ies"):
        forms.append(token[:-3] + "y")
    if len(token) > 5 and token.endswith("ing"):
        forms.extend((token[:-3], token[:-3] + "e"))
    if len(token) > 4 and token.endswith("ed"):
        forms.extend((token[:-2], token[:-1]))
    if len(token) > 6 and token.endswith("ation"):
        forms.append(token[:-5] + "e")
    if len(token) > 6 and token.endswith("sion"):
        forms.append(token[:-4] + "de")
    elif len(token) > 6 and token.endswith("tion"):
        forms.append(token[:-3])
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "is")):
        forms.append(token[:-1])
    return tuple(dict.fromkeys(form for form in forms if len(form) >= 3))


def task_terms(task: str) -> list[str]:
    terms: list[str] = []
    for token in _split_identifier(task):
        if token in _STOP_WORDS:
            continue
        terms.extend(_normalized_forms(token))
    return list(dict.fromkeys(terms))[:32]


def _raw_task_terms(task: str) -> list[str]:
    return list(
        dict.fromkeys(
            token
            for token in _split_identifier(task)
            if len(token) >= 3 and token not in _STOP_WORDS
        )
    )[:16]


def _is_test_path(path: str) -> bool:
    lowered = path.casefold()
    parts = Path(lowered).parts
    name = Path(lowered).name
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _file_kind(path: str) -> str | None:
    candidate = Path(path)
    lowered_parts = tuple(part.casefold() for part in candidate.parts)
    name = candidate.name.casefold()
    if any(part in _GENERATED_PARTS for part in lowered_parts):
        return None
    if name in _GENERATED_NAMES or candidate.suffix.casefold() == ".map" or ".min." in name:
        return None
    if candidate.suffix.casefold() in _SOURCE_EXTENSIONS:
        return "test" if _is_test_path(path) else "source"
    if name in _CONFIG_NAMES or candidate.suffix.casefold() in _CONFIG_EXTENSIONS:
        return "documentation" if name.startswith(("readme", "contributing")) else "manifest"
    if path.startswith(".github/workflows/") and candidate.suffix.casefold() in {".yml", ".yaml"}:
        return "manifest"
    return None


def _inventory(workspace: Workspace, budget: RetrievalBudget) -> list[tuple[str, str, int]]:
    inventory: list[tuple[str, str, int]] = []
    for relative in workspace.list_files(limit=budget.inventory_max_files):
        kind = _file_kind(relative)
        if kind is None:
            continue
        try:
            target = workspace.paths.resolve(relative, allow_missing=False)
            size = target.stat().st_size
        except (OSError, SafetyError):
            continue
        if size > 2_000_000:
            continue
        inventory.append((relative, kind, size))
    return sorted(inventory)


def _content_tokens(text: str) -> tuple[set[str], set[str]]:
    raw = set(_split_identifier(text))
    normalized = {form for token in raw for form in _normalized_forms(token)}
    return raw, normalized


def _git_grep_paths(
    root: Path,
    terms: list[str],
    allowed_paths: list[str],
    budget: RetrievalBudget,
) -> tuple[list[str], bool]:
    """Use Git's index as a bounded large-repository accelerator when available."""
    if not terms or budget.max_git_grep_files <= 0:
        return [], False
    argv = ["git", "grep", "-l", "-I", "-F"]
    for term in terms[:8]:
        argv.extend(("-e", term))
    argv.extend(("--", "."))
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(budget.git_grep_timeout_seconds), 30)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], False
    if completed.returncode not in {0, 1}:
        return [], False
    allowed = set(allowed_paths)
    matches: list[str] = []
    for raw in completed.stdout.splitlines():
        path = raw.removeprefix("./").strip()
        if path not in allowed or path in matches:
            continue
        matches.append(path)
        if len(matches) >= budget.max_git_grep_files:
            break
    return matches, True


def _python_module_map(paths: list[str]) -> dict[str, str]:
    modules: dict[str, str] = {}
    for path in paths:
        candidate = Path(path)
        if candidate.suffix.casefold() not in {".py", ".pyi"}:
            continue
        parts = list(candidate.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.setdefault(".".join(parts), path)
            modules.setdefault(parts[-1], path)
    return modules


def _python_import_relationships(
    workspace: Workspace, paths: list[str], content_cache: dict[str, str], budget: RetrievalBudget
) -> list[dict[str, str]]:
    module_map = _python_module_map(paths)
    relationships: set[tuple[str, str, str]] = set()
    parsed = 0
    for path in paths:
        if parsed >= budget.max_relationship_files or Path(path).suffix.casefold() not in {".py", ".pyi"}:
            continue
        try:
            text = content_cache.get(path)
            if text is None:
                text = workspace.read_file(path, max_chars=80_000)
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeError, SafetyError):
            continue
        parsed += 1
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for module in imported:
            target = module_map.get(module) or module_map.get(module.split(".")[0])
            if not target or target == path:
                continue
            if _is_test_path(path) and not _is_test_path(target):
                relationships.add((target, path, "python_import"))
            elif _is_test_path(target) and not _is_test_path(path):
                relationships.add((path, target, "python_import"))
    return [
        {"source": source, "test": test, "kind": kind}
        for source, test, kind in sorted(relationships)
    ]


def _naming_relationships(paths: list[str]) -> list[dict[str, str]]:
    path_set = set(paths)
    relationships: set[tuple[str, str, str]] = set()
    for test in paths:
        candidate = Path(test)
        if not _is_test_path(test) or candidate.suffix.casefold() not in _SOURCE_EXTENSIONS:
            continue
        stem = candidate.stem
        if stem.startswith("test_"):
            source_stem = stem[5:]
        elif stem.endswith("_test"):
            source_stem = stem[:-5]
        else:
            source_stem = stem.removesuffix(".test").removesuffix(".spec")
        possible = [
            candidate.with_name(source_stem + candidate.suffix).as_posix(),
            (candidate.parent.parent / (source_stem + candidate.suffix)).as_posix(),
        ]
        for source in possible:
            if source in path_set and source != test and not _is_test_path(source):
                relationships.add((source, test, "test_naming"))
    return [
        {"source": source, "test": test, "kind": kind}
        for source, test, kind in sorted(relationships)
    ]


def _javascript_relationships(
    paths: list[str], content_cache: dict[str, str]
) -> list[dict[str, str]]:
    path_set = set(paths)
    relationships: set[tuple[str, str, str]] = set()
    extensions = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
    pattern = re.compile(r"(?:from\s+|require\s*\(\s*)[\"'](\.[^\"']+)")
    for path in paths:
        if not _is_test_path(path) or Path(path).suffix.casefold() not in extensions:
            continue
        text = content_cache.get(path)
        if text is None:
            continue
        for import_path in pattern.findall(text):
            base = (Path(path).parent / import_path).as_posix()
            candidates = [base, *(base + extension for extension in extensions)]
            candidates.extend((Path(base) / ("index" + extension)).as_posix() for extension in extensions)
            for source in candidates:
                if source in path_set and not _is_test_path(source):
                    relationships.add((source, path, "relative_import"))
                    break
    return [
        {"source": source, "test": test, "kind": kind}
        for source, test, kind in sorted(relationships)
    ]


def _relationships(
    workspace: Workspace, paths: list[str], content_cache: dict[str, str], budget: RetrievalBudget
) -> list[dict[str, str]]:
    combined = [
        *_python_import_relationships(workspace, paths, content_cache, budget),
        *_javascript_relationships(paths, content_cache),
        *_naming_relationships(paths),
    ]
    unique = {(item["source"], item["test"], item["kind"]): item for item in combined}
    return [unique[key] for key in sorted(unique)]


def _component_for(path: str, repository: RepositoryModel) -> str:
    candidates = [
        component.path
        for component in repository.components
        if component.path == "." or path == component.path or path.startswith(component.path.rstrip("/") + "/")
    ]
    return max(candidates, key=len, default=".")


def retrieve_context(
    workspace: Workspace,
    repository: RepositoryModel,
    task: str,
    max_chars: int = 24_000,
    *,
    requires_tests: bool | None = None,
    budget: RetrievalBudget | None = None,
) -> dict[str, object]:
    configured = budget or RetrievalBudget(max_chars=max_chars)
    if max_chars != configured.max_chars:
        configured = RetrievalBudget(**{**asdict(configured), "max_chars": max_chars})
    requires_tests = (
        any(term in task.casefold() for term in ("test", "regression", "verify"))
        if requires_tests is None
        else requires_tests
    )
    inventory = _inventory(workspace, configured)
    paths = [path for path, _, _ in inventory]
    kinds = {path: kind for path, kind, _ in inventory}
    sizes = {path: size for path, _, size in inventory}
    raw_terms = _raw_task_terms(task)
    terms = task_terms(task)
    scores: defaultdict[str, int] = defaultdict(int)
    lexical_scores: defaultdict[str, int] = defaultdict(int)
    matches: list[str] = []
    content_cache: dict[str, str] = {}
    scanned_chars = 0
    exact_lexical_matches = 0
    normalized_lexical_matches = 0

    for path in paths:
        path_raw, path_normalized = _content_tokens(path)
        exact_path = path_raw.intersection(raw_terms)
        normalized_path = path_normalized.intersection(terms)
        if exact_path:
            lexical_scores[path] += 12 * len(exact_path)
            exact_lexical_matches += len(exact_path)
        if normalized_path:
            lexical_scores[path] += 6 * len(normalized_path)
            normalized_lexical_matches += len(normalized_path - exact_path)

    git_grep_paths, git_grep_used = _git_grep_paths(
        workspace.root,
        terms,
        paths,
        configured,
    )
    git_priority = set(git_grep_paths)
    scan_order = sorted(
        paths,
        key=lambda path: (
            0 if path in git_priority else 1 if lexical_scores[path] > 0 else 2,
            -lexical_scores[path],
            0 if kinds[path] == "source" else 1 if kinds[path] == "test" else 2,
            path,
        ),
    )
    scanned_files = 0
    for path in scan_order:
        if scanned_files >= configured.max_scan_files or scanned_chars >= configured.max_scan_chars:
            break
        allowance = min(200_000, configured.max_scan_chars - scanned_chars)
        if allowance <= 0:
            break
        try:
            text = workspace.read_file(path, max_chars=allowance)
        except (OSError, UnicodeError, SafetyError):
            continue
        scanned_files += 1
        content_cache[path] = text
        scanned_chars += min(len(text), allowance)
        raw, normalized = _content_tokens(text)
        exact = raw.intersection(raw_terms)
        related = normalized.intersection(terms)
        if exact:
            lexical_scores[path] += 8 * len(exact)
            exact_lexical_matches += len(exact)
            matches.append(f"{path}: exact terms: {', '.join(sorted(exact))}")
        if related:
            lexical_scores[path] += 4 * len(related)
            normalized_lexical_matches += len(related - exact)
            if not exact:
                matches.append(f"{path}: normalized terms: {', '.join(sorted(related))}")

    for path in paths:
        scores[path] = lexical_scores[path]
        if kinds[path] == "test" and requires_tests:
            scores[path] += 3
        elif kinds[path] == "source":
            scores[path] += 1
        elif kinds[path] == "manifest":
            scores[path] += 1

    relationships = _relationships(workspace, paths, content_cache, configured)
    lexical_paths = {path for path, score in lexical_scores.items() if score > 0}
    for relationship in relationships:
        source, test = relationship["source"], relationship["test"]
        if source in lexical_paths:
            scores[test] += 10
        if test in lexical_paths:
            scores[source] += 10

    small_repository = (
        len(inventory) <= configured.small_repository_max_files
        and sum(sizes.values()) <= configured.max_chars
        and all(size <= configured.max_per_file_chars for size in sizes.values())
    )
    fallback = "none"
    fallback_paths: list[str] = []
    if small_repository:
        selected = paths[: configured.max_files]
        fallback = "small-repository inventory"
    else:
        ranked_lexical = sorted(
            (path for path in paths if lexical_scores[path] > 0),
            key=lambda path: (-scores[path], path),
        )
        primary_limit = max(1, configured.max_files - configured.max_fallback_files)
        selected = ranked_lexical[:primary_limit]
        selected_kinds = {kinds[path] for path in selected}
        low_coverage = (
            not any(kinds[path] == "source" for path in selected)
            or selected_kinds <= {"manifest", "documentation"}
            or (requires_tests and not any(kinds[path] == "test" for path in selected))
            or len(selected) < 2
        )
        if low_coverage:
            fallback = "bounded structural coverage"
            candidates: list[str] = []
            for relationship in relationships:
                if relationship["source"] in selected or relationship["test"] in selected:
                    candidates.extend((relationship["source"], relationship["test"]))
            matched_components = {_component_for(path, repository) for path in selected}
            if selected:
                parents = {str(Path(path).parent) for path in selected}
                candidates.extend(path for path in paths if str(Path(path).parent) in parents)
                candidates.extend(
                    path for path in paths if _component_for(path, repository) in matched_components
                )
            structural = sorted(
                (
                    path for path in paths
                    if kinds[path] in ({"source", "test", "manifest"} if requires_tests else {"source", "manifest"})
                ),
                key=lambda path: (
                    0 if requires_tests and kinds[path] == "test" else 1 if kinds[path] == "source" else 2,
                    -scores[path],
                    path,
                ),
            )
            candidates.extend(structural)
            if any(term in task.casefold() for term in ("architecture", "setup", "build", "command")):
                candidates.extend(path for path in paths if kinds[path] == "documentation")
            for path in candidates:
                if path in selected or path in fallback_paths:
                    continue
                fallback_paths.append(path)
                if len(fallback_paths) >= configured.max_fallback_files:
                    break
            selected.extend(fallback_paths)
        selected = selected[: configured.max_files]

    snippets: dict[str, str] = {}
    remaining = configured.max_chars
    for path in selected:
        if remaining <= 0:
            break
        try:
            content = workspace.read_file(path, max_chars=min(configured.max_per_file_chars, remaining))
        except (OSError, UnicodeError, SafetyError):
            continue
        snippets[path] = content
        remaining -= len(content)

    selected = list(snippets)
    return {
        "terms": terms,
        "ranked_paths": selected,
        "matches": matches[:100],
        "snippets": snippets,
        "relationships": [
            relationship
            for relationship in relationships
            if relationship["source"] in selected or relationship["test"] in selected
        ],
        "diagnostics": {
            "repository_files": len(inventory),
            "inventory_truncated": len(inventory) >= configured.inventory_max_files,
            "scanned_files": scanned_files,
            "scanned_chars": scanned_chars,
            "scan_truncated": scanned_files < len(paths),
            "git_grep_used": git_grep_used,
            "git_grep_paths": git_grep_paths,
            "exact_lexical_matches": exact_lexical_matches,
            "normalized_lexical_matches": normalized_lexical_matches,
            "fallback": fallback,
            "fallback_paths": fallback_paths,
            "selected": selected,
            "relationship_count": len(relationships),
            "budgets": asdict(configured),
        },
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
