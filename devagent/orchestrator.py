from __future__ import annotations

import json
import difflib
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from devagent.artifacts import RunArtifacts
from devagent.discovery import discover_repository
from devagent.memory import RepositoryMemory
from devagent.models import (
    AcceptanceSource,
    AcceptanceStatus,
    AgentState,
    ChangeMetrics,
    DeveloperReviewEvidence,
    EngineeringPlan,
    Evidence,
    Outcome,
    RiskLevel,
    ReviewDecision,
    ReviewIssue,
    RunResult,
    TaskType,
    Understanding,
    VerificationResult,
    jsonable,
    preserved_subjects,
)
from devagent.providers import ModelProvider, ProviderError
from devagent.report import render_report
from devagent.retrieval import retrieve_context
from devagent.safety import SafetyError
from devagent.state_machine import InvalidTransition, Lifecycle
from devagent.tasking import compile_task, enrich_acceptance_contract
from devagent.technical_review import analyze_developer_review
from devagent.workspace import Workspace
from devagent.worktree import select_worktree


_BUILTIN_GIT_VERIFICATION: dict[tuple[str, ...], str] = {
    ("git", "diff", "--check"): "harness",
}

_NON_EMPTY_STRING = {"type": "string", "minLength": 1, "pattern": r"\S"}
_STRING_LIST = {"type": "array", "items": _NON_EMPTY_STRING}
_CONFIDENCE = {"type": "number", "minimum": 0.0, "maximum": 1.0}

_REPLACE_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "replace_text"},
        "arguments": {
            "type": "object",
            "properties": {
                "path": _NON_EMPTY_STRING,
                "old": _NON_EMPTY_STRING,
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_COUNTED_REPLACE_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "replace_text"},
        "arguments": {
            "type": "object",
            "properties": {
                "path": _NON_EMPTY_STRING,
                "old": _NON_EMPTY_STRING,
                "new": {"type": "string"},
                "count": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "old", "new", "count"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_WRITE_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "write_file"},
        "arguments": {
            "type": "object",
            "properties": {"path": _NON_EMPTY_STRING, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_DELETE_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "delete_file"},
        "arguments": {
            "type": "object",
            "properties": {"path": _NON_EMPTY_STRING},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_MOVE_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "move_file"},
        "arguments": {
            "type": "object",
            "properties": {"source": _NON_EMPTY_STRING, "destination": _NON_EMPTY_STRING},
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_RENAME_ACTION = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "const": "rename_file"},
        "arguments": {
            "type": "object",
            "properties": {"source": _NON_EMPTY_STRING, "destination": _NON_EMPTY_STRING},
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}
_ACTION = {
    "anyOf": [
        _REPLACE_ACTION,
        _COUNTED_REPLACE_ACTION,
        _WRITE_ACTION,
        _DELETE_ACTION,
        _MOVE_ACTION,
        _RENAME_ACTION,
    ]
}

UNDERSTANDING_SCHEMA = {
    "type": "object",
    "properties": {
        "problem": _NON_EMPTY_STRING,
        "expected_behavior": _NON_EMPTY_STRING,
        "affected_paths": {**_STRING_LIST, "minItems": 1},
        "root_cause": _NON_EMPTY_STRING,
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "statement": _NON_EMPTY_STRING,
                    "paths": {**_STRING_LIST, "minItems": 1},
                    "confidence": _CONFIDENCE,
                },
                "required": ["statement", "paths", "confidence"],
                "additionalProperties": False,
            },
        },
        "proposed_solution": {**_STRING_LIST, "minItems": 1},
        "confidence": _CONFIDENCE,
    },
    "required": ["problem", "expected_behavior", "affected_paths", "root_cause", "evidence", "proposed_solution", "confidence"],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "files_to_inspect": {**_STRING_LIST, "minItems": 1},
        "implementation": {**_STRING_LIST, "minItems": 1},
        "verification": {
            "type": "array",
            "items": {"type": "array", "minItems": 1, "items": _NON_EMPTY_STRING},
        },
        "rationale": _NON_EMPTY_STRING,
    },
    "required": ["files_to_inspect", "implementation", "verification", "rationale"],
    "additionalProperties": False,
}
IMPLEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "minItems": 1, "items": _ACTION},
        "summary": {"anyOf": [_NON_EMPTY_STRING, {**_STRING_LIST, "minItems": 1}]},
    },
    "required": ["actions", "summary"],
    "additionalProperties": False,
}
DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["fix", "replan", "block"]},
        "updated_hypothesis": _NON_EMPTY_STRING,
        "actions": {"type": "array", "items": _ACTION},
    },
    "required": ["decision", "updated_hypothesis", "actions"],
    "additionalProperties": False,
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "reason": _NON_EMPTY_STRING,
                    "path": {"anyOf": [_NON_EMPTY_STRING, {"type": "null"}]},
                },
                "required": ["severity", "reason", "path"],
                "additionalProperties": False,
            },
        },
        "summary": _NON_EMPTY_STRING,
    },
    "required": ["approved", "issues", "summary"],
    "additionalProperties": False,
}


class OrchestrationError(RuntimeError):
    pass


