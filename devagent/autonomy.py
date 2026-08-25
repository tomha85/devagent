from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from devagent.config import load_config, load_role_configs
from devagent.discovery import discover_repository
from devagent.orchestrator import DevAgent
from devagent.providers import ModelProvider, create_provider
from devagent.routing import create_routed_provider
from devagent.skills import SkillAwareProvider, SkillRegistry


_MAX_PARALLEL = 4
_MAX_TASKS = 16


class AutonomyError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentTask:
    id: str
    requirement: str


@dataclass(frozen=True)
class AgentTaskResult:
    id: str
    outcome: str
    run_id: str
    working_root: str
    error: str | None = None


class AgentRunner(Protocol):
    def __call__(self, task: AgentTask) -> AgentTaskResult: ...


class WorktreePool:
    """Bound parallel work without allowing unbounded agent fan-out."""

    def __init__(self, capacity: int) -> None:
        if not 1 <= capacity <= _MAX_PARALLEL:
            raise ValueError(f"parallel capacity must be between 1 and {_MAX_PARALLEL}")
        self.capacity = capacity
        self._semaphore = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._peak = 0

    @property
    def peak_active(self) -> int:
        with self._lock:
            return self._peak

    def run(self, lease_id: str, operation: Callable[[], AgentTaskResult]) -> AgentTaskResult:
        if not lease_id or len(lease_id) > 128:
            raise ValueError("lease id must be non-empty and at most 128 characters")
        with self._semaphore:
            with self._lock:
                if lease_id in self._active:
                    raise AutonomyError(f"duplicate active worktree lease: {lease_id}")
                self._active.add(lease_id)
                self._peak = max(self._peak, len(self._active))
            try:
                return operation()
            finally:
                with self._lock:
                    self._active.remove(lease_id)


def _git_clean_head(root: Path) -> str:
    repository = discover_repository(root, probe_capabilities=False)
    if not repository.git_head:
        raise AutonomyError("parallel autonomy requires a Git worktree with a valid HEAD")
    if repository.dirty_files:
        raise AutonomyError(
            "parallel autonomy requires a clean repository; existing source changes are never shared across agents"
        )
    return repository.git_head


class ParallelAgentCoordinator:
    def __init__(
        self,
        repository_root: Path | str,
        *,
        max_parallel: int = 2,
        provider_factory: Callable[[], ModelProvider] | None = None,
        runner: AgentRunner | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).expanduser().resolve()
        self.pool = WorktreePool(max_parallel)
        self.provider_factory = provider_factory
        self.runner = runner

    def _default_provider(self) -> ModelProvider:
        if self.provider_factory is not None:
            provider = self.provider_factory()
        else:
            default = load_config()
            provider = create_routed_provider(
                default,
                load_role_configs(),
                provider_factory=create_provider,
            )
        registry = SkillRegistry.discover(self.repository_root)
        return SkillAwareProvider(provider, registry)

    def _run_one(self, task: AgentTask) -> AgentTaskResult:
        if self.runner is not None:
            return self.runner(task)
        provider = self._default_provider()
        result = DevAgent(provider, isolate=True).run(self.repository_root, task.requirement)
        working = Path(result.working_root).resolve()
        if working == self.repository_root or not result.repository.git_head:
            raise AutonomyError("parallel agent did not obtain an isolated Git worktree")
        return AgentTaskResult(
            id=task.id,
            outcome=result.outcome.value,
            run_id=result.run_id,
            working_root=str(working),
        )

    def run(self, tasks: Iterable[AgentTask]) -> tuple[AgentTaskResult, ...]:
        task_list = list(tasks)
        if not task_list:
            return ()
        if len(task_list) > _MAX_TASKS:
            raise AutonomyError(f"at most {_MAX_TASKS} tasks may be submitted in one parallel batch")
        ids = [task.id for task in task_list]
        if len(set(ids)) != len(ids) or any(not item.strip() for item in ids):
            raise AutonomyError("parallel task ids must be unique and non-empty")
        if any(not task.requirement.strip() for task in task_list):
            raise AutonomyError("parallel task requirements must be non-empty")

        _git_clean_head(self.repository_root)
        indexed = {task.id: index for index, task in enumerate(task_list)}
        results: list[AgentTaskResult] = []
        with ThreadPoolExecutor(max_workers=self.pool.capacity, thread_name_prefix="devagent") as executor:
            futures = {
                executor.submit(
                    self.pool.run,
                    task.id,
                    lambda item=task: self._run_one(item),
                ): task
                for task in task_list
            }
            for future in as_completed(futures):
                task = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(
                        AgentTaskResult(
                            id=task.id,
                            outcome="BLOCKED",
                            run_id="",
                            working_root="",
                            error=str(exc),
                        )
                    )
        results.sort(key=lambda item: indexed[item.id])
        return tuple(results)


def _load_tasks(path: Path) -> tuple[AgentTask, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("task file must contain a JSON array")
    tasks: list[AgentTask] = []
    for raw in payload:
        if not isinstance(raw, dict) or set(raw) != {"id", "requirement"}:
            raise ValueError("each task must contain exactly id and requirement")
        tasks.append(AgentTask(id=str(raw["id"]).strip(), requirement=str(raw["requirement"]).strip()))
    return tuple(tasks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded batch of isolated DevAgent tasks in parallel")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--tasks", type=Path, required=True, help="JSON array of {id, requirement}")
    parser.add_argument("--max-parallel", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        tasks = _load_tasks(args.tasks)
        results = ParallelAgentCoordinator(args.repo, max_parallel=args.max_parallel).run(tasks)
    except (AutonomyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"DevAgent autonomy failed: {exc}")
        return 1
    print(json.dumps([result.__dict__ for result in results], indent=2))
    return 0 if all(result.outcome == "VERIFIED" for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
