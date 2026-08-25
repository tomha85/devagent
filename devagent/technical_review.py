from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from devagent.models import CodeSymbol, DeveloperReviewEvidence


_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class _PythonSymbol:
    name: str
    kind: str
    line: int
    end_line: int
    is_test: bool


def _git(root: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _line_range(start: int, count_text: str | None) -> set[int]:
    count = int(count_text) if count_text is not None else 1
    if count <= 0:
        return set()
    return set(range(start, start + count))


def _changed_lines(root: Path, path: str) -> tuple[set[int], set[int], bool]:
    tracked = _git(root, "cat-file", "-e", f"HEAD:{path}", timeout=10).returncode == 0
    target = root / path
    if not tracked:
        if not target.is_file():
            return set(), set(), False
        line_count = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        return set(), set(range(1, line_count + 1)), False

    diff = _git(root, "diff", "--unified=0", "--no-ext-diff", "--", path)
    if diff.returncode != 0:
        return set(), set(), True
    old_lines: set[int] = set()
    new_lines: set[int] = set()
    for line in diff.stdout.splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        old_lines.update(_line_range(int(match.group("old_start")), match.group("old_count")))
        new_lines.update(_line_range(int(match.group("new_start")), match.group("new_count")))
    return old_lines, new_lines, True


def _old_text(root: Path, path: str) -> str:
    completed = _git(root, "show", f"HEAD:{path}")
    return completed.stdout if completed.returncode == 0 else ""


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    parts = Path(lowered).parts
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)
        or Path(lowered).name.startswith("test_")
        or ".test." in lowered
        or ".spec." in lowered
    )


def _overlaps(symbol: _PythonSymbol, changed_lines: set[int]) -> bool:
    if not changed_lines:
        return False
    return any(symbol.line <= line <= symbol.end_line for line in changed_lines)


def _python_symbols(text: str) -> list[_PythonSymbol]:
    if not text.strip():
        return []
    tree = ast.parse(text)
    symbols: list[_PythonSymbol] = []

    def walk(body: Iterable[ast.stmt], parents: list[tuple[str, str]]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualified = ".".join([*(name for name, _kind in parents), node.name])
                symbols.append(
                    _PythonSymbol(
                        qualified,
                        "class",
                        node.lineno,
                        getattr(node, "end_lineno", node.lineno),
                        node.name.startswith("Test"),
                    )
                )
                walk(node.body, [*parents, (node.name, "class")])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = ".".join([*(name for name, _kind in parents), node.name])
                inside_class = any(kind == "class" for _name, kind in parents)
                kind = "method" if inside_class else "async function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                is_test = node.name.startswith("test_") or any(
                    kind_name == "class" and name.startswith("Test") for name, kind_name in parents
                )
                symbols.append(
                    _PythonSymbol(
                        qualified,
                        kind,
                        node.lineno,
                        getattr(node, "end_lineno", node.lineno),
                        is_test,
                    )
                )
                walk(node.body, [*parents, (node.name, "function")])

    walk(tree.body, [])
    return symbols


def _class_header_changed(symbol: _PythonSymbol, changed_lines: set[int]) -> bool:
    return symbol.line in changed_lines


def _symbol_changed(symbol: _PythonSymbol, changed_lines: set[int]) -> bool:
    if symbol.kind == "class":
        return _class_header_changed(symbol, changed_lines)
    return _overlaps(symbol, changed_lines)


def _append_unique(items: list[CodeSymbol], item: CodeSymbol) -> None:
    key = (item.path, item.name, item.kind, item.change)
    if not any((current.path, current.name, current.kind, current.change) == key for current in items):
        items.append(item)


def _analyze_python_file(root: Path, path: str, review: DeveloperReviewEvidence) -> None:
    old_changed, new_changed, tracked = _changed_lines(root, path)
    target = root / path
    old_text = _old_text(root, path) if tracked else ""
    new_text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""

    try:
        old_symbols = _python_symbols(old_text)
    except SyntaxError:
        old_symbols = []
        review.notes.append(f"Could not parse baseline Python symbols for {path}")
    try:
        new_symbols = _python_symbols(new_text)
    except SyntaxError:
        new_symbols = []
        review.notes.append(f"Could not parse final Python symbols for {path}")

    old_names = {symbol.name for symbol in old_symbols}
    new_names = {symbol.name for symbol in new_symbols}
    is_test_file = _is_test_path(path)
    if is_test_file and path not in review.test_files:
        review.test_files.append(path)

    for symbol in new_symbols:
        changed = _symbol_changed(symbol, new_changed)
        if symbol.is_test or is_test_file:
            if symbol.is_test:
                change = "ADDED" if symbol.name not in old_names else "MODIFIED" if changed else "UNCHANGED"
                _append_unique(
                    review.test_cases,
                    CodeSymbol(path, symbol.name, symbol.kind, symbol.line, change),
                )
            continue
        if changed:
            change = "ADDED" if symbol.name not in old_names else "MODIFIED"
            _append_unique(
                review.changed_symbols,
                CodeSymbol(path, symbol.name, symbol.kind, symbol.line, change),
            )

    for symbol in old_symbols:
        if not _symbol_changed(symbol, old_changed) or symbol.name in new_names:
            continue
        item = CodeSymbol(path, symbol.name, symbol.kind, symbol.line, "REMOVED")
        if symbol.is_test or is_test_file:
            if symbol.is_test:
                _append_unique(review.test_cases, item)
        else:
            _append_unique(review.changed_symbols, item)


def analyze_developer_review(working_root: Path | str, changed_paths: Iterable[str]) -> DeveloperReviewEvidence:
    """Build deterministic developer-review evidence from the final Git diff and source tree."""

    root = Path(working_root).expanduser().resolve()
    review = DeveloperReviewEvidence()
    unsupported: list[str] = []

    for path in sorted(set(changed_paths)):
        if path.endswith(".py"):
            _analyze_python_file(root, path, review)
        elif _is_test_path(path):
            if path not in review.test_files:
                review.test_files.append(path)
            unsupported.append(path)
        else:
            unsupported.append(path)

    review.changed_symbols.sort(key=lambda item: (item.path, item.line or 0, item.name))
    review.test_cases.sort(key=lambda item: (item.path, item.line or 0, item.name))
    review.test_files.sort()

    if unsupported:
        review.notes.append(
            "Symbol-level extraction is currently deterministic for Python; file-level evidence is retained for: "
            + ", ".join(sorted(unsupported))
        )
    if not review.changed_symbols:
        review.notes.append("No changed Python function/class symbols were identified from the final diff")
    if review.test_files and not review.test_cases:
        review.notes.append("Changed test files were detected, but no Python test function/method names were extracted")
    return review
