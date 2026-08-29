from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .agent_integration import (
    LiveAgentEvidenceItem,
    LiveAgentEvidencePack,
    LiveDataTrustLayer,
    LiveEvidenceStore,
    build_live_agent_evidence_pack,
)
from .manager import MultiPlcConnectionManager
from .tag_reconciliation import LiveTagReconciliation


@dataclass(frozen=True)
class ReconciledLiveAgentEvidence:
    """Engineering-tag mapping evidence plus its trust-gated live snapshot."""

    reconciliation: LiveTagReconciliation
    live_pack: LiveAgentEvidencePack
    mapping_evidence: tuple[LiveAgentEvidenceItem, ...]

    def evidence_for_agent(self) -> tuple[LiveAgentEvidenceItem, ...]:
        return self.mapping_evidence + self.live_pack.evidence_for_agent()


async def build_reconciled_live_agent_evidence(
    manager: MultiPlcConnectionManager,
    reconciliation: LiveTagReconciliation,
    *,
    required_tag_ids: Iterable[str] | None = None,
    require_all: bool = True,
    trust_layer: LiveDataTrustLayer | None = None,
    store: LiveEvidenceStore | None = None,
) -> ReconciledLiveAgentEvidence:
    """Read reconciled tags and build AI evidence without accepting unresolved mappings."""

    requests = reconciliation.node_request_map(
        required_tag_ids=required_tag_ids,
        require_all=require_all,
    )
    node_ids = requests.get(reconciliation.plc_id, ())
    if not node_ids:
        from .errors import LiveConfigurationError

        raise LiveConfigurationError(
            f"No safely reconciled live nodes are available for PLC {reconciliation.plc_id}"
        )
    pack = await build_live_agent_evidence_pack(
        manager,
        requests,
        trust_layer=trust_layer,
        store=store,
    )
    return ReconciledLiveAgentEvidence(
        reconciliation=reconciliation,
        live_pack=pack,
        mapping_evidence=reconciliation.evidence_items(),
    )


async def run_reconciled_live_augmented_ai_review(
    provider: Any,
    engineering: Any,
    static_evidence: list[Any],
    deterministic_findings: list[Any],
    manager: MultiPlcConnectionManager,
    reconciliation: LiveTagReconciliation,
    *,
    required_tag_ids: Iterable[str] | None = None,
    require_all: bool = True,
    trace_sink: list[dict[str, Any]] | None = None,
    trust_layer: LiveDataTrustLayer | None = None,
    store: LiveEvidenceStore | None = None,
) -> tuple[tuple[Any, ...], tuple[str, ...], ReconciledLiveAgentEvidence]:
    """Run the existing bounded AI review with mapping provenance + trusted live data."""

    from devagent.plc.production_ai import run_ai_review

    reconciled = await build_reconciled_live_agent_evidence(
        manager,
        reconciliation,
        required_tag_ids=required_tag_ids,
        require_all=require_all,
        trust_layer=trust_layer,
        store=store,
    )
    combined = list(static_evidence) + list(reconciled.evidence_for_agent())
    findings, warnings = run_ai_review(
        provider,
        engineering,
        combined,
        deterministic_findings,
        trace_sink=trace_sink,
    )
    return tuple(findings), tuple(warnings), reconciled


async def run_reconciled_live_augmented_requirement_mapping(
    provider: Any,
    requirements: list[Any],
    verifications: list[Any],
    static_evidence: list[Any],
    manager: MultiPlcConnectionManager,
    reconciliation: LiveTagReconciliation,
    *,
    required_tag_ids: Iterable[str] | None = None,
    require_all: bool = True,
    trace_sink: list[dict[str, Any]] | None = None,
    trust_layer: LiveDataTrustLayer | None = None,
    store: LiveEvidenceStore | None = None,
) -> tuple[dict[str, Any], tuple[str, ...], ReconciledLiveAgentEvidence]:
    """Run requirement mapping with exact engineering-tag-to-NodeId provenance."""

    from devagent.plc.production_ai import run_ai_requirement_mapping

    reconciled = await build_reconciled_live_agent_evidence(
        manager,
        reconciliation,
        required_tag_ids=required_tag_ids,
        require_all=require_all,
        trust_layer=trust_layer,
        store=store,
    )
    combined = list(static_evidence) + list(reconciled.evidence_for_agent())
    mappings, warnings = run_ai_requirement_mapping(
        provider,
        requirements,
        verifications,
        combined,
        trace_sink=trace_sink,
    )
    return mappings, tuple(warnings), reconciled
