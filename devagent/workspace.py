from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Sequence

from devagent.artifacts import RunArtifacts
from devagent.models import FailureClass, VerificationResult
from devagent.runtime import NetworkMode, RuntimeExecutor, RuntimePolicy, RuntimePolicyError
from devagent.safety import CommandPolicy, PathPolicy, SKIP_DIRECTORIES, SafetyError, is_secret_path


class Workspace:
    def __init__(self, root: Path | str, artifacts: RunArtifacts, dirty_files: Sequence[str] = ()) -> None:
        self.root = Path(root).resolve()
        self.paths = PathPolicy(self.root)
        self.artifacts = artifacts
        self.dirty_files = frozenset(dirty_files)
        self.revision = 0
        self.modified_paths: set[str] = set()
        try:
            self.runtime_policy = RuntimePolicy.from_environment()
        except RuntimePolicyError as exc:
            raise SafetyError(str(exc)) from exc
        self.runtime = RuntimeExecutor(self.root, self.runtime_policy)
        self.artifacts.record("runtime_policy", **self.runtime.status())

    def _relative(self, target: Path) -> str:
        return target.relative_to(self.root).as_posix()

    def _writable(self, relative: str) -> Path:
        target = self.paths.resolve(relative)
        normalized = self._relative(target)
        if normalized in self.dirty_files:
            raise SafetyError(f"Refusing to overwrite pre-existing developer modification: {normalized}")
        return target

    def list_files(self, path: str = ".", pattern: str = "*", limit: int = 250) -> list[str]:
        """Return a deterministic bounded file inventory without descending into skipped trees."""
        base = self.paths.resolve(path, allow_missing=False)
        maximum = min(max(0, int(limit)), 12_000)
        if maximum == 0:
            return []
        candidates: list[Path] = []
        if base.is_file():
            candidates.append(base)
        else:
            for current, directories, filenames in os.walk(base, topdown=True, followlinks=False):
                current_path = Path(current)
                relative_current = current_path.relative_to(self.root)
                directories[:] = sorted(
                    directory
                    for directory in directories
                    if directory not in SKIP_DIRECTORIES
                    and not is_secret_path(relative_current / directory)
                )
                for filename in sorted(filenames):
                    candidates.append(current_path / filename)
                    if len(candidates) >= maximum:
                        break
                if len(candidates) >= maximum:
                    break

        results: list[str] = []
        for candidate in candidates:
            relative = candidate.relative_to(self.root)
            try:
                resolved = candidate.resolve()
                resolved_relative = resolved.relative_to(self.root)
            except (OSError, ValueError):
                continue
            if (
                not resolved.is_file()
                or any(part in SKIP_DIRECTORIES for part in relative.parts)
                or is_secret_path(relative)
                or is_secret_path(resolved_relative)
            ):
                continue
            rendered = relative.as_posix()
            if fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(rendered, pattern):
                results.append(rendered)
                if len(results) >= maximum:
                    break
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
            try:
                target = self.paths.resolve(relative, allow_missing=False)
            except SafetyError:
                continue
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

    def _structural_file(self, path: str) -> Path:
        lexical = self.root / path
        if lexical.is_symlink():
            raise SafetyError(f"Structural file operations do not follow symlinks: {path}")
        target = self._writable(path)
        if not target.exists() or not target.is_file():
            raise SafetyError(f"Structural operation requires an existing regular file: {path}")
        return target

    def delete_file(self, path: str) -> None:
        """Delete one planned regular file after preserving a run-local backup."""
        target = self._structural_file(path)
        relative = self._relative(target)
        self.artifacts.backup(target, relative_to=self.root)
        target.unlink()
        self.revision += 1
        self.modified_paths.add(relative)
        self.artifacts.record("file_deleted", path=relative, revision=self.revision)

    def move_file(self, source: str, destination: str, *, operation: str = "move") -> None:
        """Move/rename one planned regular file without overwriting existing content."""
        source_target = self._structural_file(source)
        destination_lexical = self.root / destination
        if destination_lexical.is_symlink():
            raise SafetyError(f"Structural destination cannot be a symlink: {destination}")
        destination_target = self._writable(destination)
        source_relative = self._relative(source_target)
        destination_relative = self._relative(destination_target)
        if source_relative == destination_relative:
            raise SafetyError("Structural source and destination must be different")
        if destination_target.exists():
            raise SafetyError(f"Refusing to overwrite structural destination: {destination_relative}")
        self.artifacts.backup(source_target, relative_to=self.root)
        destination_target.parent.mkdir(parents=True, exist_ok=True)
        source_target.replace(destination_target)
        self.revision += 1
        self.modified_paths.update({source_relative, destination_relative})
        self.artifacts.record(
            "file_moved",
            source=source_relative,
            destination=destination_relative,
            operation=operation,
            revision=self.revision,
        )

    def rename_file(self, source: str, destination: str) -> None:
        self.move_file(source, destination, operation="rename")

    def _validate_python_requirement_file(self, target: Path) -> None:
        if target.stat().st_size > 1_000_000:
            raise SafetyError(f"Dependency requirement file is too large: {self._relative(target)}")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise SafetyError("Dependency requirement file must be UTF-8 text") from exc

        blocked_prefixes = (
            "-e ",
            "--editable ",
            "-r ",
            "--requirement ",
            "-c ",
            "--constraint ",
            "--index-url",
            "--extra-index-url",
            "--trusted-host",
            "--find-links",
            "--no-binary",
        )
        blocked_fragments = (
            "http://",
            "https://",
            "ftp://",
            "file://",
            "git+",
            "hg+",
            "svn+",
            "bzr+",
            "ssh://",
        )
        for line_number, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            lowered = line.casefold()
            if lowered.startswith(blocked_prefixes):
                raise SafetyError(
                    f"Unsafe dependency directive in {self._relative(target)}:{line_number}"
                )
            if any(fragment in lowered for fragment in blocked_fragments) or " @ " in lowered:
                raise SafetyError(
                    f"Direct dependency source is blocked in {self._relative(target)}:{line_number}"
                )
            if line.startswith(("/", "./", "../", "~")):
                raise SafetyError(
                    f"Local dependency path is blocked in {self._relative(target)}:{line_number}"
                )

    def _validate_dependency_files(self, argv: tuple[str, ...]) -> None:
        executable = Path(argv[0]).name.lower()
        if executable in {"python", "python3"}:
            args = argv[4:]
        elif executable in {"pip", "pip3"}:
            args = argv[2:]
        else:
            args = argv[2:]

        if executable in {"python", "python3", "pip", "pip3"}:
            for index, token in enumerate(args):
                if token.lower() in {"-r", "--requirement"}:
                    if index + 1 >= len(args):
                        raise SafetyError("Dependency requirement file is missing")
                    target = self.paths.resolve(args[index + 1], allow_missing=False)
                    if not target.is_file():
                        raise SafetyError(f"Dependency requirement is not a file: {args[index + 1]}")
                    self._validate_python_requirement_file(target)
                    return
            raise SafetyError("Safe pip installation requires a repository requirement file")

        candidates: tuple[str, ...]
        if executable == "npm":
            candidates = ("package-lock.json", "npm-shrinkwrap.json")
        elif executable == "pnpm":
            candidates = ("pnpm-lock.yaml",)
        elif executable == "yarn":
            candidates = ("yarn.lock",)
        else:
            raise SafetyError(f"Unsupported dependency installer: {executable}")
        if not any((self.root / name).is_file() for name in candidates):
            raise SafetyError(
                f"Safe {executable} installation requires a committed lockfile: {', '.join(candidates)}"
            )

    def run(self, command: str | Sequence[str], *, timeout: int = 300, phase: str, baseline: bool = False) -> VerificationResult:
        parsed = CommandPolicy.parse(command)
        dependency_install = CommandPolicy.is_dependency_install(parsed)
        argv = CommandPolicy.validate(
            parsed,
            allow_dependency_install=self.runtime_policy.allow_dependency_install,
        )
        if dependency_install:
            if self.runtime_policy.network is not NetworkMode.INHERIT:
                raise SafetyError(
                    "Dependency installation requires explicit DEVAGENT_NETWORK=inherit in addition to "
                    "DEVAGENT_ALLOW_DEPENDENCY_INSTALL=1"
                )
            self._validate_dependency_files(argv)

        timeout = max(1, min(int(timeout), 1800))
        started = time.monotonic()
        timed_out = False
        sandbox_home = self.artifacts.root / "command-home"
        sandbox_home.mkdir(exist_ok=True)
        sandbox_tmp = sandbox_home / "tmp"
        sandbox_tmp.mkdir(exist_ok=True)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "TERM", "VIRTUAL_ENV", "PYTHONPATH", "SYSTEMROOT", "WINDIR"}
        }
        rustup_home = os.environ.get("RUSTUP_HOME")
        if rustup_home:
            environment["RUSTUP_HOME"] = rustup_home
        else:
            original_home = os.environ.get("HOME")
            if original_home:
                inferred_rustup = Path(original_home).expanduser() / ".rustup"
                if inferred_rustup.is_dir():
                    environment["RUSTUP_HOME"] = str(inferred_rustup)
        rustup_toolchain = os.environ.get("RUSTUP_TOOLCHAIN")
        if rustup_toolchain:
            environment["RUSTUP_TOOLCHAIN"] = rustup_toolchain
        environment.update(
            {
                "HOME": str(sandbox_home),
                "TMPDIR": str(sandbox_tmp),
                "CI": "true",
                "DEVAGENT_RUN_ID": self.artifacts.run_id,
                "DEVAGENT_RUNTIME_BACKEND": self.runtime.backend,
                "DEVAGENT_NETWORK_MODE": self.runtime_policy.network.value,
            }
        )
        if dependency_install and Path(argv[0]).name.lower() == "yarn":
            environment["YARN_IGNORE_SCRIPTS"] = "true"
            environment["YARN_ENABLE_SCRIPTS"] = "false"
        try:
            try:
                execution_argv = self.runtime.prepare(argv, writable_paths=(sandbox_home,))
            except RuntimePolicyError as exc:
                raise SafetyError(str(exc)) from exc
            completed = subprocess.run(
                execution_argv, cwd=self.root, env=environment, capture_output=True, text=True, timeout=timeout, check=False
            )
            exit_code: int | None = completed.returncode
            stdout, stderr = completed.stdout[-24_000:], completed.stderr[-24_000:]
            runtime_failure = self.runtime.infrastructure_failure(stderr) if exit_code != 0 else None
            if runtime_failure:
                self.artifacts.record(
                    "runtime_blocked",
                    backend=self.runtime.backend,
                    network=self.runtime_policy.network.value,
                    error=runtime_failure,
                )
                raise SafetyError(runtime_failure)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = (exc.stdout or "")[-24_000:] if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "")[-24_000:] if isinstance(exc.stderr, str) else ""
        duration = time.monotonic() - started
        classification = None if exit_code == 0 else classify_failure(stdout, stderr, timed_out)
        tests_run, tests_passed = _test_counts(stdout, stderr, exit_code)
        result = VerificationResult(
            command=argv,
            exit_code=exit_code,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            classification=classification,
            revision=self.revision,
            phase=phase,
            timed_out=timed_out,
            baseline=baseline,
            tests_run=tests_run,
            tests_passed=tests_passed,
        )
        self.artifacts.record(
            "command_finished",
            result=result,
            runtime_backend=self.runtime.backend,
            network=self.runtime_policy.network.value,
            dependency_install=dependency_install,
        )
        return result


def _test_counts(stdout: str, stderr: str, exit_code: int | None) -> tuple[int | None, int | None]:
    text = f"{stdout}\n{stderr}"
    summary = "\n".join(text.splitlines()[-8:])
    counts: dict[str, int] = {}
    for value, status in re.findall(
        r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)(?:\b|$)", summary, re.IGNORECASE
    ):
        normalized = "error" if status.casefold().startswith("error") else status.casefold()
        counts[normalized] = max(counts.get(normalized, 0), int(value))
    if counts:
        tests_run = sum(counts.get(name, 0) for name in ("passed", "failed", "error", "skipped", "xfailed", "xpassed"))
        return tests_run, counts.get("passed", 0)
    unittest_match = re.search(r"Ran\s+(\d+)\s+tests?", text)
    if unittest_match:
        tests_run = int(unittest_match.group(1))
        return tests_run, tests_run if exit_code == 0 else None
    return None, None


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
        (
            FailureClass.ENVIRONMENT_ERROR,
            ("permission denied", "operation not permitted", "connection refused", "credentials", "not installed"),
        ),
    )
    for classification, needles in patterns:
        if any(needle in text for needle in needles):
            return classification
    return FailureClass.UNKNOWN
