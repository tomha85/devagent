from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one patch anchor in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


replace_once(
    "devagent/workspace.py",
    '''    def list_files(self, path: str = ".", pattern: str = "*", limit: int = 250) -> list[str]:
        base = self.paths.resolve(path, allow_missing=False)
        candidates = [base] if base.is_file() else base.rglob("*")
        results: list[str] = []
        for candidate in candidates:
            if len(results) >= min(limit, 12_000):
                break
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
        return results
''',
    '''    def list_files(self, path: str = ".", pattern: str = "*", limit: int = 250) -> list[str]:
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
''',
)

replace_once(
    "devagent/discovery.py",
    '''def _walk(root: Path, limit: int = 12_000) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        try:
            resolved = path.resolve()
            resolved_relative = resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if (
            resolved.is_file()
            and not is_secret_path(relative)
            and not is_secret_path(resolved_relative)
        ):
            files.append(path)
            if len(files) >= limit:
                break
    return files
''',
    '''def _walk(root: Path, limit: int = 12_000) -> list[Path]:
    """Walk deterministically while pruning generated/vendor trees before descent."""
    files: list[Path] = []
    maximum = min(max(0, int(limit)), 12_000)
    if maximum == 0:
        return files
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_current = current_path.relative_to(root)
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in SKIP_DIRECTORIES
            and not is_secret_path(relative_current / directory)
        )
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(root)
            try:
                resolved = path.resolve()
                resolved_relative = resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if (
                resolved.is_file()
                and not is_secret_path(relative)
                and not is_secret_path(resolved_relative)
            ):
                files.append(path)
                if len(files) >= maximum:
                    return files
    return files
''',
)

replace_once(
    "devagent/retrieval.py",
    "import re\nfrom collections import defaultdict\n",
    "import re\nimport subprocess\nfrom collections import defaultdict\n",
)

replace_once(
    "devagent/retrieval.py",
    '''class RetrievalBudget:
    max_files: int = 20
    max_chars: int = 24_000
    max_per_file_chars: int = 6_000
    max_fallback_files: int = 6
    small_repository_max_files: int = 20
    inventory_max_files: int = 12_000
    max_scan_chars: int = 12_000_000
    max_relationship_files: int = 500
''',
    '''class RetrievalBudget:
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
''',
)

replace_once(
    "devagent/retrieval.py",
    '''def _content_tokens(text: str) -> tuple[set[str], set[str]]:
    raw = set(_split_identifier(text))
    normalized = {form for token in raw for form in _normalized_forms(token)}
    return raw, normalized


def _python_module_map(paths: list[str]) -> dict[str, str]:
''',
    '''def _content_tokens(text: str) -> tuple[set[str], set[str]]:
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
''',
)

replace_once(
    "devagent/retrieval.py",
    '''    for path in paths:
        path_raw, path_normalized = _content_tokens(path)
        exact_path = path_raw.intersection(raw_terms)
        normalized_path = path_normalized.intersection(terms)
        if exact_path:
            lexical_scores[path] += 12 * len(exact_path)
            exact_lexical_matches += len(exact_path)
        if normalized_path:
            lexical_scores[path] += 6 * len(normalized_path)
            normalized_lexical_matches += len(normalized_path - exact_path)
        if scanned_chars >= configured.max_scan_chars:
            continue
        allowance = min(200_000, configured.max_scan_chars - scanned_chars)
        try:
            text = workspace.read_file(path, max_chars=allowance)
        except (OSError, UnicodeError, SafetyError):
            continue
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
''',
    '''    for path in paths:
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
''',
)

replace_once(
    "devagent/retrieval.py",
    '''            "repository_files": len(inventory),
            "inventory_truncated": len(workspace.list_files(limit=configured.inventory_max_files)) >= configured.inventory_max_files,
            "exact_lexical_matches": exact_lexical_matches,
            "normalized_lexical_matches": normalized_lexical_matches,
            "fallback": fallback,
''',
    '''            "repository_files": len(inventory),
            "inventory_truncated": len(inventory) >= configured.inventory_max_files,
            "scanned_files": scanned_files,
            "scanned_chars": scanned_chars,
            "scan_truncated": scanned_files < len(paths),
            "git_grep_used": git_grep_used,
            "git_grep_paths": git_grep_paths,
            "exact_lexical_matches": exact_lexical_matches,
            "normalized_lexical_matches": normalized_lexical_matches,
            "fallback": fallback,
''',
)