def _strings(value: Any, field: str, role: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise OrchestrationError(
            f"Invalid {role} response field '{field}': expected array of non-empty strings; received {type(value).__name__}"
        )
    return [item.strip() for item in value]


def _non_empty_string(value: Any, role: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationError(f"Invalid {role} response field '{field}': expected non-empty string; received {type(value).__name__}")
    return value.strip()


def _bounded_confidence(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        safe_actual = repr(value) if isinstance(value, str) and value.lower() in {"low", "medium", "high"} else type(value).__name__
        raise OrchestrationError(
            f"Invalid understand response field '{field}': expected number from 0.0 through 1.0; received {safe_actual}"
        )
    confidence = float(value)
    if not 0.0 <= confidence <= 1.0:
        raise OrchestrationError(
            f"Invalid understand response field '{field}': expected number from 0.0 through 1.0; received {value!r}"
        )
    return confidence


def _understanding(response: dict[str, Any]) -> Understanding:
    try:
        expected = {"problem", "expected_behavior", "affected_paths", "root_cause", "evidence", "proposed_solution", "confidence"}
        if set(response) != expected:
            raise TypeError(f"expected exactly fields {sorted(expected)!r}")
        raw_evidence = response["evidence"]
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise TypeError("evidence must be a non-empty array")
        evidence: list[Evidence] = []
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, dict) or set(item) != {"statement", "paths", "confidence"}:
                raise TypeError(f"evidence[{index}] must contain exactly statement, paths, and confidence")
            evidence.append(
                Evidence(
                    _non_empty_string(item["statement"], "understand", f"evidence[{index}].statement"),
                    tuple(_strings(item["paths"], f"evidence[{index}].paths", "understand")),
                    _bounded_confidence(item["confidence"], f"evidence[{index}].confidence"),
                )
            )
        return Understanding(
            problem=_non_empty_string(response["problem"], "understand", "problem"),
            expected_behavior=_non_empty_string(response["expected_behavior"], "understand", "expected_behavior"),
            affected_paths=_strings(response["affected_paths"], "affected_paths", "understand"),
            root_cause=_non_empty_string(response["root_cause"], "understand", "root_cause"),
            evidence=evidence,
            proposed_solution=_strings(response["proposed_solution"], "proposed_solution", "understand"),
            confidence=_bounded_confidence(response["confidence"], "confidence"),
        )
    except OrchestrationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise OrchestrationError(f"Invalid understanding response: {exc}") from exc


def _plan(response: dict[str, Any], fallback: Sequence[tuple[str, ...]]) -> EngineeringPlan:
    try:
        expected = {"files_to_inspect", "implementation", "verification", "rationale"}
        if set(response) != expected:
            raise TypeError(f"expected exactly fields {sorted(expected)!r}")
        commands: list[tuple[str, ...]] = []
        raw_commands = response["verification"]
        if not isinstance(raw_commands, list):
            raise TypeError("verification must be a list")
        for command in raw_commands:
            if not isinstance(command, list) or not all(isinstance(token, str) and token for token in command):
                raise TypeError("verification commands must be argv arrays")
            commands.append(tuple(command))
        return EngineeringPlan(
            _strings(response["files_to_inspect"], "files_to_inspect", "plan"),
            _strings(response["implementation"], "implementation", "plan"),
            commands or list(fallback),
            _non_empty_string(response["rationale"], "plan", "rationale"),
        )
    except OrchestrationError:
        raise
    except (KeyError, TypeError) as exc:
        raise OrchestrationError(f"Invalid plan response: {exc}") from exc


def _review(response: dict[str, Any]) -> ReviewDecision:
    try:
        if set(response) != {"approved", "issues", "summary"}:
            raise TypeError("expected exactly approved, issues, and summary")
        if not isinstance(response["approved"], bool) or not isinstance(response["issues"], list):
            raise TypeError("approved must be boolean and issues must be an array")
        issues: list[ReviewIssue] = []
        for index, item in enumerate(response["issues"]):
            if not isinstance(item, dict) or set(item) != {"severity", "reason", "path"}:
                raise TypeError(f"issues[{index}] must contain exactly severity, reason, and path")
            severity = _non_empty_string(item["severity"], "review", f"issues[{index}].severity")
            if severity not in {"low", "medium", "high", "critical"}:
                raise TypeError(f"issues[{index}].severity is unsupported")
            path = item["path"]
            if path is not None:
                path = _non_empty_string(path, "review", f"issues[{index}].path")
            issues.append(ReviewIssue(severity, _non_empty_string(item["reason"], "review", f"issues[{index}].reason"), path))
        approved = response["approved"]
        if approved and issues:
            raise OrchestrationError("Reviewer cannot approve while returning issues")
        return ReviewDecision(approved, issues, _non_empty_string(response["summary"], "review", "summary"))
    except OrchestrationError:
        raise
    except (KeyError, TypeError) as exc:
        raise OrchestrationError(f"Invalid review response: {exc}") from exc


def _execute_actions(workspace: Workspace, response: dict[str, Any], allowed_paths: set[str]) -> list[str]:
    if not isinstance(response, dict) or set(response) != {"actions", "summary"}:
        raise OrchestrationError("Invalid implement response: expected exactly actions and summary")
    actions = response.get("actions")
    if not isinstance(actions, list) or not actions:
        raise OrchestrationError("Implementation requires at least one structured action")
    changed: list[str] = []
    allowed_tools = {"replace_text", "write_file", "delete_file", "move_file", "rename_file"}
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"tool", "arguments"} or not isinstance(action["arguments"], dict):
            raise OrchestrationError("Every action must contain only tool and arguments")
        tool, arguments = action["tool"], action["arguments"]
        if tool not in allowed_tools:
            raise OrchestrationError(f"Implementation tool is not allowed: {tool}")

        if tool in {"move_file", "rename_file"}:
            if set(arguments) != {"source", "destination"}:
                raise OrchestrationError(f"{tool} requires only string source and destination")
            source = arguments.get("source")
            destination = arguments.get("destination")
            if (
                not isinstance(source, str)
                or not isinstance(destination, str)
                or source not in allowed_paths
                or destination not in allowed_paths
            ):
                raise OrchestrationError(
                    f"Structural source/destination was not inspected/planned: {source} -> {destination}"
                )
            if tool == "move_file":
                workspace.move_file(source, destination)
            else:
                workspace.rename_file(source, destination)
            changed.extend((source, destination))
            continue

        path = arguments.get("path")
        if not isinstance(path, str) or path not in allowed_paths:
            raise OrchestrationError(f"Action path was not inspected/planned: {path}")
        if tool == "replace_text":
            required = {"path", "old", "new"}
            if frozenset(arguments) not in {frozenset(required), frozenset({*required, "count"})}:
                raise OrchestrationError("replace_text arguments must contain only path, old, new, and optional count")
            if not all(isinstance(arguments[key], str) for key in required) or not arguments["old"]:
                raise OrchestrationError("replace_text requires string path, non-empty old, and string new")
            count = arguments.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise OrchestrationError("replace_text count must be an integer greater than zero")
            workspace.replace_text(path, arguments["old"], arguments["new"], count)
        elif tool == "write_file":
            if set(arguments) != {"path", "content"} or not isinstance(arguments.get("content"), str):
                raise OrchestrationError("write_file requires only string path and content")
            workspace.write_file(path, arguments["content"])
        else:
            if set(arguments) != {"path"}:
                raise OrchestrationError("delete_file requires only string path")
            workspace.delete_file(path)
        changed.append(path)
    return list(dict.fromkeys(changed))


def _diff(root: Path, workspace: Workspace, max_chars: int = 40_000) -> str:
    completed = subprocess.run(["git", "diff", "--no-ext-diff", "--"], cwd=root, capture_output=True, text=True, timeout=20, check=False)
    text = completed.stdout if completed.returncode == 0 else "(Git diff unavailable)\n"
    tracked = {
        line[3:].split(" -> ")[-1]
        for line in subprocess.run(["git", "status", "--porcelain=v1"], cwd=root, capture_output=True, text=True, timeout=20, check=False).stdout.splitlines()
        if len(line) > 3 and not line.startswith("??")
    }
    for relative in sorted(workspace.modified_paths - tracked):
        target = root / relative
        if not target.is_file():
            continue
        content = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        text += "".join(difflib.unified_diff([], content, fromfile="/dev/null", tofile=f"b/{relative}"))
    return text[:max_chars]


def _metrics(root: Path, modified_paths: set[str]) -> ChangeMetrics:
    completed = subprocess.run(["git", "diff", "--numstat", "--"], cwd=root, capture_output=True, text=True, timeout=20, check=False)
    added = deleted = 0
    paths: set[str] = set(modified_paths)
    if completed.returncode == 0:
        for line in completed.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                deleted += int(parts[1])
            paths.add(parts[2])
    for relative in modified_paths:
        tracked = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{relative}"], cwd=root, capture_output=True, timeout=10, check=False
        ).returncode == 0
        target = root / relative
        if not tracked and target.is_file():
            added += len(target.read_text(encoding="utf-8", errors="replace").splitlines())
    return ChangeMetrics(len(paths), added, deleted, sorted(paths))


