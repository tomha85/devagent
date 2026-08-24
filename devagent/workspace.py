from __future__ import annotations

import fnmatch
import os
import subprocess
import time
from pathlib import Path
from typing import Sequence

from devagent.artifacts import RunArtifacts
from devagent.models import FailureClass, VerificationResult
from devagent.safety import CommandPolicy, PathPolicy, SKIP_DIRECTORIES, SafetyError, is_secret_path


class Workspace:
    def __init__(self, root: Path | str, artifacts: RunArtifacts, dirty_files: Sequence[str] = ()) -> None:
        self.root = Path(root).resolve()
        self.paths = PathPolicy(self.root)
        self.artifacts = artifacts
        self.dirty_files = frozenset(dirty_files)
        self.revision = 0
        self.modified_paths: set[str] = set()

    def _relative(self, target: Path) -> str:
        return target.relative_to(self.root).as_posix()

    def _writable(self, relative: str) -> Path:
        target = self.paths.resolve(relative)
        normalized = self._relative(target)
        if normalized in self.dirty_files:
            raise SafetyError(f"Refusing to overwrite pre-existing developer modification: {normalized}")
        return target

    def list_files(self, path: str = ".", pattern: str = "*", limit: int = 250) -> list[str]:
        base = self.paths.resolve(path, allow_missing=False)
        candidates = [base] if base.is_file() else base.rglob("*")
        results: list[str] = []
        for candidate in candidates:
            if len(results) >= min(limit, 1000):
                break
            relative = candidate.relative_to(self.root)
            if not candidate.is_file() or any(part in SKIP_DIRECTORIES for part in relative.parts) or is_secret_path(relative):
                continue
            rendered = relative.as_posix()
            if fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(rendered, pattern):
                results.append(rendered)
        return results

    def read_file(self, path: str, max_chars: int = 60_000) -> str:
        target = self.paths.resolve(path, allow_missing=False)
        if not target.is_file() or target.stat().st_size > 2_000_000:
            raise SafetyError(f"File is not safe bounded text: {path}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SafetyError(f"File is not UTF-8 text: {path}") from exc
        return text[:max_chars] + (f"\n... truncated at {max_chars} characters" if len(text) > max_chars else "")

    def search_text(self, query: str, path: str = ".", limit: int = 100) -> list[str]:
        if not query.strip():
            raise SafetyError("Search query cannot be empty")
        matches: list[str] = []
        for relative in self.list_files(path, "*", limit=1000):
            target = self.root / relative
            if target.stat().st_size > 2_000_000:
                continue
            try:
                lines = target.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if query.casefold() in line.casefold():
                    matches.append(f"{relative}:{line_number}: {line[:500]}")
                    if len(matches) >= limit:
                        return matches
        return matches

    def write_file(self, path: str, content: str) -> None:
        target = self._writable(path)
        if target.exists() and not target.is_file():
            raise SafetyError(f"Write target is not a file: {path}")
        self.artifacts.backup(target, relative_to=self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.revision += 1
        self.modified_paths.add(self._relative(target))
        self.artifacts.record("file_written", path=path, revision=self.revision)

    def replace_text(self, path: str, old: str, new: str, count: int = 1) -> None:
        if not old or count < 1:
            raise SafetyError("replace_text requires non-empty old text and count >= 1")
        target = self._writable(path)
        if not target.is_file():
            raise SafetyError(f"Replace target is not a file: {path}")
        source = target.read_text(encoding="utf-8")
        occurrences = source.count(old)
        if occurrences < count:
            raise SafetyError(f"Expected {count} exact occurrence(s), found {occurrences}: {path}")
        self.artifacts.backup(target, relative_to=self.root)
        target.write_text(source.replace(old, new, count), encoding="utf-8")
        self.revision += 1
        self.modified_paths.add(self._relative(target))
        self.artifacts.record("text_replaced", path=path, count=count, revision=self.revision)

    def run(self, command: str | Sequence[str], *, timeout: int = 300, phase: str, baseline: bool = False) -> VerificationResult:
        argv = CommandPolicy.validate(command)
        timeout = max(1, min(int(timeout), 1800))
        started = time.monotonic()
        timed_out = False
        sandbox_home = self.artifacts.root / "command-home"
        sandbox_home.mkdir(exist_ok=True)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "SYSTEMROOT", "WINDIR"}
        }
        environment.update({"HOME": str(sandbox_home), "CI": "true", "DEVAGENT_RUN_ID": self.artifacts.run_id})
        try:
            completed = subprocess.run(
                argv, cwd=self.root, env=environment, capture_output=True, text=True, timeout=timeout, check=False
            )
            exit_code: int | None = completed.returncode
            stdout, stderr = completed.stdout[-24_000:], completed.stderr[-24_000:]
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = (exc.stdout or "")[-24_000:] if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "")[-24_000:] if isinstance(exc.stderr, str) else ""
        duration = time.monotonic() - started
        classification = None if exit_code == 0 else classify_failure(stdout, stderr, timed_out)
        result = VerificationResult(argv, exit_code, duration, stdout, stderr, classification, self.revision, phase, timed_out, baseline)
        self.artifacts.record("command_finished", result=result)
        return result


def classify_failure(stdout: str, stderr: str, timed_out: bool = False) -> FailureClass:
    if timed_out:
        return FailureClass.TIMEOUT
    text = f"{stdout}\n{stderr}".lower()
    patterns = (
        (FailureClass.SYNTAX_ERROR, ("syntaxerror", "syntax error")),
        (FailureClass.IMPORT_ERROR, ("importerror", "modulenotfounderror", "cannot find module", "no module named")),
        (FailureClass.TYPE_ERROR, ("typeerror", "type error", "mypy")),
        (FailureClass.ASSERTION_FAILURE, ("assertionerror", "assertion failed", " failed")),
        (FailureClass.DEPENDENCY_ERROR, ("command not found", "could not resolve", "package is not installed")),
        (FailureClass.BUILD_ERROR, ("build failed", "compilation failed", "linker")),
        (FailureClass.ENVIRONMENT_ERROR, ("permission denied", "connection refused", "credentials", "not installed")),
    )
    for classification, needles in patterns:
        if any(needle in text for needle in needles):
            return classification
    return FailureClass.UNKNOWN