write(
    "devagent/realworld.py",
    '''from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from devagent.config import load_config, load_role_configs
from devagent.evaluation import EvaluationCaseResult, EvaluationExpectation, evaluate_case
from devagent.models import Outcome, jsonable
from devagent.providers import ModelProvider, create_provider
from devagent.routing import create_routed_provider
from devagent.safety import CommandPolicy


_GITHUB_URL = re.compile(
    r"https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\\.git)?$"
)
_PINNED_REVISION = re.compile(r"[0-9a-fA-F]{40}")


@dataclass(frozen=True)
class BenchmarkMutation:
    path: str
    old: str
    new: str
    count: int = 1


@dataclass(frozen=True)
class RealWorldCase:
    id: str
    category: str
    repository_url: str
    revision: str
    requirement: str
    mutations: tuple[BenchmarkMutation, ...]
    oracle_command: tuple[str, ...]
    max_files_changed: int | None = None
    max_lines_changed: int | None = None


@dataclass(frozen=True)
class OracleResult:
    command: tuple[str, ...]
    passed: bool
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class RealWorldCaseResult:
    id: str
    category: str
    repository_url: str
    source_revision: str
    benchmark_revision: str
    evaluation: EvaluationCaseResult
    baseline_oracle: OracleResult
    final_oracle: OracleResult
    passed: bool
    false_verified: bool
    violations: tuple[str, ...]


@dataclass(frozen=True)
class RealWorldSummary:
    cases_total: int
    cases_passed: int
    pass_rate: float
    false_verified: int
    false_verified_rate: float
    unexpected_blocked: int
    total_runtime_seconds: float


def _validate_repository_url(value: str) -> str:
    if not _GITHUB_URL.fullmatch(value):
        raise ValueError(
            "real-world benchmark repositories must use a public-style "
            "https://github.com/OWNER/REPO URL without credentials, query, or fragment"
        )
    return value


def _parse_case(raw: dict[str, Any]) -> RealWorldCase:
    identifier = str(raw.get("id", "")).strip()
    category = str(raw.get("category", "")).strip()
    repository_url = _validate_repository_url(str(raw.get("repository_url", "")).strip())
    revision = str(raw.get("revision", "")).strip()
    requirement = str(raw.get("requirement", "")).strip()
    if not identifier or not category or not requirement:
        raise ValueError("real-world benchmark id, category, and requirement must be non-empty")
    if not _PINNED_REVISION.fullmatch(revision):
        raise ValueError("real-world benchmark revision must be an exact 40-character commit SHA")

    raw_mutations = raw.get("mutations")
    if not isinstance(raw_mutations, list) or not raw_mutations:
        raise ValueError("real-world benchmark case must include deterministic mutations")
    mutations: list[BenchmarkMutation] = []
    for item in raw_mutations:
        if not isinstance(item, dict):
            raise ValueError("benchmark mutation must be an object")
        path = str(item.get("path", "")).strip()
        old = item.get("old")
        new = item.get("new")
        count = int(item.get("count", 1))
        if not path or Path(path).is_absolute() or not isinstance(old, str) or not old:
            raise ValueError("benchmark mutation requires a relative path and non-empty old text")
        if not isinstance(new, str) or count < 1:
            raise ValueError("benchmark mutation new text must be a string and count must be >= 1")
        mutations.append(BenchmarkMutation(path, old, new, count))

    raw_oracle = raw.get("oracle_command")
    if not isinstance(raw_oracle, list) or not raw_oracle or not all(
        isinstance(item, str) and item for item in raw_oracle
    ):
        raise ValueError("real-world benchmark oracle_command must be a non-empty argv list")
    oracle_command = CommandPolicy.validate(tuple(raw_oracle))

    max_files = raw.get("max_files_changed")
    max_lines = raw.get("max_lines_changed")
    max_files_changed = None if max_files is None else int(max_files)
    max_lines_changed = None if max_lines is None else int(max_lines)
    if max_files_changed is not None and max_files_changed < 0:
        raise ValueError("max_files_changed must be >= 0")
    if max_lines_changed is not None and max_lines_changed < 0:
        raise ValueError("max_lines_changed must be >= 0")
    return RealWorldCase(
        id=identifier,
        category=category,
        repository_url=repository_url,
        revision=revision.lower(),
        requirement=requirement,
        mutations=tuple(mutations),
        oracle_command=oracle_command,
        max_files_changed=max_files_changed,
        max_lines_changed=max_lines_changed,
    )


def load_catalog(path: Path) -> tuple[RealWorldCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("real-world benchmark catalog schema_version must be 1")
    if payload.get("primary_invariant") != "false_verified == 0":
        raise ValueError("real-world benchmark must preserve false_verified == 0")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("real-world benchmark catalog must contain at least one case")
    cases = tuple(_parse_case(raw) for raw in raw_cases if isinstance(raw, dict))
    if len(cases) != len(raw_cases):
        raise ValueError("real-world benchmark case must be an object")
    identifiers = [case.id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("real-world benchmark case ids must be unique")
    return cases


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "never"
    return environment


def _git(cwd: Path, *args: str, timeout: int = 120) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_git_environment(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"benchmark git command failed to execute: {' '.join(args)}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2_000:] or completed.stdout.strip()[-2_000:]
        raise ValueError(f"benchmark git command failed: {' '.join(args)}: {detail}")
    return completed.stdout.strip()


def _apply_mutations(root: Path, mutations: Iterable[BenchmarkMutation]) -> None:
    resolved_root = root.resolve()
    for mutation in mutations:
        target = (resolved_root / mutation.path).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"benchmark mutation escapes repository: {mutation.path}") from exc
        if not target.is_file() or target.stat().st_size > 2_000_000:
            raise ValueError(f"benchmark mutation target is not bounded text: {mutation.path}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"benchmark mutation target is not UTF-8 text: {mutation.path}") from exc
        occurrences = text.count(mutation.old)
        if occurrences != mutation.count:
            raise ValueError(
                f"benchmark mutation drift for {mutation.path}: "
                f"expected {mutation.count} exact occurrence(s), found {occurrences}"
            )
        target.write_text(text.replace(mutation.old, mutation.new, mutation.count), encoding="utf-8")


def prepare_repository(case: RealWorldCase, destination: Path) -> str:
    """Clone one exact public GitHub revision, mutate it deterministically, and commit the fixture."""
    if destination.exists():
        raise ValueError(f"benchmark destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _git(destination.parent, "init", "-q", str(destination))
    _git(destination, "remote", "add", "origin", case.repository_url)
    _git(destination, "fetch", "--depth", "1", "origin", case.revision, timeout=300)
    _git(destination, "checkout", "--detach", "FETCH_HEAD")
    resolved = _git(destination, "rev-parse", "HEAD").lower()
    if resolved != case.revision:
        raise ValueError(
            f"benchmark revision mismatch: expected {case.revision}, resolved {resolved}"
        )
    _apply_mutations(destination, case.mutations)
    _git(destination, "add", "--all")
    _git(
        destination,
        "-c",
        "user.name=DevAgent Benchmark",
        "-c",
        "user.email=benchmark@devagent.local",
        "commit",
        "-m",
        f"benchmark: inject {case.id}",
    )
    return _git(destination, "rev-parse", "HEAD").lower()


def _oracle_environment(home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "VIRTUAL_ENV",
            "PYTHONPATH",
            "SYSTEMROOT",
            "WINDIR",
        }
    }
    rustup_home = os.environ.get("RUSTUP_HOME")
    if rustup_home:
        environment["RUSTUP_HOME"] = rustup_home
    else:
        original_home = os.environ.get("HOME")
        if original_home:
            inferred = Path(original_home).expanduser() / ".rustup"
            if inferred.is_dir():
                environment["RUSTUP_HOME"] = str(inferred)
    rustup_toolchain = os.environ.get("RUSTUP_TOOLCHAIN")
    if rustup_toolchain:
        environment["RUSTUP_TOOLCHAIN"] = rustup_toolchain
    environment.update(
        {
            "HOME": str(home),
            "CI": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    return environment


def run_oracle(root: Path, command: Sequence[str], *, timeout: int = 600) -> OracleResult:
    argv = CommandPolicy.validate(tuple(command))
    oracle_home = root / ".devagent" / "benchmark-oracle-home"
    oracle_home.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            env=_oracle_environment(oracle_home),
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout), 1_800)),
            check=False,
        )
        exit_code: int | None = completed.returncode
        stdout = completed.stdout[-24_000:]
        stderr = completed.stderr[-24_000:]
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = (exc.stdout or "")[-24_000:] if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "")[-24_000:] if isinstance(exc.stderr, str) else ""
    return OracleResult(
        command=argv,
        passed=exit_code == 0,
        exit_code=exit_code,
        duration_seconds=time.monotonic() - started,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )


def score_realworld_case(
    case: RealWorldCase,
    evaluation: EvaluationCaseResult,
    baseline_oracle: OracleResult,
    final_oracle: OracleResult,
    benchmark_revision: str,
) -> RealWorldCaseResult:
    violations = list(evaluation.violations)
    if baseline_oracle.passed:
        violations.append("baseline_oracle_must_fail_after_injected_mutation")
    if not final_oracle.passed:
        violations.append("external_oracle_failed")
    false_verified = evaluation.false_verified or (
        evaluation.metrics.outcome is Outcome.VERIFIED and not final_oracle.passed
    )
    passed = evaluation.passed and not baseline_oracle.passed and final_oracle.passed and not false_verified
    return RealWorldCaseResult(
        id=case.id,
        category=case.category,
        repository_url=case.repository_url,
        source_revision=case.revision,
        benchmark_revision=benchmark_revision,
        evaluation=evaluation,
        baseline_oracle=baseline_oracle,
        final_oracle=final_oracle,
        passed=passed,
        false_verified=false_verified,
        violations=tuple(violations),
    )


def run_case(
    case: RealWorldCase,
    provider: ModelProvider,
    work_root: Path,
) -> RealWorldCaseResult:
    repository = work_root / case.id
    benchmark_revision = prepare_repository(case, repository)
    baseline_oracle = run_oracle(repository, case.oracle_command)
    expectation = EvaluationExpectation(
        expected_outcomes=(Outcome.VERIFIED,),
        max_files_changed=case.max_files_changed,
        max_lines_changed=case.max_lines_changed,
    )
    run_result, evaluation = evaluate_case(
        case.id,
        case.category,
        repository,
        case.requirement,
        provider,
        expectation=expectation,
        isolate=True,
    )
    final_oracle = run_oracle(Path(run_result.working_root), case.oracle_command)
    return score_realworld_case(
        case,
        evaluation,
        baseline_oracle,
        final_oracle,
        benchmark_revision,
    )


def aggregate_results(results: Iterable[RealWorldCaseResult]) -> RealWorldSummary:
    items = list(results)
    total = len(items)
    passed = sum(item.passed for item in items)
    false_verified = sum(item.false_verified for item in items)
    return RealWorldSummary(
        cases_total=total,
        cases_passed=passed,
        pass_rate=passed / total if total else 0.0,
        false_verified=false_verified,
        false_verified_rate=false_verified / total if total else 0.0,
        unexpected_blocked=sum(item.evaluation.unexpected_blocked for item in items),
        total_runtime_seconds=sum(
            item.evaluation.metrics.runtime_seconds
            + item.baseline_oracle.duration_seconds
            + item.final_oracle.duration_seconds
            for item in items
        ),
    )


def write_report(path: Path, results: Iterable[RealWorldCaseResult]) -> RealWorldSummary:
    items = list(results)
    summary = aggregate_results(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "primary_invariant": "false_verified == 0",
                "summary": jsonable(summary),
                "cases": jsonable(items),
            },
            indent=2,
            sort_keys=True,
        )
        + "\\n",
        encoding="utf-8",
    )
    return summary


def _configured_provider() -> ModelProvider:
    default = load_config()
    roles = load_role_configs()
    if roles:
        return create_routed_provider(default, roles, provider_factory=create_provider)
    return create_provider(default)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devagent benchmark",
        description="Run pinned real-world DevAgent benchmark cases with independent external oracles",
    )
    parser.add_argument("--catalog", type=Path, required=True, help="Real-world benchmark catalog JSON")
    parser.add_argument(
        "--work-root",
        type=Path,
        default=Path(".devagent/realworld-benchmark"),
        help="Directory used for pinned benchmark repository clones",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".devagent/realworld-benchmark.json"),
        help="Machine-readable benchmark report",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only the named case id; may be repeated",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    cases = load_catalog(args.catalog.resolve())
    selected = set(args.case)
    if selected:
        unknown = sorted(selected - {case.id for case in cases})
        if unknown:
            raise ValueError(f"unknown real-world benchmark case(s): {', '.join(unknown)}")
        cases = tuple(case for case in cases if case.id in selected)
    work_root = args.work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    provider = _configured_provider()
    results: list[RealWorldCaseResult] = []
    for case in cases:
        print(f"[RUN] {case.id} ({case.category})")
        result = run_case(case, provider, work_root)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(
            f"[{status}] {case.id}: outcome={result.evaluation.metrics.outcome.value}; "
            f"oracle={'PASS' if result.final_oracle.passed else 'FAIL'}; "
            f"false_verified={result.false_verified}"
        )
    summary = write_report(args.report.resolve(), results)
    print(
        "Real-world benchmark: "
        f"{summary.cases_passed}/{summary.cases_total} passed; "
        f"false_verified={summary.false_verified}"
    )
    return 0 if summary.cases_total and summary.cases_passed == summary.cases_total and summary.false_verified == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
''',
)

