from __future__ import annotations

from dataclasses import dataclass, field

from devagent.models import AgentState


class InvalidTransition(RuntimeError):
    pass


_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.PREFLIGHT: {AgentState.DISCOVER, AgentState.BLOCKED},
    AgentState.DISCOVER: {AgentState.UNDERSTAND, AgentState.BLOCKED},
    AgentState.UNDERSTAND: {AgentState.TASK_SPEC, AgentState.GATHER_CONTEXT, AgentState.BLOCKED},
    AgentState.TASK_SPEC: {AgentState.BASELINE, AgentState.BLOCKED},
    AgentState.BASELINE: {AgentState.PLAN, AgentState.BLOCKED},
    AgentState.PLAN: {AgentState.GATHER_CONTEXT, AgentState.BLOCKED},
    AgentState.GATHER_CONTEXT: {AgentState.UNDERSTAND, AgentState.REPRODUCE, AgentState.PLAN, AgentState.BLOCKED},
    AgentState.REPRODUCE: {AgentState.IMPLEMENT, AgentState.GATHER_CONTEXT, AgentState.BLOCKED},
    AgentState.IMPLEMENT: {AgentState.VERIFY_TARGETED, AgentState.BLOCKED},
    AgentState.VERIFY_TARGETED: {AgentState.DIAGNOSE, AgentState.VERIFY_BROAD, AgentState.BLOCKED},
    AgentState.DIAGNOSE: {AgentState.PLAN, AgentState.IMPLEMENT, AgentState.BLOCKED},
    AgentState.VERIFY_BROAD: {AgentState.REVIEW, AgentState.DIAGNOSE, AgentState.PARTIALLY_VERIFIED},
    AgentState.REVIEW: {AgentState.IMPLEMENT, AgentState.QUALITY_CHECK, AgentState.BLOCKED},
    AgentState.QUALITY_CHECK: {AgentState.IMPLEMENT, AgentState.FINAL_VERIFY, AgentState.BLOCKED},
    AgentState.FINAL_VERIFY: {AgentState.LEARN, AgentState.DIAGNOSE, AgentState.PARTIALLY_VERIFIED},
    AgentState.LEARN: {AgentState.REPORT},
    AgentState.REPORT: {AgentState.SUCCESS, AgentState.PARTIALLY_VERIFIED, AgentState.BLOCKED},
    AgentState.SUCCESS: set(),
    AgentState.PARTIALLY_VERIFIED: set(),
    AgentState.BLOCKED: set(),
}


@dataclass
class Lifecycle:
    state: AgentState = AgentState.PREFLIGHT
    history: list[AgentState] = field(default_factory=lambda: [AgentState.PREFLIGHT])

    def transition(self, target: AgentState) -> None:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidTransition(f"Invalid DevAgent transition: {self.state.value} -> {target.value}")
        self.state = target
        self.history.append(target)
