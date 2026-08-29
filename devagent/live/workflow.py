from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .agent_integration import LiveEvidenceStore
from .manager import ManagedPlcStatus, MultiPlcConnectionManager, PlcConnectionSpec, PlcSessionState
from .reconciled_evidence import ReconciledLiveAgentEvidence, build_reconciled_live_agent_evidence
from .tag_reconciliation import LiveTagReconciliation, reconcile_connected_project_tags


class LiveCommissioningState(str, Enum):
    COMPLETE = "COMPLETE"
    LIMITED = "LIMITED"
    CONNECT_FAILED = "CONNECT_FAILED"
    MAPPING_BLOCKED = "MAPPING_BLOCKED"
    CAPTURE_FAILED = "CAPTURE_FAILED"


@dataclass(frozen=True)
class LiveCommissioningPlcSpec:
    connection: PlcConnectionSpec
    engineering_project: Any = field(repr=False)
    required_tag_ids: tuple[str, ...] = ()
    explicit_node_map: Mapping[str, str] = field(default_factory=dict, repr=False)
    require_all_mappings: bool = True
    browse_max_depth: int = 4
    browse_max_nodes: int = 500

    def __post_init__(self) -> None:
        tags = getattr(self.engineering_project, "tags", None)
        if tags is None:
            raise ValueError(
                f"Engineering project for PLC {self.connection.plc_id} does not expose tags"
            )
        normalized_tags: list[str] = []
        seen: set[str] = set()
        for raw in self.required_tag_ids:
            tag_id = str(raw).strip()
            if not tag_id:
                raise ValueError("required_tag_ids cannot contain blank values")
            if tag_id in seen:
                continue
            seen.add(tag_id)
            normalized_tags.append(tag_id)
        if not normalized_tags:
            raise ValueError(
                f"At least one required engineering tag id is required for PLC {self.connection.plc_id}"
            )
        if len(normalized_tags) > 200:
            raise ValueError("Live commissioning V1 supports at most 200 required tags per PLC")
        object.__setattr__(self, "required_tag_ids", tuple(normalized_tags))

        clean_map: dict[str, str] = {}
        for raw_key, raw_value in dict(self.explicit_node_map).items():
            key = str(raw_key).strip()
            value = str(raw_value).strip()
            if not key or not value:
                raise ValueError("explicit_node_map keys and NodeIds cannot be blank")
            clean_map[key] = value
        object.__setattr__(self, "explicit_node_map", MappingProxyType(clean_map))

        if self.browse_max_depth < 0:
            raise ValueError("browse_max_depth must be >= 0")
        if self.browse_max_nodes < 1:
            raise ValueError("browse_max_nodes must be >= 1")


@dataclass(frozen=True)
class LiveCommissioningPlcResult:
    plc_id: str
    plc_name: str
    state: LiveCommissioningState
    connection_status: ManagedPlcStatus
    reconciliation: LiveTagReconciliation | None = None
    evidence: ReconciledLiveAgentEvidence | None = None
    error: str | None = None

    @property
    def workflow_completed(self) -> bool:
        return self.state in {
            LiveCommissioningState.COMPLETE,
            LiveCommissioningState.LIMITED,
        }

    @property
    def definitive_current(self) -> bool:
        return self.state is LiveCommissioningState.COMPLETE


@dataclass(frozen=True)
class LiveCommissioningWorkflowResult:
    started_at: datetime
    finished_at: datetime
    plc_results: dict[str, LiveCommissioningPlcResult]
    disconnect_statuses: dict[str, ManagedPlcStatus]

    @property
    def all_complete(self) -> bool:
        return bool(self.plc_results) and all(
            result.state is LiveCommissioningState.COMPLETE
            for result in self.plc_results.values()
        )

    @property
    def any_limited_or_failed(self) -> bool:
        return any(
            result.state is not LiveCommissioningState.COMPLETE
            for result in self.plc_results.values()
        )

    def live_packs(self) -> dict[str, Any]:
        return {
            plc_id: result.evidence.live_pack
            for plc_id, result in self.plc_results.items()
            if result.evidence is not None
        }