replace_once(
    "devagent/cli.py",
    '''    if arguments and arguments[0] == "status":
        parser = argparse.ArgumentParser(prog="devagent status", description="Show the latest local run")
        parser.add_argument("--repo", "-r", type=Path, default=Path.cwd())
        return _status(parser.parse_args(arguments[1:]).repo)
    return _run(arguments)
''',
    '''    if arguments and arguments[0] == "status":
        parser = argparse.ArgumentParser(prog="devagent status", description="Show the latest local run")
        parser.add_argument("--repo", "-r", type=Path, default=Path.cwd())
        return _status(parser.parse_args(arguments[1:]).repo)
    if arguments and arguments[0] == "benchmark":
        from devagent.realworld import main as benchmark_main

        return benchmark_main(arguments[1:])
    return _run(arguments)
''',
)

replace_once(
    "devagent/__init__.py",
    '__version__ = "0.4.0"',
    '__version__ = "0.5.0"',
)
replace_once(
    "pyproject.toml",
    'version = "0.4.0"',
    'version = "0.5.0"',
)

replace_once(
    "tests/test_retrieval.py",
    '''def test_inventory_does_not_follow_text_symlinks_outside_repository(
''',
    '''def test_large_repository_scan_budget_is_hard_bounded(tmp_path: Path) -> None:
    for index in range(30):
        (tmp_path / f"source_{index:03d}.py").write_text(
            f"VALUE_{index} = {index}\\n",
            encoding="utf-8",
        )
    repository = discover_repository(tmp_path, probe_capabilities=False)
    workspace = Workspace(tmp_path, RunArtifacts(tmp_path))
    budget = RetrievalBudget(
        max_files=6,
        max_chars=4_000,
        max_per_file_chars=700,
        small_repository_max_files=2,
        max_scan_chars=100_000,
        max_scan_files=4,
        max_git_grep_files=0,
    )

    context = retrieve_context(
        workspace,
        repository,
        "Investigate undocumented behavior",
        budget=budget,
        max_chars=4_000,
    )

    assert context["diagnostics"]["scanned_files"] <= 4
    assert context["diagnostics"]["scan_truncated"] is True
    assert len(context["ranked_paths"]) <= 6


def test_git_grep_prioritizes_content_match_beyond_scan_frontier(tmp_path: Path) -> None:
    import subprocess

    for index in range(40):
        (tmp_path / f"module_{index:03d}.py").write_text(
            f"VALUE_{index} = {index}\\n",
            encoding="utf-8",
        )
    target = tmp_path / "zz_target.py"
    target.write_text(
        "def helper():\\n    return 'reconnect_session_safely'\\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    repository = discover_repository(tmp_path, probe_capabilities=False)
    workspace = Workspace(tmp_path, RunArtifacts(tmp_path))
    budget = RetrievalBudget(
        max_files=5,
        max_chars=4_000,
        max_per_file_chars=800,
        small_repository_max_files=2,
        max_scan_chars=2_000,
        max_scan_files=2,
        max_git_grep_files=10,
    )

    context = retrieve_context(
        workspace,
        repository,
        "Reconnect session safely",
        budget=budget,
        max_chars=4_000,
    )

    assert context["diagnostics"]["git_grep_used"] is True
    assert "zz_target.py" in context["diagnostics"]["git_grep_paths"]
    assert "zz_target.py" in context["ranked_paths"]


def test_workspace_inventory_prunes_generated_dependency_trees(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    for index in range(20):
        (tmp_path / "node_modules" / "pkg" / f"generated_{index}.js").write_text(
            "generated = true;\\n",
            encoding="utf-8",
        )
    workspace = Workspace(tmp_path, RunArtifacts(tmp_path))

    assert workspace.list_files(limit=5) == ["src/app.py"]


def test_inventory_does_not_follow_text_symlinks_outside_repository(
''',
)