def _default_commands(repository: Any) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    targeted: list[tuple[str, ...]] = []
    broad: list[tuple[str, ...]] = []
    for capability in repository.capabilities:
        if not capability.trusted:
            continue
        destination = broad if capability.broad or capability.kind in {"build", "lint", "typecheck", "integration"} else targeted
        if capability.command not in destination:
            destination.append(capability.command)
    if not targeted and any("python" in component.languages for component in repository.components):
        targeted.append(("python", "-m", "compileall", "-q", "."))
    return targeted[:3], broad[:5]


def _builtin_verification_commands(repository: Any) -> list[tuple[str, ...]]:
    if not repository.git_head:
        return []
    return list(_BUILTIN_GIT_VERIFICATION)


def _command_kind(command: tuple[str, ...], repository: Any) -> str | None:
    if repository.git_head:
        builtin_kind = _BUILTIN_GIT_VERIFICATION.get(command)
        if builtin_kind is not None:
            return builtin_kind
    for capability in repository.capabilities:
        if not capability.trusted:
            continue
        if command == capability.command:
            return capability.kind
        if capability.command[:3] in {("python", "-m", "pytest"), ("python3", "-m", "pytest")} and command[:3] == capability.command[:3]:
            return capability.kind
        if capability.command and command and capability.command[0] in {"pytest", "cargo", "go", "mvn", "ctest", "dotnet"} and command[0] == capability.command[0]:
            return capability.kind
    if command[:3] in {("python", "-m", "compileall"), ("python3", "-m", "compileall")} and any(
        "python" in component.languages for component in repository.components
    ):
        return "build"
    return None


_ACCEPTANCE_STOPWORDS = frozenset(
    {
        "add", "and", "the", "handle", "safely", "changing", "implement", "support", "preserve", "preserving", "existing", "behavior",
        "behaviour", "function", "functionality", "values", "value", "with", "without",
        "from", "into", "that", "this", "then", "than", "must", "should", "shall",
        "keep", "ensure", "when", "where", "make", "changes", "change", "tests", "test",
        "verify", "verification", "relevant", "automated", "pass", "passes", "final",
        "revision", "repository", "supported", "check", "review", "independent", "current",
        "normal", "edge", "case", "covered", "coverage", "feature", "follows", "conventions",
        "unrelated", "externally", "observable", "remains", "unchanged", "explicitly",
        "requested", "root", "cause", "addressed", "merely", "masked", "regression",
        "protects", "refactored", "migration", "strategy", "application", "contract", "against",
        "representative", "state", "safe", "forward", "rollback", "non", "reversible",
    }
)


def _acceptance_tokens(text: str) -> set[str]:
    preferred = {
        item.lower()
        for item in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
        if item.lower() not in _ACCEPTANCE_STOPWORDS
    }
    preferred.update(
        item.lower()
        for item in re.findall(r"`([A-Za-z_][A-Za-z0-9_.-]*)`", text)
        if item.lower() not in _ACCEPTANCE_STOPWORDS
    )
    if preferred:
        return preferred
    return {
        item.lower()
        for item in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", text)
        if len(item) > 2 and item.lower() not in _ACCEPTANCE_STOPWORDS
    }


def _matching_acceptance_inventory(
    description: str,
    changes: ChangeMetrics,
    developer_review: DeveloperReviewEvidence,
) -> tuple[list[str], list[str], set[str]]:
    tokens = _acceptance_tokens(description)
    matched: list[str] = []
    matched_tests: list[str] = []
    matched_tokens: set[str] = set()
    inventory: list[tuple[str, str, bool]] = []
    for symbol in developer_review.changed_symbols:
        inventory.append((f"symbol {symbol.path}:{symbol.name}", f"{symbol.path} {symbol.name}".lower(), False))
    for test in developer_review.test_cases:
        inventory.append((f"test {test.path}:{test.name}", f"{test.path} {test.name}".lower(), True))
    for path in changes.paths:
        inventory.append((f"changed path {path}", path.lower(), _is_test_path(path)))

    for label, searchable, is_test in inventory:
        hits = {token for token in tokens if token in searchable}
        if not hits:
            continue
        matched_tokens.update(hits)
        if label not in matched:
            matched.append(label)
        if is_test and label not in matched_tests:
            matched_tests.append(label)
    return matched, matched_tests, matched_tokens


def _set_acceptance(
    criterion: Any,
    status: AcceptanceStatus,
    evidence: Sequence[str] = (),
    reason: str | None = None,
) -> None:
    criterion.status = status
    criterion.evidence = list(dict.fromkeys(item for item in evidence if item))
    criterion.reason = reason


def _baseline_subject_evidence(root: Path, subject: str, paths: Sequence[str]) -> list[str]:
    """Find a named preservation subject in the exact Git baseline for affected paths."""

    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(subject)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    evidence: list[str] = []
    for path in sorted(set(paths)):
        completed = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0 and pattern.search(completed.stdout):
            evidence.append(f"baseline {path} contains named subject {subject}")
    return evidence


def _final_contract_evidence(root: Path, contract: str, paths: Sequence[str]) -> list[str]:
    """Find an exact quoted user contract in bounded final relevant files."""

    needle = contract.lower()
    evidence: list[str] = []
    for path in sorted(set(paths)):
        target = root / path
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text.lower():
            evidence.append(f"final {path} contains exact quoted contract")
    return evidence


