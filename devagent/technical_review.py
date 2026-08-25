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
_PATTERN_SOURCE_SUFFIXES = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".java",
    ".cs",
}


@dataclass(frozen=True)
class _PythonSymbol:
    name: str
    kind: str
    line: int
    end_line: int
    is_test: bool


@dataclass(frozen=True)
class _PatternSymbol:
    name: str
    kind: str
    line: int
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
    name = Path(lowered).name
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)
        or name.startswith("test_")
        or name.startswith("test.")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
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
                kind = (
                    "method"
                    if inside_class
                    else "async function"
                    if isinstance(node, ast.AsyncFunctionDef)
                    else "function"
                )
                is_test = node.name.startswith("test_") or any(
                    kind_name == "class" and name.startswith("Test")
                    for name, kind_name in parents
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


def _pattern_symbols(text: str, path: str) -> list[_PatternSymbol]:
    """Extract bounded deterministic symbols for common non-Python source shapes.

    This intentionally avoids pretending to be a full parser. It recognizes stable declaration
    and test forms used as acceptance/review evidence while final repository-native tests/builds
    remain authoritative for behavior.
    """

    suffix = Path(path).suffix.lower()
    test_file = _is_test_path(path)
    lines = text.splitlines()
    symbols: list[_PatternSymbol] = []
    seen: set[tuple[str, str, int]] = set()

    def add(name: str, kind: str, line: int, *, is_test: bool = False) -> None:
        cleaned = re.sub(r"\s+", " ", name.strip())
        if not cleaned:
            return
        key = (cleaned, kind, line)
        if key in seen:
            return
        seen.add(key)
        symbols.append(_PatternSymbol(cleaned, kind, line, is_test))

    for index, line in enumerate(lines, start=1):
        if suffix in {".js", ".jsx", ".ts", ".tsx"}:
            for match in re.finditer(
                r"\b(?:test|it)\s*\(\s*[\"'`]([^\"'`]+)[\"'`]",
                line,
            ):
                add(match.group(1), "test case", index, is_test=True)
            match = re.search(
                r"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
                line,
            )
            if match:
                add(match.group(1), "function", index, is_test=test_file)
            match = re.search(
                r"\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>",
                line,
            )
            if match:
                add(match.group(1), "function", index, is_test=test_file)
            match = re.search(r"\bclass\s+([A-Za-z_$][A-Za-z0-9_$]*)\b", line)
            if match:
                add(match.group(1), "class", index, is_test=test_file)

        elif suffix == ".go":
            match = re.match(
                r"\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                line,
            )
            if match:
                name = match.group(1)
                add(name, "function", index, is_test=test_file or name.startswith("Test"))

        elif suffix == ".rs":
            match = re.match(
                r"\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                line,
            )
            if match:
                context = "\n".join(lines[max(0, index - 4) : index - 1])
                add(
                    match.group(1),
                    "function",
                    index,
                    is_test=test_file or "#[test]" in context,
                )

        elif suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
            match = re.match(
                r"\s*(?:[A-Za-z_][A-Za-z0-9_:<>~*&]*\s+)+([A-Za-z_~][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*(?:const\s*)?\{",
                line,
            )
            if match and match.group(1) not in {"if", "for", "while", "switch", "catch"}:
                add(match.group(1), "function", index, is_test=test_file)

        elif suffix in {".java", ".cs"}:
            class_match = re.search(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", line)
            if class_match:
                add(class_match.group(1), "class", index, is_test=test_file)
            method_match = re.match(
                r"\s*(?:(?:public|private|protected|internal|static|final|virtual|override|async)\s+)*"
                r"[A-Za-z_][A-Za-z0-9_<>,.?\[\]]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*\{",
                line,
            )
            if method_match:
                context = "\n".join(lines[max(0, index - 4) : index - 1])
                add(
                    method_match.group(1),
                    "method",
                    index,
                    is_test=test_file or "@Test" in context or "[Test" in context,
                )

    return symbols


def _class_header_changed(symbol: _PythonSymbol, changed_lines: set[int]) -> bool:
    return symbol.line in changed_lines


def _symbol_changed(symbol: _PythonSymbol, changed_lines: set[int]) -> bool:
    if symbol.kind == "class":
        return _class_header_changed(symbol, changed_lines)
    return _overlaps(symbol, changed_lines)


def _append_unique(items: list[CodeSymbol], item: CodeSymbol) -> None:
    key = (item.path, item.name, item.kind, item.change)
    if not any(
        (current.path, current.name, current.kind, current.change) == key for current in items
    ):
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
                change = (
                    "ADDED"
                    if symbol.name not in old_names
                    else "MODIFIED"
                    if changed
                    else "UNCHANGED"
                )
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


def _analyze_pattern_file(root: Path, path: str, review: DeveloperReviewEvidence) -> None:
    old_changed, new_changed, tracked = _changed_lines(root, path)
    target = root / path
    old_text = _old_text(root, path) if tracked else ""
    new_text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    old_symbols = _pattern_symbols(old_text, path)
    new_symbols = _pattern_symbols(new_text, path)
    old_names = {symbol.name for symbol in old_symbols}
    new_names = {symbol.name for symbol in new_symbols}

    if (_is_test_path(path) or any(symbol.is_test for symbol in new_symbols)) and path not in review.test_files:
        review.test_files.append(path)

    for symbol in new_symbols:
        changed = symbol.line in new_changed
        if symbol.is_test:
            change = (
                "ADDED"
                if symbol.name not in old_names
                else "MODIFIED"
                if changed
                else "UNCHANGED"
            )
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
        if symbol.line not in old_changed or symbol.name in new_names:
            continue
        item = CodeSymbol(path, symbol.name, symbol.kind, symbol.line, "REMOVED")
        if symbol.is_test:
            _append_unique(review.test_cases, item)
        else:
            _append_unique(review.changed_symbols, item)


def analyze_developer_review(
    working_root: Path | str,
    changed_paths: Iterable[str],
) -> DeveloperReviewEvidence:
    """Build deterministic developer-review evidence from the final Git diff and source tree."""

    root = Path(working_root).expanduser().resolve()
    review = DeveloperReviewEvidence()
    unsupported: list[str] = []

    for path in sorted(set(changed_paths)):
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            _analyze_python_file(root, path, review)
        elif suffix in _PATTERN_SOURCE_SUFFIXES:
            _analyze_pattern_file(root, path, review)
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
            "File-level review evidence is retained for unsupported symbol extraction paths: "
            + ", ".join(sorted(unsupported))
        )
    if not review.changed_symbols:
        review.notes.append("No changed function/class symbols were identified from the final diff")
    if review.test_files and not review.test_cases:
        review.notes.append("Changed test files were detected, but no supported test-case declarations were extracted")
    return review