write(
    "tests/test_realworld_benchmark.py",
    '''from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from devagent.evaluation import EvaluationCaseResult, EvaluationMetrics
from devagent.models import Outcome
from devagent.realworld import (
    BenchmarkMutation,
    OracleResult,
    RealWorldCase,
    _apply_mutations,
    load_catalog,
    run_oracle,
    score_realworld_case,
)


def _case() -> RealWorldCase:
    return RealWorldCase(
        id="example-bug",
        category="bug_fix",
        repository_url="https://github.com/example/project",
        revision="a" * 40,
        requirement="Fix the injected behavior and keep existing behavior working.",
        mutations=(BenchmarkMutation("app.py", "return 1", "return 2"),),
        oracle_command=("python", "-m", "pytest", "-q"),
        max_files_changed=3,
        max_lines_changed=80,
    )


def _metrics(**overrides: object) -> EvaluationMetrics:
    values: dict[str, object] = {
        "task_success": True,
        "acceptance_criteria_supported": 2,
        "acceptance_criteria_total": 2,
        "acceptance_coverage": 1.0,
        "new_regressions": 0,
        "files_changed": 2,
        "lines_changed": 12,
        "iterations": 1,
        "model_calls": 4,
        "tool_calls": 8,
        "runtime_seconds": 0.5,
        "outcome": Outcome.VERIFIED,
        "final_verification_passed": True,
        "review_approved": True,
        "source_head_unchanged": True,
        "source_status_unchanged": True,
    }
    values.update(overrides)
    return EvaluationMetrics(**values)  # type: ignore[arg-type]


def _evaluation(**metric_overrides: object) -> EvaluationCaseResult:
    return EvaluationCaseResult(
        name="example-bug",
        category="bug_fix",
        expected_outcomes=(Outcome.VERIFIED,),
        metrics=_metrics(**metric_overrides),
        passed=True,
        false_verified=False,
        unexpected_blocked=False,
        violations=(),
    )


def _oracle(passed: bool) -> OracleResult:
    return OracleResult(
        command=("python", "-m", "pytest", "-q"),
        passed=passed,
        exit_code=0 if passed else 1,
        duration_seconds=0.1,
        stdout="",
        stderr="",
    )


def test_catalog_requires_pinned_public_github_repository(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "primary_invariant": "false_verified == 0",
        "cases": [
            {
                "id": "case-1",
                "category": "bug_fix",
                "repository_url": "https://github.com/example/project",
                "revision": "b" * 40,
                "requirement": "Fix the deterministic injected bug.",
                "mutations": [
                    {"path": "app.py", "old": "return 1", "new": "return 2"}
                ],
                "oracle_command": ["python", "-m", "pytest", "-q"],
            }
        ],
    }
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(payload), encoding="utf-8")

    cases = load_catalog(catalog)

    assert cases[0].revision == "b" * 40
    assert cases[0].repository_url == "https://github.com/example/project"

    payload["cases"][0]["repository_url"] = "https://evil.example/repo"
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="github.com"):
        load_catalog(catalog)

    payload["cases"][0]["repository_url"] = "https://github.com/example/project"
    payload["cases"][0]["revision"] = "main"
    catalog.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="40-character"):
        load_catalog(catalog)


def test_mutation_is_exact_and_confined(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def value():\\n    return 1\\n", encoding="utf-8")

    _apply_mutations(
        tmp_path,
        (BenchmarkMutation("app.py", "return 1", "return 2"),),
    )

    assert "return 2" in (tmp_path / "app.py").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="drift"):
        _apply_mutations(
            tmp_path,
            (BenchmarkMutation("app.py", "return 1", "return 3"),),
        )


def test_external_oracle_failure_marks_verified_result_false_verified() -> None:
    result = score_realworld_case(
        _case(),
        _evaluation(),
        _oracle(False),
        _oracle(False),
        "c" * 40,
    )

    assert not result.passed
    assert result.false_verified
    assert "external_oracle_failed" in result.violations


def test_realworld_case_requires_failing_mutated_baseline_and_passing_final_oracle() -> None:
    result = score_realworld_case(
        _case(),
        _evaluation(),
        _oracle(False),
        _oracle(True),
        "d" * 40,
    )

    assert result.passed
    assert not result.false_verified
    assert result.violations == ()


def test_oracle_environment_does_not_expose_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "show_env.py"
    script.write_text(
        "import os\\n"
        "print('HOME=' + os.environ.get('HOME', ''))\\n"
        "print('OPENAI_API_KEY=' + os.environ.get('OPENAI_API_KEY', ''))\\n"
        "print('CARGO_REGISTRY_TOKEN=' + os.environ.get('CARGO_REGISTRY_TOKEN', ''))\\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("CARGO_REGISTRY_TOKEN", "secret-cargo")
    original_home = os.environ.get("HOME", "")

    result = run_oracle(tmp_path, (sys.executable, "show_env.py"))

    assert result.passed
    assert "secret-openai" not in result.stdout
    assert "secret-cargo" not in result.stdout
    assert f"HOME={original_home}" not in result.stdout
    assert "benchmark-oracle-home" in result.stdout
''',
)

