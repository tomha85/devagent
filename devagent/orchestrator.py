from __future__ import annotations

import json
import difflib
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from devagent.artifacts import RunArtifacts
from devagent.discovery import discover_repository
from devagent.memory import RepositoryMemory
from devagent.models import (
    AgentState,
    ChangeMetrics,
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
)
from devagent.providers import ModelProvider, ProviderError
from devagent.report import render_report
from devagent.retrieval import retrieve_context
from devagent.safety import SafetyError
from devagent.state_machine import InvalidTransition, Lifecycle
from devagent.tasking import compile_task
from devagent.workspace import Workspace
from devagent.worktree import select_worktree


UNDERSTANDING_SCHEMA = {
    "type": "object",
    "required": ["problem", "expected_behavior", "affected_paths", "root_cause", "evidence", "proposed_solution", "confidence"],
}
PLAN_SCHEMA = {"type": "object", "required": ["files_to_inspect", "implementation", "verification", "rationale"]}
IMPLEMENT_SCHEMA = {"type": "object", "required": ["actions", "summary"]}
DIAGNOSE_SCHEMA = {"type": "object", "required": ["decision", "updated_hypothesis", "actions"]}
REVIEW_SCHEMA = {"type": "object", "required": ["approved", "issues", "summary"]}


class OrchestrationError(RuntimeError):
    pass


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise OrchestrationError(f"Provider field '{field}' must be a list of non-empty strings")
    return [item.strip() for item in value]


def _understanding(response: dict[str, Any]) -> Understanding:
    try:
        raw_evidence = response["evidence"]
        if not isinstance(raw_evidence, list):
            raise TypeError
        evidence = [
            Evidence(str(item["statement"]), tuple(_strings(item["paths"], "evidence.paths")), float(item.get("confidence", 1.0)))
            for item in raw_evidence
            if isinstance(item, dict)
        ]
        return Understanding(
            problem=str(response["problem"]).strip(),
            expected_behavior=str(response["expected_behavior"]).strip(),
            affected_paths=_strings(response["affected_paths"], "affected_paths"),
            root_cause=str(response["root_cause"]).strip(),
            evidence=evidence,
            proposed_solution=_strings(response["proposed_solution"], "proposed_solution"),
            confidence=float(response["confidence"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OrchestrationError(f"Invalid understanding response: {exc}") from exc


def _plan(response: dict[str, Any], fallback: Sequence[tuple[str, ...]]) -> EngineeringPlan:
    try:
        commands: list[tuple[str, ...]] = []
        raw_commands = response["verification"]
        if not isinstance(raw_commands, list):
            raise TypeError("verification must be a list")
        for command in raw_commands:
            if not isinstance(command, list) or not all(isinstance(token, str) and token for token in command):
                raise TypeError("verification commands must be argv arrays")
            commands.append(tuple(command))
        return EngineeringPlan(
            _strings(response["files_to_inspect"], "files_to_inspect"),
            _strings(response["implementation"], "implementation"),
            commands or list(fallback),
            str(response["rationale"]).strip(),
        )
    except (KeyError, TypeError) as exc:
        raise OrchestrationError(f"Invalid plan response: {exc}") from exc


def _review(response: dict[str, Any]) -> ReviewDecision:
    try:
        if not isinstance(response["approved"], bool) or not isinstance(response["issues"], list):
            raise TypeError
        issues = [
            ReviewIssue(str(item["severity"]), str(item["reason"]), item.get("path"))
            for item in response["issues"]
            if isinstance(item, dict) and "severity" in item and "reason" in item
        ]
        approved = response["approved"]
        if approved and issues:
            raise OrchestrationError("Reviewer cannot approve while returning issues")
        return ReviewDecision(approved, issues, str(response.get("summary", "")).strip())
    except (KeyError, TypeError) as exc:
        raise OrchestrationError("Invalid review response") from exc


def _execute_actions(workspace: Workspace, response: dict[str, Any], allowed_paths: set[str]) -> list[str]:
    actions = response.get("actions")
    if not isinstance(actions, list) or not actions:
        raise OrchestrationError("Implementation requires at least one structured action")
    changed: list[str] = []
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"tool", "arguments"} or not isinstance(action["arguments"], dict):
            raise OrchestrationError("Every action must contain only tool and arguments")
        tool, arguments = action["tool"], action["arguments"]
        if tool not in {"replace_text", "write_file"}:
            raise OrchestrationError(f"Implementation tool is not allowed: {tool}")
        path = arguments.get("path")
        if not isinstance(path, str) or path not in allowed_paths:
            raise OrchestrationError(f"Action path was not inspected/planned: {path}")
        if tool == "replace_text":
            required = {"path", "old", "new"}
            if not required.issubset(arguments) or not all(isinstance(arguments[key], str) for key in required):
                raise OrchestrationError("replace_text requires string path, old, and new")
            workspace.replace_text(path, arguments["old"], arguments["new"], int(arguments.get("count", 1)))
        else:
            if set(arguments) != {"path", "content"} or not isinstance(arguments.get("content"), str):
                raise OrchestrationError("write_file requires only string path and content")
            workspace.write_file(path, arguments["content"])
        changed.append(path)
    return changed


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
        destination = broad if capability.broad or capability.kind in {"build", "lint", "typecheck", "integration"} else targeted
        if capability.command not in destination:
            destination.append(capability.command)
    if not targeted and any("python" in component.languages for component in repository.components):
        targeted.append(("python", "-m", "compileall", "-q", "."))
    return targeted[:3], broad[:5]


def _command_kind(command: tuple[str, ...], repository: Any) -> str | None:
    for capability in repository.capabilities:
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


def _support_acceptance_criteria(task: Any, changes: ChangeMetrics, final_results: list[VerificationResult], review: ReviewDecision) -> None:
    passing_commands = [" ".join(result.command) for result in final_results if result.passed]
    for criterion in task.acceptance_criteria:
        lowered = criterion.description.lower()
        if "review" in lowered and review.approved:
            criterion.evidence.append("Independent reviewer approved the final diff")
        elif "test" in lowered or "coverage" in lowered:
            criterion.evidence.extend(path for path in changes.paths if _is_test_path(path))
            criterion.evidence.extend(passing_commands)
        elif "unrelated" in lowered:
            criterion.evidence.append(f"Minimal-diff gate accepted {changes.files_changed} changed file(s)")
        elif changes.paths:
            criterion.evidence.extend(changes.paths)


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    parts = Path(lowered).parts
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)
        or Path(lowered).name.startswith("test_")
        or ".test." in lowered
        or ".spec." in lowered
    )