def _support_acceptance_criteria(
    task: Any,
    repository: Any,
    understanding: Understanding,
    changes: ChangeMetrics,
    verification: list[VerificationResult],
    final_results: list[VerificationResult],
    review: ReviewDecision,
    developer_review: DeveloperReviewEvidence,
    implementation: Sequence[str],
    diff_text: str,
    working_root: Path | None = None,
) -> None:
    passing_tests = [
        result
        for result in final_results
        if result.passed and _command_kind(result.command, repository) in {"test", "integration"}
    ]
    baseline_tests = [
        result
        for result in verification
        if result.baseline and result.passed and _command_kind(result.command, repository) in {"test", "integration"}
    ]
    diff_checks = [
        result for result in final_results if result.passed and result.command == ("git", "diff", "--check")
    ]
    changed_tests = sorted(
        set(path for path in changes.paths if _is_test_path(path))
        | set(developer_review.test_files)
    )
    all_final_pass = bool(final_results) and all(result.passed for result in final_results)
    implementation_text = " ".join(implementation).lower()
    lower_diff = diff_text.lower()
    conflicts = set(understanding.preservation_conflicts())

    for criterion in task.acceptance_criteria:
        _set_acceptance(criterion, AcceptanceStatus.UNPROVEN, reason="No criterion-specific final evidence was established")
        lowered = criterion.description.lower()

        if criterion.source is AcceptanceSource.REPOSITORY:
            result = next(
                (
                    item
                    for item in final_results
                    if criterion.verification_command is not None
                    and item.command == criterion.verification_command
                    and item.passed
                ),
                None,
            )
            if result is not None:
                _set_acceptance(
                    criterion,
                    AcceptanceStatus.SATISFIED,
                    [" ".join(result.command)],
                    "Trusted repository check passed on the final revision",
                )
            else:
                criterion.reason = "Repository-derived final verification command did not pass exactly as discovered"
            continue

        if criterion.source is AcceptanceSource.QUALITY_GATE:
            if lowered == "relevant automated tests pass":
                if passing_tests:
                    _set_acceptance(
                        criterion,
                        AcceptanceStatus.SATISFIED,
                        [" ".join(item.command) for item in passing_tests],
                        "Evidence-backed test command passed on the final revision",
                    )
                else:
                    criterion.reason = "No evidence-backed test/integration command passed on the final revision"
            elif lowered == "no unrelated behavior changes":
                if review.approved and diff_checks:
                    _set_acceptance(
                        criterion,
                        AcceptanceStatus.SATISFIED,
                        [
                            f"Minimal-diff gate accepted {changes.files_changed} changed file(s)",
                            "Independent reviewer approved the final diff",
                            "git diff --check",
                        ],
                        "Scope was bounded by the deterministic minimal-diff gate and independent review",
                    )
                else:
                    criterion.reason = "Scope cannot be accepted without both final diff validation and independent review"
            elif lowered == "final diff and independent review pass":
                if review.approved and diff_checks and all_final_pass:
                    _set_acceptance(
                        criterion,
                        AcceptanceStatus.SATISFIED,
                        ["Independent reviewer approved the final diff", "git diff --check"],
                        "All current-revision final checks and independent review passed",
                    )
                else:
                    criterion.reason = "Final verification and independent review are not both proven"
            continue

        if criterion.source is AcceptanceSource.TASK_POLICY:
            evidence: list[str] = []
            satisfied = False
            if lowered == "the feature follows existing repository conventions":
                satisfied = review.approved and bool(changes.paths)
                evidence = ["Independent reviewer approved repository-convention fit", *changes.paths]
            elif lowered == "normal and relevant edge-case behavior are covered":
                satisfied = bool(changed_tests and passing_tests)
                evidence = [*changed_tests, *(" ".join(item.command) for item in passing_tests)]
            elif lowered == "the root cause is addressed, not merely masked":
                satisfied = review.approved and bool(changes.paths) and bool(passing_tests or final_results)
                evidence = ["Independent reviewer approved the root-cause fix", *changes.paths]
            elif lowered == "regression coverage exercises the failing case":
                satisfied = bool(changed_tests and passing_tests)
                evidence = [*changed_tests, *(" ".join(item.command) for item in passing_tests)]
            elif lowered == "tests assert externally meaningful behavior":
                satisfied = review.approved and bool(changed_tests and passing_tests)
                evidence = [*changed_tests, "Independent reviewer approved the test behavior"]
            elif lowered == "externally observable behavior remains unchanged unless explicitly requested":
                satisfied = review.approved and bool(baseline_tests and passing_tests)
                evidence = [
                    "Independent reviewer approved behavior preservation",
                    *("baseline: " + " ".join(item.command) for item in baseline_tests),
                    *("final: " + " ".join(item.command) for item in passing_tests),
                ]
            elif lowered == "regression coverage protects the refactored behavior":
                satisfied = review.approved and bool(baseline_tests and passing_tests)
                evidence = [
                    "Independent reviewer approved refactor regression coverage",
                    *("baseline: " + " ".join(item.command) for item in baseline_tests),
                    *("final: " + " ".join(item.command) for item in passing_tests),
                ]
            elif lowered == "migration preserves compatibility with the current supported application contract":
                satisfied = review.approved and bool(baseline_tests and passing_tests)
                evidence = [
                    "Independent reviewer approved migration compatibility",
                    *("baseline: " + " ".join(item.command) for item in baseline_tests),
                    *("final: " + " ".join(item.command) for item in passing_tests),
                ]
            elif lowered == "migration has an explicit forward and rollback or safe non-reversible strategy":
                strategy_terms = ("rollback", "downgrade", "reversible", "non-reversible", "expand", "contract")
                present = [term for term in strategy_terms if term in lower_diff]
                satisfied = review.approved and bool(present)
                evidence = ["Independent reviewer approved migration strategy", *(f"strategy evidence: {item}" for item in present)]
            elif lowered == "migration behavior is covered against representative existing state":
                satisfied = bool(baseline_tests and changed_tests and passing_tests)
                evidence = [
                    *changed_tests,
                    *("baseline: " + " ".join(item.command) for item in baseline_tests),
                    *("final: " + " ".join(item.command) for item in passing_tests),
                ]
            if satisfied:
                _set_acceptance(criterion, AcceptanceStatus.SATISFIED, evidence, "Task-specific engineering policy was proven")
            else:
                criterion.reason = "Task-specific engineering policy lacks its required final evidence"
            continue

        # User criteria are never blanket-proven by a passing command. They need semantic
        # linkage to changed code/tests (or an exact quoted contract) plus final verification.
        tokens = _acceptance_tokens(criterion.description)
        matched, matched_tests, matched_tokens = _matching_acceptance_inventory(
            criterion.description, changes, developer_review
        )
        diff_tokens = {token for token in tokens if token in lower_diff}
        semantic_tokens = matched_tokens | diff_tokens
        quoted_contracts = [
            item for item in re.findall(r'["\']([^"\']{4,})["\']', criterion.description)
        ]
        relevant_paths = [*understanding.affected_paths, *changes.paths]
        quoted_evidence: list[str] = []
        quoted_missing: list[str] = []
        for contract in quoted_contracts:
            hits = (
                _final_contract_evidence(working_root, contract, relevant_paths)
                if working_root is not None
                else []
            )
            if hits:
                quoted_evidence.extend(hits)
            elif contract.lower() in lower_diff:
                quoted_evidence.append("Final diff contains exact quoted contract")
            else:
                quoted_missing.append(contract)
        conflict = next((subject for subject in conflicts if subject in lowered), None)
        if conflict:
            _set_acceptance(
                criterion,
                AcceptanceStatus.CONTRADICTED,
                [],
                f"Baseline evidence contradicts the requested preservation subject: {conflict}",
            )
            continue

        if "unrelated" in lowered:
            if review.approved and diff_checks:
                _set_acceptance(
                    criterion,
                    AcceptanceStatus.SATISFIED,
                    [f"Minimal-diff gate accepted {changes.files_changed} changed file(s)", "Independent reviewer approved the final diff"],
                    "User scope constraint passed deterministic scope and review gates",
                )
            else:
                criterion.reason = "User scope constraint lacks deterministic diff/review proof"
            continue

        preservation = any(word in lowered for word in ("preserve", "unchanged", "remain", "existing"))
        behavior_specific = any(
            word in lowered
            for word in ("return", "raise", "error", "empty", "positive", "negative", "decimal", "when", "support")
        )
        if quoted_missing:
            criterion.reason = "Exact quoted user contract is not present in the final diff: " + "; ".join(quoted_missing)
            continue

        preservation = preservation or "without changing" in lowered
        generic_test_intent = (
            not tokens
            and any(term in lowered for term in ("regression test", "add test", "add tests", "verify the application", "verify application"))
        )
        if generic_test_intent:
            if changed_tests and passing_tests and review.approved:
                _set_acceptance(
                    criterion,
                    AcceptanceStatus.SATISFIED,
                    [
                        *changed_tests,
                        *(" ".join(item.command) for item in passing_tests),
                        "Independent reviewer approved the final diff",
                    ],
                    "Generic user test/verification intent is proven by changed test coverage, final tests, and independent review",
                )
            else:
                criterion.reason = "Generic user test/verification intent lacks changed tests, passing final tests, or independent review"
            continue

        if preservation and not matched_tests:
            criterion.reason = "Preservation requirement has no matching regression-test inventory evidence"
            continue
        named_subjects = preserved_subjects(criterion.description) if preservation else set()
        named_baseline_evidence: list[str] = []
        if named_subjects:
            if working_root is None:
                criterion.reason = "Named preservation requirement has no deterministic baseline repository root"
                continue
            missing_subjects: list[str] = []
            for subject in sorted(named_subjects):
                hits = _baseline_subject_evidence(working_root, subject, understanding.affected_paths)
                if hits:
                    named_baseline_evidence.extend(hits)
                else:
                    missing_subjects.append(subject)
            if missing_subjects:
                criterion.reason = (
                    "Named preservation subject is not present in deterministic baseline affected-path evidence: "
                    + ", ".join(missing_subjects)
                )
                continue
        if preservation and not baseline_tests:
            criterion.reason = "Preservation requirement has no passing baseline test evidence for comparison"
            continue
        if tokens and not semantic_tokens:
            criterion.reason = "No changed symbol, test, path, or final diff token semantically matches this user criterion"
            continue
        if behavior_specific and task.requires_tests and not matched_tests:
            criterion.reason = "Behavior-specific requirement has no semantically matching test-case evidence"
            continue
        if task.requires_tests and not passing_tests:
            criterion.reason = "User criterion cannot be satisfied because no final evidence-backed tests passed"
            continue
        evidence = [*matched, *quoted_evidence, *named_baseline_evidence]
        if diff_tokens:
            evidence.append("Final diff semantic tokens: " + ", ".join(sorted(diff_tokens)))
        if preservation:
            evidence.extend("baseline: " + " ".join(item.command) for item in baseline_tests)
        evidence.extend("final: " + " ".join(item.command) for item in passing_tests)
        if review.approved:
            evidence.append("Independent reviewer approved the final diff")
        _set_acceptance(
            criterion,
            AcceptanceStatus.SATISFIED,
            evidence,
            "User criterion is linked to final code/test evidence and current-revision verification",
        )


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