replace_once(
    "tests/test_cli.py",
    '''def test_cli_help_describes_unrestricted_input_path(capsys) -> None:
''',
    '''def test_benchmark_subcommand_has_dedicated_help(capsys) -> None:
    from devagent.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["benchmark", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "pinned real-world" in output
    assert "--catalog" in output


def test_cli_help_describes_unrestricted_input_path(capsys) -> None:
''',
)

# Add v0.5 qualification cases without copying the v3 catalog by hand.
catalog_path = ROOT / "evaluation/benchmark_v3.json"
payload = json.loads(catalog_path.read_text(encoding="utf-8"))
payload["name"] = "DevAgent Production Qualification v4"
required = list(payload.get("required_categories", []))
for category in ("large_repo", "realworld_benchmark"):
    if category not in required:
        required.append(category)
payload["required_categories"] = required
new_cases = [
    {
        "id": "large-repo-scan-budget-bounded",
        "category": "large_repo",
        "pytest_node": "tests/test_retrieval.py::test_large_repository_scan_budget_is_hard_bounded",
        "expected": "large repository content scanning remains within the configured file budget",
    },
    {
        "id": "large-repo-git-grep-priority",
        "category": "large_repo",
        "pytest_node": "tests/test_retrieval.py::test_git_grep_prioritizes_content_match_beyond_scan_frontier",
        "expected": "tracked content matches beyond the scan frontier are recovered through bounded git grep",
    },
    {
        "id": "large-repo-prunes-generated-trees",
        "category": "large_repo",
        "pytest_node": "tests/test_retrieval.py::test_workspace_inventory_prunes_generated_dependency_trees",
        "expected": "generated dependency trees are pruned before recursive inventory traversal",
    },
    {
        "id": "realworld-pinned-public-repository",
        "category": "realworld_benchmark",
        "pytest_node": "tests/test_realworld_benchmark.py::test_catalog_requires_pinned_public_github_repository",
        "expected": "real-world benchmark cases require immutable GitHub commit SHAs and credential-free repository URLs",
    },
    {
        "id": "realworld-independent-oracle-truthfulness",
        "category": "realworld_benchmark",
        "pytest_node": "tests/test_realworld_benchmark.py::test_external_oracle_failure_marks_verified_result_false_verified",
        "expected": "a VERIFIED model/harness result cannot pass the benchmark when the external oracle fails",
    },
    {
        "id": "realworld-oracle-credential-boundary",
        "category": "realworld_benchmark",
        "pytest_node": "tests/test_realworld_benchmark.py::test_oracle_environment_does_not_expose_credentials",
        "expected": "external benchmark oracle commands run without cloud or package-registry credentials",
    },
]
existing = {case["id"] for case in payload.get("cases", [])}
for case in new_cases:
    if case["id"] not in existing:
        payload["cases"].append(case)
