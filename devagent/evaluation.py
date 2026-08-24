from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from devagent.models import Outcome, RunResult
from devagent.orchestrator import DevAgent
from devagent.providers import ModelProvider


@dataclass(frozen=True)
class EvaluationMetrics:
    task_success: bool
    acceptance_criteria_supported: int
    acceptance_criteria_total: int
    new_regressions: int | None
    files_changed: int
    lines_changed: int
    iterations: int
    model_calls: int
    tool_calls: int
    runtime_seconds: float
    outcome: Outcome


def evaluate(repository: Path, requirement: str, provider: ModelProvider, *, isolate: bool = True) -> tuple[RunResult, EvaluationMetrics]:
    """Run one deterministic fixture evaluation without any paid model requirement."""
    started = time.monotonic()
    result = DevAgent(provider, isolate=isolate).run(repository, requirement)
    runtime = time.monotonic() - started
    supported = sum(bool(criterion.evidence) for criterion in result.task.acceptance_criteria)
    observations = Path(result.run_dir) / "observations.jsonl"
    tool_calls = 0
    if observations.is_file():
        for line in observations.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line).get("event")
            except json.JSONDecodeError:
                continue
            if event in {"command_finished", "file_written", "text_replaced"}:
                tool_calls += 1
    model_calls = len(getattr(provider, "calls", []))
    metrics = EvaluationMetrics(
        task_success=result.outcome is Outcome.VERIFIED,
        acceptance_criteria_supported=supported,
        acceptance_criteria_total=len(result.task.acceptance_criteria),
        new_regressions=0 if result.outcome is Outcome.VERIFIED else None,
        files_changed=result.changes.files_changed,
        lines_changed=result.changes.lines_added + result.changes.lines_deleted,
        iterations=result.state_history.count(result.state_history[-1]) + sum(state.value == "DIAGNOSE" for state in result.state_history),
        model_calls=model_calls,
        tool_calls=tool_calls,
        runtime_seconds=runtime,
        outcome=result.outcome,
    )
    return result, metrics

