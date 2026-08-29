from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from devagent.live.manager import (
    MultiPlcConnectionManager,
    PlcReadResult,
    PlcSessionState,
)
from devagent.live.models import Quality, RuntimeValue, TrustState


@dataclass(frozen=True)
class LiveAgentEvidenceItem:
    id: str
    kind: str
    summary: str
    source_locator: str | None = None
    source_sha256: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class LiveEvidenceDisposition(str, Enum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNCERTAIN = "UNCERTAIN"
    UNTRUSTED = "UNTRUSTED"
    REPLAYED = "REPLAYED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class LiveEvidenceRecord:
    evidence_id: str
    plc_id: str
    plc_name: str
    node_id: str
    disposition: LiveEvidenceDisposition
    quality: str
    trust: str
    value: Any
    variant_type: str | None
    status_code: str
    source_timestamp: datetime | None
    server_timestamp: datetime | None
    received_at: datetime
    age_seconds: float | None
    replayed: bool
    agent_eligible: bool
    definitive_current: bool

    def as_evidence_item(self) -> LiveAgentEvidenceItem:
        value_text = _compact_value(self.value)
        summary = (
            f"Live OPC UA observation {self.plc_name} ({self.plc_id}) {self.node_id}: "
            f"value={value_text}, quality={self.quality}, trust={self.trust}, "
            f"disposition={self.disposition.value}. "
            "CURRENT live observations support diagnosis only; they do not by themselves prove "
            "FAT PASS, safety certification, or release readiness."
        )
        return LiveAgentEvidenceItem(
            id=self.evidence_id,
            kind=f"LIVE_OPCUA_{self.disposition.value}",
            summary=summary,
            source_locator=f"opcua:{self.plc_id}:{self.node_id}",
            payload={
                "plc_id": self.plc_id,
                "plc_name": self.plc_name,
                "node_id": self.node_id,
                "value": _json_safe(self.value),
                "variant_type": self.variant_type,
                "status_code": self.status_code,
                "quality": self.quality,
                "trust": self.trust,
                "disposition": self.disposition.value,
                "source_timestamp": _iso(self.source_timestamp),
                "server_timestamp": _iso(self.server_timestamp),
                "received_at": _iso(self.received_at),
                "age_seconds": self.age_seconds,
                "replayed": self.replayed,
                "agent_eligible": self.agent_eligible,
                "definitive_current": self.definitive_current,
            },
        )


@dataclass(frozen=True)
class LiveAgentEvidencePack:
    pack_id: str
    captured_at: datetime
    records: tuple[LiveEvidenceRecord, ...]
    evidence: tuple[LiveAgentEvidenceItem, ...]
    agent_evidence_ids: frozenset[str]
    definitive_current_evidence_ids: frozenset[str]
    excluded_raw_evidence_ids: frozenset[str]
    limitations: tuple[str, ...]
    plc_states: dict[str, str]

    def evidence_for_agent(self) -> tuple[LiveAgentEvidenceItem, ...]:
        return tuple(item for item in self.evidence if item.id in self.agent_evidence_ids)

    def current_records(self) -> tuple[LiveEvidenceRecord, ...]:
        return tuple(record for record in self.records if record.definitive_current)


@dataclass(frozen=True)
class LiveAugmentedReviewResult:
    findings: tuple[Any, ...]
    warnings: tuple[str, ...]
    pack: LiveAgentEvidencePack


@dataclass(frozen=True)
class LiveAugmentedRequirementMappingResult:
    mappings: dict[str, Any]
    warnings: tuple[str, ...]
    pack: LiveAgentEvidencePack


class LiveDataTrustLayer:
    """Deterministically decide which live values may reach the AI review context."""

    def classify(self, value: RuntimeValue) -> LiveEvidenceDisposition:
        if value.quality is Quality.BAD or value.trust is TrustState.UNTRUSTED:
            return LiveEvidenceDisposition.UNTRUSTED
        if value.stale or value.trust is TrustState.STALE:
            return LiveEvidenceDisposition.STALE
        if value.quality is Quality.UNCERTAIN or value.trust is TrustState.UNCERTAIN:
            return LiveEvidenceDisposition.UNCERTAIN
        if value.replayed:
            return LiveEvidenceDisposition.REPLAYED
        if value.quality is Quality.GOOD and value.trust is TrustState.CURRENT:
            return LiveEvidenceDisposition.CURRENT
        return LiveEvidenceDisposition.UNTRUSTED

    def record(
        self,
        *,
        plc_id: str,
        plc_name: str,
        value: RuntimeValue,
    ) -> LiveEvidenceRecord:
        disposition = self.classify(value)
        eligible = disposition is LiveEvidenceDisposition.CURRENT
        evidence_id = _live_evidence_id(plc_id, value)
        return LiveEvidenceRecord(
            evidence_id=evidence_id,
            plc_id=plc_id,
            plc_name=plc_name,
            node_id=value.node_id,
            disposition=disposition,
            quality=value.quality.value,
            trust=value.trust.value,
            value=value.value,
            variant_type=value.variant_type,
            status_code=value.status_code,
            source_timestamp=value.source_timestamp,
            server_timestamp=value.server_timestamp,
            received_at=value.received_at,
            age_seconds=value.age_seconds,
            replayed=value.replayed,
            agent_eligible=eligible,
            definitive_current=eligible,
        )


class LiveEvidenceStore:
    """Bounded in-memory audit store for immutable live evidence packs."""

    def __init__(self, *, max_packs: int = 32) -> None:
        if max_packs < 1:
            raise ValueError("max_packs must be >= 1")
        self.max_packs = max_packs
        self._packs: deque[LiveAgentEvidencePack] = deque(maxlen=max_packs)

    def add(self, pack: LiveAgentEvidencePack) -> None:
        self._packs.append(pack)

    def latest(self) -> LiveAgentEvidencePack | None:
        return self._packs[-1] if self._packs else None

    def get(self, pack_id: str) -> LiveAgentEvidencePack | None:
        for pack in reversed(self._packs):
            if pack.pack_id == pack_id:
                return pack
        return None

    def packs(self) -> tuple[LiveAgentEvidencePack, ...]:
        return tuple(self._packs)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return repr(value)


def _compact_value(value: Any, *, limit: int = 160) -> str:
    rendered = repr(_json_safe(value))
    return rendered if len(rendered) <= limit else f"{rendered[: limit - 3]}..."


def _live_evidence_id(plc_id: str, value: RuntimeValue) -> str:
    fingerprint = {
        "plc_id": plc_id,
        "node_id": value.node_id,
        "value": _json_safe(value.value),
        "variant_type": value.variant_type,
        "status_code": value.status_code,
        "quality": value.quality.value,
        "trust": value.trust.value,
        "source_timestamp": _iso(value.source_timestamp),
        "server_timestamp": _iso(value.server_timestamp),
        "received_at": _iso(value.received_at),
        "age_seconds": value.age_seconds,
        "stale": value.stale,
        "replayed": value.replayed,
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return f"LIVE:{plc_id}:{digest[:24]}"


def _limitation_evidence(
    *,
    pack_seed: str,
    plc_id: str,
    plc_name: str,
    state: str,
    reason: str,
    node_id: str | None = None,
) -> LiveAgentEvidenceItem:
    key = f"{pack_seed}|{plc_id}|{node_id or '-'}|{state}|{reason}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    target = f" node {node_id}" if node_id else ""
    return LiveAgentEvidenceItem(
        id=f"LIVE-LIMIT:{plc_id}:{digest}",
        kind="LIVE_OPCUA_LIMITATION",
        summary=(
            f"Live OPC UA evidence limitation for {plc_name} ({plc_id}){target}: "
            f"{reason}; session_state={state}. No raw excluded value is supplied to the AI. "
            "Treat this as an evidence gap, not as proof of controller behavior."
        ),
        source_locator=f"opcua:{plc_id}:{node_id or 'session'}",
        payload={
            "plc_id": plc_id,
            "plc_name": plc_name,
            "node_id": node_id,
            "session_state": state,
            "reason": reason,
            "raw_value_included": False,
            "definitive_current": False,
        },
    )


def _normalize_requests(
    node_ids_by_plc: Mapping[str, Iterable[str]],
) -> dict[str, tuple[str, ...]]:
    if not node_ids_by_plc:
        raise ValueError("At least one PLC/node request is required")
    normalized: dict[str, tuple[str, ...]] = {}
    for plc_id, node_ids in node_ids_by_plc.items():
        clean_plc_id = str(plc_id).strip()
        if not clean_plc_id:
            raise ValueError("PLC id cannot be blank")
        values: list[str] = []
        seen: set[str] = set()
        for node_id in node_ids:
            text = str(node_id).strip()
            if not text:
                raise ValueError(f"Node id for PLC {clean_plc_id} cannot be blank")
            if text not in seen:
                seen.add(text)
                values.append(text)
        if not values:
            raise ValueError(f"At least one node id is required for PLC {clean_plc_id}")
        normalized[clean_plc_id] = tuple(values)
    return normalized


async def build_live_agent_evidence_pack(
    manager: MultiPlcConnectionManager,
    node_ids_by_plc: Mapping[str, Iterable[str]],
    *,
    trust_layer: LiveDataTrustLayer | None = None,
    store: LiveEvidenceStore | None = None,
) -> LiveAgentEvidencePack:
    requests = _normalize_requests(node_ids_by_plc)
    trust_layer = trust_layer or LiveDataTrustLayer()
    captured_at = datetime.now(timezone.utc)
    statuses = manager.statuses()
    results = await manager.read_many(requests)
    statuses = manager.statuses()

    records: list[LiveEvidenceRecord] = []
    evidence: list[LiveAgentEvidenceItem] = []
    eligible_ids: set[str] = set()
    definitive_ids: set[str] = set()
    excluded_ids: set[str] = set()
    limitations: list[str] = []
    pack_seed = captured_at.isoformat()

    for plc_id, requested_nodes in requests.items():
        status = statuses[plc_id]
        result: PlcReadResult = results[plc_id]
        observed_nodes: set[str] = set()

        for value in result.values:
            observed_nodes.add(value.node_id)
            record = trust_layer.record(
                plc_id=plc_id,
                plc_name=status.plc_name,
                value=value,
            )
            records.append(record)
            item = record.as_evidence_item()
            evidence.append(item)
            if record.agent_eligible:
                eligible_ids.add(record.evidence_id)
                definitive_ids.add(record.evidence_id)
            else:
                excluded_ids.add(record.evidence_id)
                reason = (
                    f"raw live value excluded because disposition={record.disposition.value}, "
                    f"quality={record.quality}, trust={record.trust}"
                )
                limitation = _limitation_evidence(
                    pack_seed=pack_seed,
                    plc_id=plc_id,
                    plc_name=status.plc_name,
                    state=status.state.value,
                    reason=reason,
                    node_id=value.node_id,
                )
                evidence.append(limitation)
                eligible_ids.add(limitation.id)
                limitations.append(
                    f"{plc_id}:{value.node_id}: excluded {record.disposition.value} live value"
                )

        missing_nodes = [node_id for node_id in requested_nodes if node_id not in observed_nodes]
        if result.error is not None or missing_nodes:
            targets = missing_nodes or [None]
            for node_id in targets:
                reason = "requested live read was unavailable or failed"
                limitation = _limitation_evidence(
                    pack_seed=pack_seed,
                    plc_id=plc_id,
                    plc_name=status.plc_name,
                    state=status.state.value,
                    reason=reason,
                    node_id=node_id,
                )
                evidence.append(limitation)
                eligible_ids.add(limitation.id)
                limitations.append(
                    f"{plc_id}:{node_id or 'session'}: live read unavailable"
                )

        if status.state is not PlcSessionState.CONNECTED:
            limitation = _limitation_evidence(
                pack_seed=pack_seed,
                plc_id=plc_id,
                plc_name=status.plc_name,
                state=status.state.value,
                reason="PLC session is not in CONNECTED state after evidence capture",
            )
            if limitation.id not in {item.id for item in evidence}:
                evidence.append(limitation)
                eligible_ids.add(limitation.id)
            limitations.append(f"{plc_id}: session state {status.state.value}")

    pack_fingerprint = {
        "captured_at": captured_at.isoformat(),
        "record_ids": [record.evidence_id for record in records],
        "eligible_ids": sorted(eligible_ids),
        "plc_states": {plc_id: statuses[plc_id].state.value for plc_id in sorted(statuses)},
    }
    pack_id = "LIVE-PACK:" + hashlib.sha256(
        json.dumps(pack_fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]

    pack = LiveAgentEvidencePack(
        pack_id=pack_id,
        captured_at=captured_at,
        records=tuple(records),
        evidence=tuple(evidence),
        agent_evidence_ids=frozenset(eligible_ids),
        definitive_current_evidence_ids=frozenset(definitive_ids),
        excluded_raw_evidence_ids=frozenset(excluded_ids),
        limitations=tuple(dict.fromkeys(limitations)),
        plc_states={plc_id: status.state.value for plc_id, status in statuses.items()},
    )
    if store is not None:
        store.add(pack)
    return pack


async def run_live_augmented_ai_review(
    provider: Any,
    engineering: Any,
    static_evidence: list[Any],
    deterministic_findings: list[Any],
    manager: MultiPlcConnectionManager,
    node_ids_by_plc: Mapping[str, Iterable[str]],
    *,
    trace_sink: list[dict[str, Any]] | None = None,
    trust_layer: LiveDataTrustLayer | None = None,
    store: LiveEvidenceStore | None = None,
) -> LiveAugmentedReviewResult:
    from devagent.plc.production_ai import run_ai_review

    pack = await build_live_agent_evidence_pack(
        manager,
        node_ids_by_plc,
        trust_layer=trust_layer,
        store=store,
    )
    combined = list(static_evidence) + list(pack.evidence_for_agent())
    findings, warnings = run_ai_review(
        provider,
        engineering,
        combined,
        deterministic_findings,
        trace_sink=trace_sink,
    )
    return LiveAugmentedReviewResult(
        findings=tuple(findings),
        warnings=tuple(warnings),
        pack=pack,
    )


async def run_live_augmented_requirement_mapping(
    provider: Any,
    requirements: list[Any],
    verifications: list[Any],
    static_evidence: list[Any],
    manager: MultiPlcConnectionManager,
    node_ids_by_plc: Mapping[str, Iterable[str]],
    *,
    trace_sink: list[dict[str, Any]] | None = None,
    trust_layer: LiveDataTrustLayer | None = None,
    store: LiveEvidenceStore | None = None,
) -> LiveAugmentedRequirementMappingResult:
    from devagent.plc.production_ai import run_ai_requirement_mapping

    pack = await build_live_agent_evidence_pack(
        manager,
        node_ids_by_plc,
        trust_layer=trust_layer,
        store=store,
    )
    combined = list(static_evidence) + list(pack.evidence_for_agent())
    mappings, warnings = run_ai_requirement_mapping(
        provider,
        requirements,
        verifications,
        combined,
        trace_sink=trace_sink,
    )
    return LiveAugmentedRequirementMappingResult(
        mappings=mappings,
        warnings=tuple(warnings),
        pack=pack,
    )