class LiveCommissioningWorkflow:
    """Orchestrate bounded multi-PLC commissioning evidence capture.

    This workflow is intentionally read-only. It coordinates existing connect,
    browse/reconcile, read/trust, and evidence surfaces but introduces no PLC
    control or mutation capability.
    """

    def __init__(
        self,
        specs: Iterable[LiveCommissioningPlcSpec],
        *,
        manager: MultiPlcConnectionManager | None = None,
        evidence_store: LiveEvidenceStore | None = None,
        disconnect_when_done: bool = True,
    ) -> None:
        normalized = tuple(specs)
        if not normalized:
            raise ValueError("At least one Live commissioning PLC spec is required")
        ids = [spec.connection.plc_id for spec in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("Live commissioning PLC ids must be unique")
        self.specs = normalized
        self.manager = manager or MultiPlcConnectionManager(
            [spec.connection for spec in normalized]
        )
        if set(self.manager.plc_ids) != set(ids):
            raise ValueError(
                "Provided MultiPlcConnectionManager PLC ids must exactly match workflow specs"
            )
        self.evidence_store = evidence_store or LiveEvidenceStore(max_packs=max(32, len(ids) * 4))
        self.disconnect_when_done = disconnect_when_done

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _safe_error(spec: LiveCommissioningPlcSpec, exc: BaseException) -> str:
        return spec.connection.security.redact(str(exc))

    def _requested_mappings(
        self,
        spec: LiveCommissioningPlcSpec,
        reconciliation: LiveTagReconciliation,
    ) -> tuple[Any, ...]:
        by_id = reconciliation.mapping_by_tag_id()
        selected: list[Any] = []
        for tag_id in spec.required_tag_ids:
            mapping = by_id.get(tag_id)
            if mapping is None:
                raise ValueError(f"Unknown engineering tag id: {tag_id}")
            selected.append(mapping)
        return tuple(selected)

    async def _run_one(
        self,
        spec: LiveCommissioningPlcSpec,
        connect_status: ManagedPlcStatus,
    ) -> LiveCommissioningPlcResult:
        plc_id = spec.connection.plc_id
        if not connect_status.connected or connect_status.state is not PlcSessionState.CONNECTED:
            return LiveCommissioningPlcResult(
                plc_id=plc_id,
                plc_name=spec.connection.display_name,
                state=LiveCommissioningState.CONNECT_FAILED,
                connection_status=connect_status,
                error=connect_status.last_error or "OPC UA session did not reach CONNECTED",
            )

        try:
            reconciliation = await reconcile_connected_project_tags(
                self.manager,
                plc_id,
                spec.engineering_project,
                explicit_node_map=spec.explicit_node_map,
                max_depth=spec.browse_max_depth,
                max_nodes=spec.browse_max_nodes,
            )
            requested_mappings = self._requested_mappings(spec, reconciliation)
        except Exception as exc:
            safe = self._safe_error(spec, exc)
            return LiveCommissioningPlcResult(
                plc_id=plc_id,
                plc_name=spec.connection.display_name,
                state=LiveCommissioningState.MAPPING_BLOCKED,
                connection_status=self.manager.status(plc_id),
                error=safe,
            )

        unresolved = tuple(mapping for mapping in requested_mappings if not mapping.accepted)
        if unresolved and spec.require_all_mappings:
            details = ", ".join(
                f"{mapping.tag_id}={mapping.status.value}"
                for mapping in unresolved
            )
            return LiveCommissioningPlcResult(
                plc_id=plc_id,
                plc_name=spec.connection.display_name,
                state=LiveCommissioningState.MAPPING_BLOCKED,
                connection_status=self.manager.status(plc_id),
                reconciliation=reconciliation,
                error=f"Required engineering tags are not safely reconciled: {details}",
            )

        try:
            evidence = await build_reconciled_live_agent_evidence(
                self.manager,
                reconciliation,
                required_tag_ids=spec.required_tag_ids,
                require_all=spec.require_all_mappings,
                store=self.evidence_store,
            )
        except Exception as exc:
            safe = self._safe_error(spec, exc)
            return LiveCommissioningPlcResult(
                plc_id=plc_id,
                plc_name=spec.connection.display_name,
                state=LiveCommissioningState.CAPTURE_FAILED,
                connection_status=self.manager.status(plc_id),
                reconciliation=reconciliation,
                error=safe,
            )

        accepted_requested = [mapping for mapping in requested_mappings if mapping.accepted]
        expected_current = len(accepted_requested)
        actual_current = len(evidence.live_pack.definitive_current_evidence_ids)
        limited = bool(unresolved) or actual_current != expected_current or bool(evidence.live_pack.limitations)
        state = LiveCommissioningState.LIMITED if limited else LiveCommissioningState.COMPLETE
        return LiveCommissioningPlcResult(
            plc_id=plc_id,
            plc_name=spec.connection.display_name,
            state=state,
            connection_status=self.manager.status(plc_id),
            reconciliation=reconciliation,
            evidence=evidence,
            error=None,
        )

    async def run(self) -> LiveCommissioningWorkflowResult:
        started_at = self._now()
        connect_statuses = await self.manager.connect_all()
        tasks = [
            asyncio.create_task(
                self._run_one(spec, connect_statuses[spec.connection.plc_id])
            )
            for spec in self.specs
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        plc_results: dict[str, LiveCommissioningPlcResult] = {}
        for spec, raw in zip(self.specs, raw_results):
            plc_id = spec.connection.plc_id
            if isinstance(raw, BaseException):
                plc_results[plc_id] = LiveCommissioningPlcResult(
                    plc_id=plc_id,
                    plc_name=spec.connection.display_name,
                    state=LiveCommissioningState.CAPTURE_FAILED,
                    connection_status=self.manager.status(plc_id),
                    error=self._safe_error(spec, raw),
                )
            else:
                plc_results[plc_id] = raw

        disconnect_statuses: dict[str, ManagedPlcStatus] = {}
        if self.disconnect_when_done:
            disconnect_statuses = await self.manager.disconnect_all()
        finished_at = self._now()
        return LiveCommissioningWorkflowResult(
            started_at=started_at,
            finished_at=finished_at,
            plc_results=plc_results,
            disconnect_statuses=disconnect_statuses,
        )
