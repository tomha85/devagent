from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from devagent.providers import ModelProvider

from .control_guard import is_plc_control_request, read_only_control_refusal
from .diagnosis import (
    LiveCommissioningDiagnosis,
    LiveDiagnosisStatus,
    LiveObservedTag,
    observations_from_reconciled,
    required_tag_ids_for_output,
    resolve_question_target,
)
from .diagnosis_guard import diagnose_output
from .engineering_context import (
    LiveLoadedEngineering,
    ProjectLoader,
    load_live_engineering_context,
)
from .errors import LiveConfigurationError
from .manager import (
    ManagedPlcStatus,
    MultiPlcConnectionManager,
    PlcConnectionSpec,
    PlcSessionState,
)
from .qa import LiveCommissioningAnswer, answer_commissioning_question
from .question_resolution import resolve_explicit_tag_reference
from .reconciled_evidence import build_reconciled_live_agent_evidence
from .system_health import is_system_health_question
from .tag_reconciliation import (
    LiveTagReconciliation,
    reconcile_connected_project_tags,
)


class LiveAssistantReplyKind(str, Enum):
    SYSTEM_OVERVIEW = "SYSTEM_OVERVIEW"
    DIAGNOSIS = "DIAGNOSIS"
    LIMITATION = "LIMITATION"


@dataclass(frozen=True)
class LiveAssistantReply:
    question: str
    kind: LiveAssistantReplyKind
    text: str
    target_output: str | None = None
    diagnosis: LiveCommissioningDiagnosis | None = None
    answer: LiveCommissioningAnswer | None = None

    def render_text(self) -> str:
        if self.answer is not None:
            return self.answer.render_text()
        return self.text


_OVERVIEW_PHRASES = (
    "what is this system",
    "understand this system",
    "understand system",
    "system overview",
    "summarize this system",
    "summarise this system",
    "what controller",
    "what plc",
    "what do you know about this system",
    "what do you know about the system",
    "tell me about this system",
    "tell me about the system",
    "describe this system",
    "describe the system",
    "what does this system do",
    "what outputs are available",
    "what are the outputs",
    "what signals are available",
    "what are the signals",
    "what tags are available",
    "what are the tags",
    "what are you monitoring",
    "what can you diagnose",
    "system details",
    "controller overview",
    "he thong nay la gi",
    "cho toi biet ve he thong",
    "hệ thống này là gì",
    "cho tôi biết về hệ thống",
)
_FOLLOWUP_PHRASES = (
    "which interlock",
    "which permissive",
    "what should i check",
    "what do i check",
    "next check",
    "why",
)


