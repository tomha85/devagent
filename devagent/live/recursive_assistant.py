from __future__ import annotations

from pathlib import Path
from typing import Mapping

from devagent.providers import ModelProvider

from .assistant import (
    LiveAssistantReply,
    LiveAssistantReplyKind,
    LiveCommissioningAssistant,
)
from .control_guard import is_plc_control_request
from .diagnosis import (
    LiveDiagnosisStatus,
    LiveObservedTag,
    observations_from_reconciled,
    resolve_question_target,
)
from .diagnosis_guard import diagnose_output
from .engineering_context import LiveLoadedEngineering, ProjectLoader, load_live_engineering_context
from .errors import LiveConfigurationError
from .manager import MultiPlcConnectionManager, PlcConnectionSpec
from .qa import answer_commissioning_question
from .reconciled_evidence import build_reconciled_live_agent_evidence
from .recursive_diagnosis import (
    DEFAULT_TRACE_MAX_DEPTH,
    DEFAULT_TRACE_MAX_NODES,
    required_tag_ids_for_recursive_output,
    trace_recursive_diagnosis,
)


class RecursiveLiveCommissioningAssistant(LiveCommissioningAssistant):
    """Onsite assistant with bounded deterministic upstream root-cause tracing.

    This extends the V1 assistant without changing DevAgent PLC. It reads a bounded
    closure of reconciled OPC UA tags, applies the existing trust gate, then recursively
    follows only canonical FULL stateless Boolean rules. Unsupported/stateful/cyclic
    logic stops fail-closed at the affected signal.
    """

    def __init__(
        self,
        loaded: LiveLoadedEngineering,
        connection: PlcConnectionSpec,
        *,
        manager: MultiPlcConnectionManager | None = None,
        explicit_node_map: Mapping[str, str] | None = None,
        browse_max_depth: int = 4,
        browse_max_nodes: int = 500,
        provider: ModelProvider | None = None,
        trace_max_depth: int = DEFAULT_TRACE_MAX_DEPTH,
        trace_max_nodes: int = DEFAULT_TRACE_MAX_NODES,
    ) -> None:
        super().__init__(
            loaded,
            connection,
            manager=manager,
            explicit_node_map=explicit_node_map,
            browse_max_depth=browse_max_depth,
            browse_max_nodes=browse_max_nodes,
            provider=provider,
        )
        if trace_max_depth < 0:
            raise ValueError("trace_max_depth must be >= 0")
        if trace_max_nodes < 1:
            raise ValueError("trace_max_nodes must be >= 1")
        self.trace_max_depth = trace_max_depth
        self.trace_max_nodes = trace_max_nodes

    async def answer(self, question: str) -> LiveAssistantReply:
        text = str(question or "").strip()
        if not text:
            return self._target_limitation_reply(
                text,
                status=LiveDiagnosisStatus.TARGET_NOT_FOUND,
                candidates=(),
                detail="Question is empty.",
            )
        if is_plc_control_request(text):
            return self._control_refusal_reply(text)
        if not self.connected or self.reconciliation is None:
            await self.start()

        if self._is_overview_question(text):
            reply = self.system_overview()
            overview = (
                reply.render_text()
                + f"\nRecursive trace max depth: {self.trace_max_depth}"
                + f"\nRecursive trace max nodes: {self.trace_max_nodes}"
            )
            return LiveAssistantReply(
                question=text,
                kind=LiveAssistantReplyKind.SYSTEM_OVERVIEW,
                text=overview,
            )

        target = resolve_question_target(self.context, text)
        output = target.output_tag
        if output is None and self._can_reuse_last_target(text):
            output = self._last_target
        if output is None:
            return self._target_limitation_reply(
                text,
                status=target.status or LiveDiagnosisStatus.TARGET_NOT_FOUND,
                candidates=target.candidates,
                detail=target.detail,
            )

        self._last_target = output
        required_tag_ids = required_tag_ids_for_recursive_output(
            self.context,
            output,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        observations: tuple[LiveObservedTag, ...]
        if required_tag_ids:
            try:
                reconciled = await build_reconciled_live_agent_evidence(
                    self.manager,
                    self.reconciliation,
                    required_tag_ids=required_tag_ids,
                    require_all=False,
                )
                observations = observations_from_reconciled(reconciled)
            except LiveConfigurationError:
                observations = self._mapping_only_observations()
        else:
            observations = self._mapping_only_observations()

        diagnosis = diagnose_output(self.context, output, observations)
        answer = answer_commissioning_question(
            text,
            diagnosis,
            provider=self.provider,
        )
        recursive = trace_recursive_diagnosis(
            self.context,
            diagnosis,
            observations,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        combined = answer.render_text()
        if recursive.roots or recursive.limitations:
            combined += "\n\n" + recursive.render_text()
        return LiveAssistantReply(
            question=text,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=combined,
            target_output=output,
            diagnosis=diagnosis,
            # Keep the combined deterministic root-cause trace authoritative in text.
            # The optional AI answer remains upstream-bounded and cannot replace it.
            answer=None,
        )


def create_recursive_live_commissioning_assistant(
    project_path: Path,
    connection: PlcConnectionSpec,
    *,
    project_loader: ProjectLoader | None = None,
    manager: MultiPlcConnectionManager | None = None,
    explicit_node_map: Mapping[str, str] | None = None,
    browse_max_depth: int = 4,
    browse_max_nodes: int = 500,
    provider: ModelProvider | None = None,
    trace_max_depth: int = DEFAULT_TRACE_MAX_DEPTH,
    trace_max_nodes: int = DEFAULT_TRACE_MAX_NODES,
) -> RecursiveLiveCommissioningAssistant:
    loaded = load_live_engineering_context(
        Path(project_path),
        project_loader=project_loader,
    )
    return RecursiveLiveCommissioningAssistant(
        loaded,
        connection,
        manager=manager,
        explicit_node_map=explicit_node_map,
        browse_max_depth=browse_max_depth,
        browse_max_nodes=browse_max_nodes,
        provider=provider,
        trace_max_depth=trace_max_depth,
        trace_max_nodes=trace_max_nodes,
    )


__all__ = [
    "RecursiveLiveCommissioningAssistant",
    "create_recursive_live_commissioning_assistant",
]