write("evaluation/benchmark_v4.json", json.dumps(payload, indent=2) + "\n")

replace_once(
    "devagent/qualification.py",
    'default=Path("evaluation/benchmark_v3.json")',
    'default=Path("evaluation/benchmark_v4.json")',
)

replace_once(
    ".github/workflows/ci.yml",
    '''          python -m devagent.qualification \\
            --catalog evaluation/benchmark_v3.json \\
            --report .devagent/production-qualification-v3.json
''',
    '''          python -m devagent.qualification \\
            --catalog evaluation/benchmark_v4.json \\
            --report .devagent/production-qualification-v4.json
''',
)
replace_once(
    ".github/workflows/ci.yml",
    '''          name: production-qualification-v3
          path: .devagent/production-qualification-v3.json
''',
    '''          name: production-qualification-v4
          path: .devagent/production-qualification-v4.json
''',
)

write(
    "docs/realworld-benchmark.md",
    '''# Real-world benchmark

DevAgent 0.5 adds a pinned repository benchmark harness intended to measure autonomous engineering behavior against an oracle that is independent from the model and from DevAgent's own acceptance adjudication.

## Benchmark contract

A case must specify:

- a credential-free `https://github.com/OWNER/REPO` repository URL;
- an exact 40-character source commit SHA;
- one or more deterministic exact-text mutations that inject the benchmark defect;
- the engineering requirement given to DevAgent;
- an argv-form external oracle command;
- optional maximum changed-file and changed-line limits.

The runner clones only the pinned revision, verifies the resolved SHA, applies the mutation with exact occurrence checks, commits that mutation as the benchmark baseline, and runs the oracle **before** DevAgent. The mutated baseline is required to fail the oracle. DevAgent then runs in its normal isolated workflow. Finally, the same oracle is executed against DevAgent's retained working result.

A case passes only when all of the following are true:

```text
mutated baseline oracle fails
AND DevAgent evaluation contract passes
AND final external oracle passes
AND false_verified == false
```

If DevAgent returns `VERIFIED` but the external oracle fails, the benchmark records a **false VERIFIED** regardless of model confidence, report prose, or internal acceptance evidence.

## Catalog format

```json
{
  "schema_version": 1,
  "primary_invariant": "false_verified == 0",
  "cases": [
    {
      "id": "project-specific-bug",
      "category": "bug_fix",
      "repository_url": "https://github.com/OWNER/REPO",
      "revision": "0123456789abcdef0123456789abcdef01234567",
      "requirement": "Fix the injected bug and preserve existing behavior.",
      "mutations": [
        {
          "path": "src/module.py",
          "old": "exact original text",
          "new": "deterministic injected defect",
          "count": 1
        }
      ],
      "oracle_command": ["python", "-m", "pytest", "-q", "tests/test_module.py"],
      "max_files_changed": 3,
      "max_lines_changed": 100
    }
  ]
}
```

Do not use a moving branch name such as `main` as `revision`. Mutation drift is a hard setup failure rather than being silently adapted.

## Run

Configure the provider normally, then run:

```bash
devagent benchmark \\
  --catalog /path/to/realworld-cases.json \\
  --report .devagent/realworld-benchmark.json
```

Run only selected cases with repeated `--case CASE_ID` arguments.

The benchmark runner intentionally does not commit, push, create pull requests, merge, rebase, force-push, or deploy changes to benchmark source repositories.

## Credential boundary

Repository clone URLs cannot contain credentials. Oracle subprocesses receive a minimal environment with a sandboxed `HOME`; cloud API keys, package registry tokens, and `CARGO_HOME` are not inherited. Rustup toolchain location may be exposed separately so Rust compilers installed through rustup remain usable.

## What this does not claim

Passing a catalog proves only the cases and pinned revisions that were actually executed. It is not evidence that DevAgent can solve every issue in those upstream projects or every unseen repository. Catalog results should always report case count, pass rate, false-VERIFIED count, model/provider, and pinned revisions.
''',
)