def _run_commands(workspace: Workspace, commands: Sequence[tuple[str, ...]], phase: str, *, baseline: bool = False) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    for command in commands:
        try:
            results.append(workspace.run(command, phase=phase, baseline=baseline))
        except (SafetyError, OSError) as exc:
            raise OrchestrationError(f"Cannot run {' '.join(command)}: {exc}") from exc
    return results


def _context_supports_understanding(
    understanding: Understanding, context: dict[str, object], working_root: Path
) -> bool:
    if not understanding.implementation_ready(working_root):
        return False
    snippets = context.get("snippets", {})
    supplied_paths = set(snippets) if isinstance(snippets, dict) else set()
    claimed_paths = {
        path
        for evidence in understanding.evidence
        for path in evidence.paths
    }
    existing_affected_paths = {
        path for path in understanding.affected_paths if (working_root / path).is_file()
    }
    return (
        existing_affected_paths.issubset(supplied_paths)
        and bool(claimed_paths)
        and claimed_paths.issubset(supplied_paths)
        and all((working_root / path).is_file() for path in claimed_paths)
    )


def _baseline_test_count_regressed(
    baseline_results: Sequence[VerificationResult],
    final_results: Sequence[VerificationResult],
) -> tuple[VerificationResult, VerificationResult] | None:
    for baseline in baseline_results:
        if baseline.tests_run is None:
            continue
        for final in final_results:
            if final.command == baseline.command and final.tests_run is not None:
                if final.tests_run < baseline.tests_run:
                    return baseline, final
                break
    return None


