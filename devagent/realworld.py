from __future__ import annotations

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
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$"
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
        + "\n",
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