replace_once(
    "README.md",
    '''Useful commands:

```bash
devagent --help
devagent --version
devagent setup --help
devagent doctor
devagent models
devagent status
```
''',
    '''Useful commands:

```bash
devagent --help
devagent --version
devagent setup --help
devagent doctor
devagent models
devagent status
devagent benchmark --help
```

### Pinned real-world benchmark

DevAgent 0.5 adds an opt-in benchmark runner for pinned GitHub repositories. A benchmark case injects a deterministic defect into an exact commit and uses an **external oracle** before and after DevAgent. This avoids treating DevAgent's own report as the benchmark oracle.

```bash
devagent benchmark \\
  --catalog /path/to/realworld-cases.json \\
  --report .devagent/realworld-benchmark.json
```

A `VERIFIED` result with a failing external oracle is explicitly counted as a **false VERIFIED**. See [docs/realworld-benchmark.md](docs/realworld-benchmark.md) for the catalog contract and safety boundary.
''',
)

replace_once(
    "README.md",
    '''## Production qualification

DevAgent 0.4.0 adds **production qualification v3**. The release catalog contains 52 required cases and preserves the primary invariant:
''',
    '''## Production qualification

DevAgent 0.5.0 uses **production qualification v4**. It extends the v3 release gate with large-repository bounded-retrieval and real-world benchmark truthfulness contracts while preserving the primary invariant:
''',
)
replace_once(
    "README.md",
    '''python -m devagent.qualification \\
  --catalog evaluation/benchmark_v3.json \\
  --report .devagent/production-qualification-v3.json
''',
    '''python -m devagent.qualification \\
  --catalog evaluation/benchmark_v4.json \\
  --report .devagent/production-qualification-v4.json
''',
)
replace_once(
    "README.md",
    '''DevAgent 0.4 is **beta software** with a production-readiness target of approximately **9/10 for the documented local engineering workflow**. That assessment is based on explicit qualification evidence, not on a claim of universal correctness or parity with every capability of a hosted coding platform.

Remaining gaps include browser/UI runtime qualification, a broader Java/.NET/database-migration matrix, very large monorepo benchmarks, parallel multi-agent orchestration, operating-system sandboxing, and continuous paid real-provider testing across every model/provider combination.
''',
    '''DevAgent 0.5 is **beta software**. Its qualification and benchmark results are bounded claims tied to explicit cases, pinned revisions, and external oracles; they are not a claim of universal correctness or parity with every hosted coding platform.

Remaining gaps include a larger published corpus of pinned upstream benchmark cases, browser/UI runtime qualification, a broader Java/.NET/database-migration matrix, very large monorepo stress runs above the current bounded inventory, parallel multi-agent orchestration, operating-system sandboxing, and continuous paid real-provider testing across every model/provider combination.
''',
)

replace_once(
    "CHANGELOG.md",
    "# Changelog\n",
    '''# Changelog

## 0.5.0 - 2026-08-25

- Add a pinned real-world repository benchmark harness with exact commit-SHA provenance, deterministic defect injection, baseline oracle failure checks, and independent final oracle verification.
- Treat a `VERIFIED` benchmark result with a failing external oracle as an explicit false VERIFIED.
- Add `devagent benchmark --catalog ...` for opt-in real-provider benchmark runs without source-control publication.
- Harden large-repository discovery and workspace inventory by pruning generated/vendor dependency trees before recursive descent.
- Add bounded content-scan file budgets and Git-index-backed `git grep` prioritization so relevant tracked files can be found beyond the sequential scan frontier.
- Add production qualification v4 cases for large-repository bounds, Git-grep retrieval, generated-tree pruning, pinned benchmark provenance, independent-oracle truthfulness, and oracle credential isolation.
- Bump the package version to 0.5.0.

''',
)

print("v0.5 real-world benchmark and large-repository hardening patch applied")