class DevAgent:
    """Deterministic orchestration; model reasoning is bounded inside named states."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_corrections: int = 2,
        isolate: bool = True,
        verbose: bool = False,
        status: Callable[[str], None] | None = None,
        base_commit: str | None = None,
    ) -> None:
        self.provider = provider
        self.max_corrections = max(0, min(max_corrections, 5))
        self.isolate = isolate
        self.verbose = verbose
        self.status = status or (lambda message: None)
        self.base_commit = base_commit

    def _announce(self, artifacts: RunArtifacts, lifecycle: Lifecycle) -> None:
        artifacts.record("state", state=lifecycle.state)
        if self.verbose:
            self.status(f"[{lifecycle.state.value}]")

    def _diagnostic(self, artifacts: RunArtifacts, category: str, message: str) -> None:
        artifacts.record("diagnostic", category=category, message=message)
        if self.verbose:
            self.status(f"[{category}] {message}")

    def _show_retrieval(self, artifacts: RunArtifacts, context: dict[str, object]) -> None:
        diagnostics = context.get("diagnostics", {})
        if not isinstance(diagnostics, dict):
            return
        self._diagnostic(
            artifacts,
            "RETRIEVAL",
            (
                f"lexical matches: {diagnostics.get('exact_lexical_matches', 0)} exact, "
                f"{diagnostics.get('normalized_lexical_matches', 0)} normalized"
            ),
        )
        self._diagnostic(
            artifacts,
            "RETRIEVAL",
            f"fallback: {diagnostics.get('fallback', 'none')}",
        )
        selected = diagnostics.get("selected", [])
        if isinstance(selected, list):
            for path in selected:
                self._diagnostic(artifacts, "RETRIEVAL", f"selected: {path}")

    def run(self, repository_root: Path | str, requirement: str) -> RunResult:
        root = Path(repository_root).expanduser().resolve()
        task = compile_task(requirement)
        artifacts = RunArtifacts(root)
        lifecycle = Lifecycle()
        verification: list[VerificationResult] = []
        not_run: list[str] = []
        implementation: list[str] = []
        recommendations: list[str] = []
        understanding = Understanding("", "", [], "", [], [], 0.0)
        review: ReviewDecision | None = None
        developer_review = DeveloperReviewEvidence()
        source_repository = discover_repository(root, probe_capabilities=False)
        effective_head = self.base_commit or source_repository.git_head
        selection = select_worktree(
            root,
            artifacts.run_id,
            enabled=self.isolate,
            git_head=effective_head,
            dirty_files=source_repository.dirty_files,
        )
        working_root = selection.root
        repository = discover_repository(working_root)
        repository.root = str(root)
        repository.git_branch = source_repository.git_branch
        repository.git_head = effective_head
        repository.dirty_files = source_repository.dirty_files
        task = enrich_acceptance_contract(task, repository)
        workspace = Workspace(working_root, artifacts, source_repository.dirty_files if not selection.isolated else ())
        artifacts.write_json("metadata.json", {"run_id": artifacts.run_id, "task": task, "repository": repository})
        artifacts.record("worktree", source=str(root), working_root=str(working_root), isolated=selection.isolated, reason=selection.reason)
        self._announce(artifacts, lifecycle)
        self._diagnostic(artifacts, "WORKTREE", f"source: {root}")
        self._diagnostic(artifacts, "WORKTREE", f"isolated: {str(selection.isolated).lower()}")
        self._diagnostic(artifacts, "WORKTREE", f"working_root: {working_root}")

        try:
            if selection.creation_failed:
                raise OrchestrationError(selection.reason)
            lifecycle.transition(AgentState.DISCOVER)
            self._announce(artifacts, lifecycle)
            self._diagnostic(
                artifacts,
                "DISCOVER",
                f"repository files: {repository.inventory_file_count}",
            )
            self._diagnostic(
                artifacts,
                "DISCOVER",
                f"components: {len(repository.components)}",
            )
            for diagnostic in repository.capability_diagnostics:
                self._diagnostic(artifacts, "VERIFICATION_DISCOVERY", diagnostic)
            memory = RepositoryMemory(root)
            memory.load_facts()
            memory.store_facts(repository.facts)

            lifecycle.transition(AgentState.UNDERSTAND)
            self._announce(artifacts, lifecycle)
            context = retrieve_context(
                workspace,
                repository,
                task.goal,
                requires_tests=task.requires_tests,
            )
            self._show_retrieval(artifacts, context)
            for context_attempt in range(2):
                response = self.provider.request(role="understand", payload={"task": jsonable(task), "repository": context}, schema=UNDERSTANDING_SCHEMA)
                understanding = _understanding(response)
                if _context_supports_understanding(understanding, context, working_root):
                    break
                if context_attempt == 0:
                    lifecycle.transition(AgentState.GATHER_CONTEXT)
                    self._announce(artifacts, lifecycle)
                    context = retrieve_context(
                        workspace,
                        repository,
                        task.goal + " " + " ".join(understanding.affected_paths),
                        max_chars=32_000,
                        requires_tests=task.requires_tests,
                    )
                    self._show_retrieval(artifacts, context)
                    lifecycle.transition(AgentState.UNDERSTAND)
                    self._announce(artifacts, lifecycle)
            else:
                raise OrchestrationError("Evidence gate rejected implementation: root cause or source evidence is insufficient")

            lifecycle.transition(AgentState.TASK_SPEC)
            self._announce(artifacts, lifecycle)
            targeted, broad = _default_commands(repository)
            lifecycle.transition(AgentState.BASELINE)
            self._announce(artifacts, lifecycle)
            if targeted:
                verification.extend(_run_commands(workspace, targeted[:1], "baseline", baseline=True))
            else:
                not_run.append("Baseline: no evidence-backed local test or compile command was discovered")

            lifecycle.transition(AgentState.PLAN)
            self._announce(artifacts, lifecycle)
            plan_response = self.provider.request(
                role="plan",
                payload={
                    "task": jsonable(task),
                    "understanding": jsonable(understanding),
                    "capabilities": jsonable(repository.capabilities),
                    "builtin_verification": jsonable(
                        _builtin_verification_commands(repository)
                    ),
                },
                schema=PLAN_SCHEMA,
            )
            plan = _plan(plan_response, targeted)
            unsupported = [command for command in plan.verification if _command_kind(command, repository) is None]
            if unsupported:
                raise OrchestrationError(
                    "Plan requested verification not supported by repository evidence "
                    f"or DevAgent built-ins: {' '.join(unsupported[0])}"
                )
            if task.requires_tests and not any(_command_kind(command, repository) in {"test", "integration"} for command in plan.verification):
                raise OrchestrationError("A task requiring tests must use an evidence-backed test command")
            allowed_paths = set(plan.files_to_inspect)
            if not set(understanding.affected_paths).issubset(allowed_paths):
                raise OrchestrationError("Plan does not include all evidence-backed affected paths")
            for path in allowed_paths:
                if (working_root / path).exists():
                    workspace.read_file(path)

            lifecycle.transition(AgentState.GATHER_CONTEXT)
            self._announce(artifacts, lifecycle)
            lifecycle.transition(AgentState.REPRODUCE)
            self._announce(artifacts, lifecycle)
            baseline_failed = any(result.baseline and not result.passed for result in verification)
            if task.task_type in {TaskType.BUG_FIX, TaskType.RUNTIME_ERROR, TaskType.TEST_FAILURE} and not baseline_failed:
                not_run.append("Pre-change failure was not independently reproduced; implementation is based on source evidence and final regression verification")

            lifecycle.transition(AgentState.IMPLEMENT)
            self._announce(artifacts, lifecycle)
            implement_response = self.provider.request(
                role="implement",
                payload={"task": jsonable(task), "understanding": jsonable(understanding), "plan": jsonable(plan), "files": {path: workspace.read_file(path) for path in allowed_paths if (working_root / path).is_file()}},
                schema=IMPLEMENT_SCHEMA,
            )
            _execute_actions(workspace, implement_response, allowed_paths)
            implementation.extend(_strings(implement_response.get("summary", []), "summary", "implement") if isinstance(implement_response.get("summary"), list) else [str(implement_response.get("summary", "Implemented planned change"))])

            corrections = 0
            failure_signatures: list[tuple[object, ...]] = []
            while True:
                lifecycle.transition(AgentState.VERIFY_TARGETED)
                self._announce(artifacts, lifecycle)
                current_targeted = plan.verification or targeted
                if not current_targeted:
                    raise OrchestrationError("No targeted verification command is available")
                target_results = _run_commands(workspace, current_targeted[:3], "targeted")
                verification.extend(target_results)
                if all(result.passed for result in target_results):
                    break
                lifecycle.transition(AgentState.DIAGNOSE)
                self._announce(artifacts, lifecycle)
                if corrections >= self.max_corrections:
                    raise OrchestrationError("Targeted verification still fails after the bounded correction budget")
                diagnosis = self.provider.request(
                    role="diagnose",
                    payload={"task": jsonable(task), "failures": jsonable([result for result in target_results if not result.passed]), "failed_hypotheses": recommendations},
                    schema=DIAGNOSE_SCHEMA,
                )
                decision = diagnosis.get("decision")
                signature = tuple((result.command, result.exit_code, result.classification, result.stderr[-1000:]) for result in target_results if not result.passed)
                failure_signatures.append(signature)
                if failure_signatures.count(signature) >= 2:
                    decision = "replan"
                hypothesis = str(diagnosis.get("updated_hypothesis", "")).strip()
                if hypothesis:
                    recommendations.append(f"Diagnosis {corrections + 1}: {hypothesis}")
                if decision == "block":
                    raise OrchestrationError(hypothesis or "Diagnosis could not identify a safe correction")
                if decision == "replan":
                    lifecycle.transition(AgentState.PLAN)
                    self._announce(artifacts, lifecycle)
                    replanned = self.provider.request(
                        role="replan",
                        payload={
                            "task": jsonable(task),
                            "understanding": jsonable(understanding),
                            "failures": jsonable(target_results),
                            "failed_hypotheses": recommendations,
                            "capabilities": jsonable(repository.capabilities),
                            "builtin_verification": jsonable(
                                _builtin_verification_commands(repository)
                            ),
                        },
                        schema=PLAN_SCHEMA,
                    )
                    plan = _plan(replanned, targeted)
                    if any(_command_kind(command, repository) is None for command in plan.verification):
                        raise OrchestrationError(
                            "Replan selected verification unsupported by repository "
                            "evidence or DevAgent built-ins"
                        )
                    allowed_paths.update(plan.files_to_inspect)
                    lifecycle.transition(AgentState.GATHER_CONTEXT)
                    self._announce(artifacts, lifecycle)
                    for path in plan.files_to_inspect:
                        if (working_root / path).is_file():
                            workspace.read_file(path)
                    lifecycle.transition(AgentState.REPRODUCE)
                    self._announce(artifacts, lifecycle)
                    lifecycle.transition(AgentState.IMPLEMENT)
                    self._announce(artifacts, lifecycle)
                    correction_response = self.provider.request(
                        role="implement_replan",
                        payload={"plan": jsonable(plan), "diagnosis": hypothesis, "files": {path: workspace.read_file(path) for path in allowed_paths if (working_root / path).is_file()}},
                        schema=IMPLEMENT_SCHEMA,
                    )
                else:
                    lifecycle.transition(AgentState.IMPLEMENT)
                    self._announce(artifacts, lifecycle)
                    correction_response = {"actions": diagnosis.get("actions"), "summary": [hypothesis or "Applied focused correction"]}
                _execute_actions(workspace, correction_response, allowed_paths)
                implementation.append(hypothesis or "Applied focused correction")
                corrections += 1

            lifecycle.transition(AgentState.VERIFY_BROAD)
            self._announce(artifacts, lifecycle)
            broad_results = _run_commands(workspace, broad, "broad") if broad else []
            verification.extend(broad_results)
            if any(not result.passed for result in broad_results):
                not_run.append("One or more broader checks did not pass in the local environment")

            lifecycle.transition(AgentState.REVIEW)
            self._announce(artifacts, lifecycle)
            review = _review(self.provider.request(
                role="review",
                payload={
                    "task": jsonable(task),
                    "acceptance_criteria": jsonable(task.acceptance_criteria),
                    "conventions": jsonable(repository.facts),
                    "diff": _diff(working_root, workspace),
                    "verification": jsonable(verification),
                },
                schema=REVIEW_SCHEMA,
            ))
            if not review.approved:
                if corrections >= self.max_corrections:
                    raise OrchestrationError("Independent review rejected the patch after the correction budget")
                lifecycle.transition(AgentState.IMPLEMENT)
                self._announce(artifacts, lifecycle)
                revision_response = self.provider.request(
                    role="implement_review_fixes",
                    payload={"issues": jsonable(review.issues), "plan": jsonable(plan), "diff": _diff(working_root, workspace)},
                    schema=IMPLEMENT_SCHEMA,
                )
                _execute_actions(workspace, revision_response, allowed_paths)
                implementation.append(str(revision_response.get("summary", "Addressed review findings")))
                corrections += 1
                lifecycle.transition(AgentState.VERIFY_TARGETED)
                self._announce(artifacts, lifecycle)
                review_fix_results = _run_commands(workspace, plan.verification or targeted, "targeted_after_review")
                verification.extend(review_fix_results)
                if any(not result.passed for result in review_fix_results):
                    raise OrchestrationError("Verification failed after review corrections")
                lifecycle.transition(AgentState.VERIFY_BROAD)
                self._announce(artifacts, lifecycle)
                verification.extend(_run_commands(workspace, broad, "broad_after_review") if broad else [])
                lifecycle.transition(AgentState.REVIEW)
                self._announce(artifacts, lifecycle)
                review = _review(self.provider.request(role="review", payload={"task": jsonable(task), "diff": _diff(working_root, workspace), "verification": jsonable(verification)}, schema=REVIEW_SCHEMA))
                if not review.approved:
                    raise OrchestrationError("Independent reviewer still rejects the corrected patch")

            lifecycle.transition(AgentState.QUALITY_CHECK)
            self._announce(artifacts, lifecycle)
            changes = _metrics(working_root, workspace.modified_paths)
            if changes.files_changed > 8 or changes.lines_added + changes.lines_deleted > 500:
                raise OrchestrationError("Minimal-diff gate rejected unexpectedly broad scope")
            if task.requires_tests and task.task_type not in {TaskType.TEST_FAILURE, TaskType.UNIT_TEST, TaskType.REFACTOR} and not any(
                _is_test_path(path) for path in changes.paths
            ):
                raise OrchestrationError("Required regression/feature coverage was not added or updated")
            quality_commands = _builtin_verification_commands(repository)
            quality_results = _run_commands(workspace, quality_commands, "quality")
            verification.extend(quality_results)
            if any(not result.passed for result in quality_results):
                raise OrchestrationError("git diff --check rejected the patch")

            lifecycle.transition(AgentState.FINAL_VERIFY)
            self._announce(artifacts, lifecycle)
            final_commands = list(dict.fromkeys([*(plan.verification or targeted), *broad, *quality_commands]))
            final_results = _run_commands(workspace, final_commands, "final")
            verification.extend(final_results)
            regression = _baseline_test_count_regressed(
                [result for result in verification if result.baseline],
                final_results,
            )
            if regression is not None:
                baseline_result, final_result = regression
                raise OrchestrationError(
                    "Final verification collected fewer tests than baseline "
                    f"for {' '.join(final_result.command)}: "
                    f"{baseline_result.tests_run} -> {final_result.tests_run}"
                )
            current_results = [result for result in final_results if result.revision == workspace.revision]
            developer_review = analyze_developer_review(working_root, changes.paths)
            final_diff = _diff(working_root, workspace)
            if review is not None:
                _support_acceptance_criteria(
                    task,
                    repository,
                    understanding,
                    changes,
                    verification,
                    current_results,
                    review,
                    developer_review,
                    implementation,
                    final_diff,
                    working_root,
                )
            contradicted_criteria = [
                criterion.description
                for criterion in task.acceptance_criteria
                if criterion.required and criterion.status is AcceptanceStatus.CONTRADICTED
            ]
            unsupported_criteria = [
                criterion.description
                for criterion in task.acceptance_criteria
                if criterion.required and criterion.status is not AcceptanceStatus.SATISFIED
            ]
            if not current_results or any(not result.passed for result in current_results):
                lifecycle.transition(AgentState.PARTIALLY_VERIFIED)
                outcome = Outcome.PARTIALLY_VERIFIED
            elif broad_results and any(not result.passed for result in broad_results):
                lifecycle.transition(AgentState.PARTIALLY_VERIFIED)
                outcome = Outcome.PARTIALLY_VERIFIED
            elif task.risk is RiskLevel.HIGH and not broad:
                not_run.append("High-risk change has no discovered broad/static verification capability")
                lifecycle.transition(AgentState.PARTIALLY_VERIFIED)
                outcome = Outcome.PARTIALLY_VERIFIED
            elif contradicted_criteria:
                not_run.append("Required acceptance criteria contradicted by final/baseline evidence: " + "; ".join(contradicted_criteria))
                lifecycle.transition(AgentState.PARTIALLY_VERIFIED)
                outcome = Outcome.PARTIALLY_VERIFIED
            elif unsupported_criteria:
                not_run.append("Required acceptance criteria not satisfied: " + "; ".join(unsupported_criteria))
                lifecycle.transition(AgentState.PARTIALLY_VERIFIED)
                outcome = Outcome.PARTIALLY_VERIFIED
            else:
                lifecycle.transition(AgentState.LEARN)
                self._announce(artifacts, lifecycle)
                for capability in repository.capabilities:
                    memory.store_strategy(f"Use {' '.join(capability.command)} for {capability.kind}", [capability.source])
                lifecycle.transition(AgentState.REPORT)
                self._announce(artifacts, lifecycle)
                lifecycle.transition(AgentState.SUCCESS)
                outcome = Outcome.VERIFIED
        except (OrchestrationError, ProviderError, SafetyError, OSError, ValueError) as exc:
            artifacts.record("blocked", reason=str(exc), state=lifecycle.state)
            not_run.append(str(exc))
            if lifecycle.state not in {AgentState.BLOCKED, AgentState.SUCCESS, AgentState.PARTIALLY_VERIFIED}:
                try:
                    lifecycle.transition(AgentState.BLOCKED)
                except InvalidTransition:
                    lifecycle.state = AgentState.BLOCKED
                    lifecycle.history.append(AgentState.BLOCKED)
            outcome = Outcome.BLOCKED
            changes = _metrics(working_root, workspace.modified_paths)

        result = RunResult(
            outcome=outcome,
            task=task,
            repository=repository,
            run_id=artifacts.run_id,
            run_dir=str(artifacts.root),
            root_cause=understanding.root_cause,
            implementation=implementation,
            changes=changes,
            verification=verification,
            review=review,
            not_run=not_run,
            recommendations=recommendations,
            state_history=lifecycle.history,
            working_root=str(working_root),
            developer_review=developer_review,
        )
        artifacts.verification.write_text(json.dumps(jsonable(verification), indent=2) + "\n", encoding="utf-8")
        artifacts.report.write_text(json.dumps(jsonable(result), indent=2) + "\n", encoding="utf-8")
        artifacts.record("report", outcome=outcome)
        return result


def run_devagent(repository_root: Path | str, requirement: str, provider: ModelProvider, *, verbose: bool = False, status: Callable[[str], None] | None = None) -> tuple[RunResult, str]:
    result = DevAgent(provider, verbose=verbose, status=status).run(repository_root, requirement)
    return result, render_report(result)
