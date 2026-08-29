from __future__ import annotations

from pathlib import Path
from typing import Mapping

from devagent.providers import ModelProvider

from .advanced_assistant import (
    advanced_observation_map,
    diagnose_advanced_target,
    required_advanced_tag_ids,
    resolve_advanced_target,
)
from .advanced_semantics import LiveAdvancedKind, build_live_advanced_coverage
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
from .history import LiveHistoryCollector, is_historical_question, requested_history_seconds
from .manager import MultiPlcConnectionManager, PlcConnectionSpec
from .qa import answer_commissioning_question
from .reconciled_evidence import build_reconciled_live_agent_evidence
from .recursive_diagnosis import (
    DEFAULT_TRACE_MAX_DEPTH,
    DEFAULT_TRACE_MAX_NODES,
    required_tag_ids_for_recursive_output,
    trace_recursive_diagnosis,
)
from .stateful_assistant import (
    render_stateful_diagnosis,
    required_stateful_tag_ids,
    resolve_stateful_model,
    stateful_observation_map,
)
from .stateful_context import build_live_stateful_coverage, diagnose_live_stateful_model


class RecursiveLiveCommissioningAssistant(LiveCommissioningAssistant):
    """Onsite assistant with bounded deterministic upstream and historical tracing.

    DevAgent PLC remains the read-only engineering authority. This class owns onsite
    state only: exact tag reconciliation, trusted live evidence, bounded recursive
    blocker tracing, stateful/sequence context, advanced commissioning semantics,
    and an optional bounded timeline.
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
        history_seconds: float = 0.0,
        history_poll_seconds: float = 1.0,
        history_max_tags: int = 64,
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
        if history_seconds < 0:
            raise ValueError("history_seconds must be >= 0")
        if history_poll_seconds <= 0:
            raise ValueError("history_poll_seconds must be > 0")
        if history_max_tags < 1 or history_max_tags > 256:
            raise ValueError("history_max_tags must be between 1 and 256")
        self.trace_max_depth = trace_max_depth
        self.trace_max_nodes = trace_max_nodes
        self.history_seconds = float(history_seconds)
        self.history_poll_seconds = float(history_poll_seconds)
        self.history_max_tags = history_max_tags
        self.history_collector: LiveHistoryCollector | None = None
        self.stateful_coverage = build_live_stateful_coverage(self.loaded.project)
        self.advanced_coverage = build_live_advanced_coverage(self.loaded.project, self.context)

    def _preferred_history_tag_ids(self) -> tuple[str, ...]:
        """Rank diagnostic signals ahead of unrelated mapped tags in bounded history."""
        result: list[str] = []

        def add_reference(reference: str) -> None:
            tag = self.context.unique_tag_for_reference(reference)
            if tag is not None and tag.id not in result:
                result.append(tag.id)

        # Outputs first because engineers commonly ask why an output stopped/changed.
        for rule in self.context.rules:
            add_reference(rule.output_tag)
        # Then direct permissive/interlock dependencies.
        for rule in self.context.rules:
            for path in rule.paths:
                for term in path.terms:
                    add_reference(term.tag_reference)
        # Stateful state/guard dependencies.
        for model in self.stateful_coverage.models:
            for tag_id in required_stateful_tag_ids(self.context, model):
                if tag_id not in result:
                    result.append(tag_id)
        # Advanced numeric, handshake, one-shot/latch, AOI/FB, fault, motion and PID context.
        for comparison in self.advanced_coverage.numeric_comparisons:
            for reference in comparison.references:
                add_reference(reference)
        for model in self.advanced_coverage.models:
            for reference in model.references:
                add_reference(reference)
        return tuple(result)

    async def _start_history(self) -> None:
        if self.history_seconds <= 0 or self.reconciliation is None:
            return
        if self.history_collector is not None:
            await self.history_collector.stop()
        self.history_collector = LiveHistoryCollector(
            self.manager,
            self.reconciliation,
            retention_seconds=self.history_seconds,
            poll_interval_seconds=self.history_poll_seconds,
            max_tags=self.history_max_tags,
            preferred_tag_ids=self._preferred_history_tag_ids(),
        )
        await self.history_collector.start()

    async def start(self):
        status = await super().start()
        await self._start_history()
        return status

    async def refresh_mapping(self):
        reconciliation = await super().refresh_mapping()
        await self._start_history()
        return reconciliation

    async def close(self):
        if self.history_collector is not None:
            try:
                await self.history_collector.stop()
            finally:
                self.history_collector = None
        return await super().close()

    def _overview_text(self) -> str:
        reply = self.system_overview()
        history = self.history_collector
        lines = [
            reply.render_text(),
            f"Recursive trace max depth: {self.trace_max_depth}",
            f"Recursive trace max nodes: {self.trace_max_nodes}",
            (
                f"Stateful models: {len(self.stateful_coverage.models)} "
                f"(timers={self.stateful_coverage.timers}, counters={self.stateful_coverage.counters}, "
                f"state_machines={self.stateful_coverage.state_machines})"
            ),
            (
                "Advanced semantics: "
                f"numeric={self.advanced_coverage.count(LiveAdvancedKind.NUMERIC_COMPARISON)} "
                f"oneshot={self.advanced_coverage.count(LiveAdvancedKind.ONE_SHOT)} "
                f"latch={self.advanced_coverage.count(LiveAdvancedKind.LATCH)} "
                f"handshake={self.advanced_coverage.count(LiveAdvancedKind.HANDSHAKE)} "
                f"aoi_fb={self.advanced_coverage.count(LiveAdvancedKind.AOI_FB)} "
                f"fault_code={self.advanced_coverage.count(LiveAdvancedKind.FAULT_CODE)} "
                f"sequencer={self.advanced_coverage.count(LiveAdvancedKind.SEQUENCER)} "
                f"motion={self.advanced_coverage.count(LiveAdvancedKind.MOTION)} "
                f"pid={self.advanced_coverage.count(LiveAdvancedKind.PID)}"
            ),
            (
                f"Historical timeline: ENABLED retention={self.history_seconds:g}s "
                f"poll={self.history_poll_seconds:g}s max_tags={self.history_max_tags} "
                f"captured_tags={len(history.captured_tag_ids) if history else 0} "
                f"cycles={history.cycles if history else 0}"
                if self.history_seconds > 0
                else "Historical timeline: OFF"
            ),
        ]
        if history is not None and history.last_error:
            lines.append(f"Historical collector last limitation: {history.last_error}")
        return "\n".join(lines)

    async def _historical_reply(self, text: str) -> LiveAssistantReply | None:
        if not is_historical_question(text):
            return None
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
        if self.history_collector is None:
            return LiveAssistantReply(
                question=text,
                kind=LiveAssistantReplyKind.LIMITATION,
                text=(
                    "Historical diagnosis is not available for this session. Start `devagent live assist` "
                    "with a positive history retention window and allow the session to observe the system before asking past-event questions."
                ),
                target_output=output,
            )
        dependency_ids = required_tag_ids_for_recursive_output(
            self.context,
            output,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        window = min(
            requested_history_seconds(text),
            self.history_seconds,
        )
        diagnosis = self.history_collector.store.diagnose_recent_transition(
            self.context,
            output,
            dependency_tag_ids=dependency_ids,
            lookback_seconds=window,
        )
        return LiveAssistantReply(
            question=text,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=diagnosis.render_text(),
            target_output=output,
        )

    async def _stateful_reply(self, text: str) -> LiveAssistantReply | None:
        model = resolve_stateful_model(self.stateful_coverage.models, text)
        if model is None:
            return None
        required_ids = required_stateful_tag_ids(self.context, model)
        observations: dict[str, object] = {}
        if required_ids and self.reconciliation is not None:
            try:
                reconciled = await build_reconciled_live_agent_evidence(
                    self.manager,
                    self.reconciliation,
                    required_tag_ids=required_ids,
                    require_all=False,
                )
                observations = stateful_observation_map(reconciled)
            except LiveConfigurationError:
                observations = {}
        diagnosis = diagnose_live_stateful_model(model, observations)
        return LiveAssistantReply(
            question=text,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=render_stateful_diagnosis(diagnosis),
            target_output=model.name,
        )

    async def _advanced_reply(self, text: str) -> LiveAssistantReply | None:
        target = resolve_advanced_target(self.advanced_coverage, text)
        if not target.found:
            return None
        required_ids = required_advanced_tag_ids(self.context, target)
        observations = {}
        if required_ids and self.reconciliation is not None:
            try:
                reconciled = await build_reconciled_live_agent_evidence(
                    self.manager,
                    self.reconciliation,
                    required_tag_ids=required_ids,
                    require_all=False,
                )
                observations = advanced_observation_map(reconciled)
            except LiveConfigurationError:
                observations = {}
        history = self.history_collector.store if self.history_collector is not None else None
        diagnosis = diagnose_advanced_target(
            self.context,
            self.advanced_coverage,
            target,
            observations,
            history=history,
        )
        if diagnosis is None:
            return None
        return LiveAssistantReply(
            question=text,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=diagnosis.render_text(),
            target_output=target.name,
        )

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
            return LiveAssistantReply(
                question=text,
                kind=LiveAssistantReplyKind.SYSTEM_OVERVIEW,
                text=self._overview_text(),
            )

        historical = await self._historical_reply(text)
        if historical is not None:
            return historical

        stateful = await self._stateful_reply(text)
        if stateful is not None:
            return stateful

        advanced = await self._advanced_reply(text)
        if advanced is not None:
            return advanced

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
        if diagnosis.source_locators:
            combined += "\n\nTarget PLC source:"
            combined += "".join(f"\n- {locator}" for locator in diagnosis.source_locators)
        if recursive.roots or recursive.limitations:
            combined += "\n\n" + recursive.render_text()

        if len(recursive.roots) == 1:
            immediate = recursive.roots[0].signal
            if self.context.rules_for_output(immediate):
                self._last_target = immediate

        return LiveAssistantReply(
            question=text,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=combined,
            target_output=output,
            diagnosis=diagnosis,
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
    history_seconds: float = 0.0,
    history_poll_seconds: float = 1.0,
    history_max_tags: int = 64,
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
        history_seconds=history_seconds,
        history_poll_seconds=history_poll_seconds,
        history_max_tags=history_max_tags,
    )


__all__ = [
    "RecursiveLiveCommissioningAssistant",
    "create_recursive_live_commissioning_assistant",
]