def _run_commands(workspace: Workspace, commands: Sequence[tuple[str, ...]], phase: str, *, baseline: bool = False) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    for command in commands:
        try:
            results.append(workspace.run(command, phase=phase, baseline=baseline))
        except (SafetyError, OSError) as exc:
            raise OrchestrationError(f"Cannot run {' '.join(command)}: {exc}") from exc
    return results


class DevAgent:
    """Deterministic orchestration; model reasoning is bounded inside named states."""

    def __init__(self, provider: ModelProvider, *, max_corrections: int = 2, isolate: bool = True, verbose: bool = False, status: Callable[[str], None] | None = None) -> None:
        self.provider = provider
        self.max_corrections = max(0, min(max_corrections, 5))
        self.isolate = isolate
        self.verbose = verbose
        self.status = status or (lambda message: None)

    def _announce(self, artifacts: RunArtifacts, lifecycle: Lifecycle) -> None:
        artifacts.record("state", state=lifecycle.state)
        if self.verbose:
            self.status(lifecycle.state.value)

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
        source_repository = discover_repository(root)
        selection = select_worktree(root, artifacts.run_id, enabled=self.isolate, git_head=source_repository.git_head, dirty_files=source_repository.dirty_files)
        working_root = selection.root
        repository = discover_repository(working_root)
        repository.root = str(root)
        repository.git_branch = source_repository.git_branch
        repository.git_head = source_repository.git_head
        repository.dirty_files = source_repository.dirty_files
        workspace = Workspace(working_root, artifacts, source_repository.dirty_files if not selection.isolated else ())
        artifacts.write_json("metadata.json", {"run_id": artifacts.run_id, "task": task, "repository": repository})
        artifacts.record("worktree", source=str(root), working_root=str(working_root), isolated=selection.isolated, reason=selection.reason)
        self._announce(artifacts, lifecycle)

        try:
            lifecycle.transition(AgentState.DISCOVER)
            self._announce(artifacts, lifecycle)
            memory = RepositoryMemory(root)
            memory.load_facts()
            memory.store_facts(repository.facts)

            lifecycle.transition(AgentState.UNDERSTAND)
            self._announce(artifacts, lifecycle)
            context = retrieve_context(workspace, repository, task.goal)
            for context_attempt in range(2):
                response = self.provider.request(role="understand", payload={"task": jsonable(task), "repository": context}, schema=UNDERSTANDING_SCHEMA)
                understanding = _understanding(response)
                if understanding.implementation_ready(working_root) and all((working_root / path).is_file() for evidence in understanding.evidence for path in evidence.paths):
                    break
                if context_attempt == 0:
                    lifecycle.transition(AgentState.GATHER_CONTEXT)
                    self._announce(artifacts, lifecycle)
                    context = retrieve_context(workspace, repository, task.goal + " " + " ".join(understanding.affected_paths), max_chars=32_000)
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
                payload={"task": jsonable(task), "understanding": jsonable(understanding), "capabilities": jsonable(repository.capabilities)},
                schema=PLAN_SCHEMA,
            )
            plan = _plan(plan_response, targeted)
            unsupported = [command for command in plan.verification if _command_kind(command, repository) is None]
            if unsupported:
                raise OrchestrationError(f"Plan requested verification not supported by repository evidence: {' '.join(unsupported[0])}")
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
            implementation.extend(_strings(implement_response.get("summary", []), "summary") if isinstance(implement_response.get("summary"), list) else [str(implement_response.get("summary", "Implemented planned change"))])

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
                        payload={"task": jsonable(task), "understanding": jsonable(understanding), "failures": jsonable(target_results), "failed_hypotheses": recommendations},
                        schema=PLAN_SCHEMA,
                    )
                    plan = _plan(replanned, targeted)
                    if any(_command_kind(command, repository) is None for command in plan.verification):
                        raise OrchestrationError("Replan selected verification unsupported by repository evidence")
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
            if task.requires_tests and task.task_type not in {TaskType.TEST_FAILURE, TaskType.UNIT_TEST} and not any(
                _is_test_path(path) for path in changes.paths
            ):
                raise OrchestrationError("Required regression/feature coverage was not added or updated")
            quality_commands = [("git", "diff", "--check")] if repository.git_head else []
            quality_results = _run_commands(workspace, quality_commands, "quality")
            verification.extend(quality_results)
            if any(not result.passed for result in quality_results):
                raise OrchestrationError("git diff --check rejected the patch")

            lifecycle.transition(AgentState.FINAL_VERIFY)
            self._announce(artifacts, lifecycle)
            final_commands = list(dict.fromkeys([*(plan.verification or targeted), *broad, *quality_commands]))
            final_results = _run_commands(workspace, final_commands, "final")
            verification.extend(final_results)
            current_results = [result for result in final_results if result.revision == workspace.revision]
            if review is not None:
                _support_acceptance_criteria(task, changes, current_results, review)
            unsupported_criteria = [criterion.description for criterion in task.acceptance_criteria if criterion.required and not criterion.evidence]
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
            elif unsupported_criteria:
                not_run.append("Acceptance criteria without final evidence: " + "; ".join(unsupported_criteria))
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
        )
        artifacts.verification.write_text(json.dumps(jsonable(verification), indent=2) + "\n", encoding="utf-8")
        artifacts.report.write_text(json.dumps(jsonable(result), indent=2) + "\n", encoding="utf-8")
        artifacts.record("report", outcome=outcome)
        return result


def run_devagent(repository_root: Path | str, requirement: str, provider: ModelProvider, *, verbose: bool = False, status: Callable[[str], None] | None = None) -> tuple[RunResult, str]:
    result = DevAgent(provider, verbose=verbose, status=status).run(repository_root, requirement)
    return result, render_report(result)