class LiveCommissioningAssistant:
    """Read-only onsite assistant combining PLC engineering context with trusted OPC UA data.

    The existing PLC engineering stack is consumed as a read-only dependency. All
    commissioning state, runtime diagnosis, and Q&A behavior lives under devagent.live.
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
    ) -> None:
        if browse_max_depth < 0:
            raise ValueError("browse_max_depth must be >= 0")
        if browse_max_nodes < 1:
            raise ValueError("browse_max_nodes must be >= 1")
        self.loaded = loaded
        self.connection = connection
        self.manager = manager or MultiPlcConnectionManager([connection])
        if set(self.manager.plc_ids) != {connection.plc_id}:
            raise ValueError(
                "LiveCommissioningAssistant manager must contain exactly the configured PLC id"
            )
        self.explicit_node_map = dict(explicit_node_map or {})
        self.browse_max_depth = browse_max_depth
        self.browse_max_nodes = browse_max_nodes
        self.provider = provider
        self.reconciliation: LiveTagReconciliation | None = None
        self._last_target: str | None = None

    @property
    def context(self):
        return self.loaded.context

    @property
    def connected(self) -> bool:
        status = self.manager.status(self.connection.plc_id)
        return status.connected and status.state is PlcSessionState.CONNECTED

    async def start(self) -> ManagedPlcStatus:
        status = await self.manager.connect(self.connection.plc_id)
        if not status.connected or status.state is not PlcSessionState.CONNECTED:
            raise LiveConfigurationError(
                f"PLC {self.connection.plc_id} did not reach CONNECTED"
            )
        self.reconciliation = await reconcile_connected_project_tags(
            self.manager,
            self.connection.plc_id,
            self.loaded.project,
            explicit_node_map=self.explicit_node_map,
            max_depth=self.browse_max_depth,
            max_nodes=self.browse_max_nodes,
        )
        return self.manager.status(self.connection.plc_id)

    async def refresh_mapping(self) -> LiveTagReconciliation:
        if not self.connected:
            await self.start()
        else:
            self.reconciliation = await reconcile_connected_project_tags(
                self.manager,
                self.connection.plc_id,
                self.loaded.project,
                explicit_node_map=self.explicit_node_map,
                max_depth=self.browse_max_depth,
                max_nodes=self.browse_max_nodes,
            )
        assert self.reconciliation is not None
        return self.reconciliation

    async def close(self) -> ManagedPlcStatus:
        return await self.manager.disconnect(self.connection.plc_id)

    def _mapping_counts(self) -> tuple[int, int]:
        if self.reconciliation is None:
            return 0, 0
        accepted = len(self.reconciliation.accepted_mappings())
        unresolved = len(self.reconciliation.unresolved_mappings())
        return accepted, unresolved

    def system_overview(self) -> LiveAssistantReply:
        accepted, unresolved = self._mapping_counts()
        status = self.manager.status(self.connection.plc_id)
        context = self.context
        limitations = len(context.limitations)
        outputs = context.output_names()
        accepted_mappings = (
            self.reconciliation.accepted_mappings()
            if self.reconciliation is not None
            else ()
        )
        unresolved_mappings = (
            self.reconciliation.unresolved_mappings()
            if self.reconciliation is not None
            else ()
        )
        mapped_names = tuple(mapping.tag_name for mapping in accepted_mappings)
        unresolved_names = tuple(mapping.tag_name for mapping in unresolved_mappings)

        lines = [
            "DEVAGENT LIVE SYSTEM MASTER",
            f"Controller: {context.controller_name or self.connection.display_name}",
            f"Vendor: {context.vendor or 'UNKNOWN'}",
            f"Engineering tool: {context.engineering_tool or 'UNKNOWN'}",
            f"Engineering source: {context.source_path or 'UNKNOWN'}",
            f"Full project model: {'YES' if context.full_project else 'NO'}",
            f"Engineering tags: {len(context.tags)}",
            f"Deterministic output rules: {len(context.rules)}",
            f"Logic statements: {len(context.statements)}",
            f"Known deterministic outputs: {len(outputs)}",
            f"OPC UA state: {status.state.value}",
            f"Mapped live tags: {accepted}",
            f"Unresolved live tags: {unresolved}",
            f"Engineering limitations: {limitations}",
            "Mode: READ ONLY",
        ]

        if outputs:
            lines.append("Known outputs:")
            lines.extend(f"- {item}" for item in outputs[:16])
            if len(outputs) > 16:
                lines.append(f"- ... {len(outputs) - 16} more")

        if mapped_names:
            lines.append("Mapped engineering/live signals:")
            lines.extend(f"- {item}" for item in mapped_names[:20])
            if len(mapped_names) > 20:
                lines.append(f"- ... {len(mapped_names) - 20} more")

        if unresolved_names:
            lines.append("Unresolved engineering signals:")
            lines.extend(f"- {item}" for item in unresolved_names[:12])
            if len(unresolved_names) > 12:
                lines.append(f"- ... {len(unresolved_names) - 12} more")

        lines.extend(
            [
                "Knowledge boundary: imported PLC engineering semantics plus safely reconciled OPC UA evidence. DevAgent does not infer unmodeled physical/process facts.",
                "Ask from general to specific: 'Does the system have any faults?', 'What is wrong with the system?', 'Why is <output> false?', or 'Why is <signal> false?'.",
            ]
        )
        return LiveAssistantReply(
            question="system overview",
            kind=LiveAssistantReplyKind.SYSTEM_OVERVIEW,
            text="\n".join(lines),
        )

    def _is_overview_question(self, question: str) -> bool:
        if is_system_health_question(question):
            return False
        if resolve_explicit_tag_reference(self.context, question) is not None:
            return False
        lowered = question.casefold()
        return any(phrase in lowered for phrase in _OVERVIEW_PHRASES)

    def _can_reuse_last_target(self, question: str) -> bool:
        if self._last_target is None:
            return False
        lowered = question.casefold().strip()
        return any(phrase in lowered for phrase in _FOLLOWUP_PHRASES)

    def _mapping_only_observations(self) -> tuple[LiveObservedTag, ...]:
        if self.reconciliation is None:
            return ()
        return tuple(
            LiveObservedTag(
                tag_id=mapping.tag_id,
                tag_name=mapping.tag_name,
                node_id=mapping.selected_node_id,
                value=None,
                evidence_id=None,
                definitive_current=False,
                mapping_status=mapping.status.value,
                limitation=mapping.reason,
            )
            for mapping in self.reconciliation.mappings
        )

    def _control_refusal_reply(self, question: str) -> LiveAssistantReply:
        text = read_only_control_refusal()
        diagnosis = LiveCommissioningDiagnosis(
            target_output="",
            status=LiveDiagnosisStatus.INDETERMINATE,
            expected_output=None,
            observed_output=None,
            rule_ids=(),
            source_locators=(),
            paths=(),
            blockers=(),
            evidence_ids=(),
            limitations=(
                "Natural-language request expressed PLC/machine control intent outside DevAgent Live read-only scope.",
            ),
            summary=text,
            next_checks=(),
        )
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.LIMITATION,
            text=text,
            diagnosis=diagnosis,
        )

    def _target_limitation_reply(
        self,
        question: str,
        *,
        status: LiveDiagnosisStatus,
        candidates: tuple[str, ...],
        detail: str,
    ) -> LiveAssistantReply:
        if status is LiveDiagnosisStatus.TARGET_AMBIGUOUS:
            candidate_text = ", ".join(candidates) if candidates else "multiple outputs"
            text = (
                f"I cannot safely choose one PLC output from this question. Candidates: {candidate_text}. "
                "Use the exact engineering output/tag name so Live does not diagnose the wrong logic path."
            )
        else:
            sample = ", ".join(self.context.output_names()[:8])
            suffix = f" Known output examples: {sample}." if sample else ""
            text = (
                "I could not identify a deterministic PLC output in this question. "
                "Use an engineering output/tag name from the loaded project."
                + suffix
            )
        diagnosis = LiveCommissioningDiagnosis(
            target_output="",
            status=status,
            expected_output=None,
            observed_output=None,
            rule_ids=(),
            source_locators=(),
            paths=(),
            blockers=(),
            evidence_ids=(),
            limitations=(detail,),
            summary=text,
            next_checks=(),
        )
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.LIMITATION,
            text=text,
            diagnosis=diagnosis,
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
            reply = self.system_overview()
            return LiveAssistantReply(
                question=text,
                kind=reply.kind,
                text=reply.text,
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
        required_tag_ids = required_tag_ids_for_output(self.context, output)
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

        diagnosis = diagnose_output(
            self.context,
            output,
            observations,
        )
        answer = answer_commissioning_question(
            text,
            diagnosis,
            provider=self.provider,
        )
        return LiveAssistantReply(
            question=text,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=answer.answer,
            target_output=output,
            diagnosis=diagnosis,
            answer=answer,
        )


def create_live_commissioning_assistant(
    project_path: Path,
    connection: PlcConnectionSpec,
    *,
    project_loader: ProjectLoader | None = None,
    manager: MultiPlcConnectionManager | None = None,
    explicit_node_map: Mapping[str, str] | None = None,
    browse_max_depth: int = 4,
    browse_max_nodes: int = 500,
    provider: ModelProvider | None = None,
) -> LiveCommissioningAssistant:
    loaded = load_live_engineering_context(
        Path(project_path),
        project_loader=project_loader,
    )
    return LiveCommissioningAssistant(
        loaded,
        connection,
        manager=manager,
        explicit_node_map=explicit_node_map,
        browse_max_depth=browse_max_depth,
        browse_max_nodes=browse_max_nodes,
        provider=provider,
    )


__all__ = [
    "LiveAssistantReply",
    "LiveAssistantReplyKind",
    "LiveCommissioningAssistant",
    "create_live_commissioning_assistant",
]
