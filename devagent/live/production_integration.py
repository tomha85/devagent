from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent_integration import (
    LiveAgentEvidencePack,
    LiveEvidenceDisposition,
    LiveEvidenceRecord,
)


@dataclass(frozen=True)
class LiveProductionEvidenceSummary:
    pack_id: str
    captured_at: datetime
    total_records: int
    current_records: int
    excluded_raw_records: int
    limitation_count: int
    plc_states: dict[str, str]
    all_plcs_connected: bool

    @property
    def has_definitive_current_evidence(self) -> bool:
        return self.current_records > 0


@dataclass(frozen=True)
class LiveProductionAugmentedResult:
    """Sidecar result that preserves the authoritative production result unchanged."""

    production_result: Any
    live_pack: LiveAgentEvidencePack
    live_summary: LiveProductionEvidenceSummary

    @property
    def readiness(self) -> Any:
        return getattr(self.production_result, "readiness", None)


@dataclass(frozen=True)
class LiveProductionArtifacts:
    report_path: Path
    evidence_path: Path
    manifest_path: Path
    report_sha256: str
    evidence_sha256: str


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


def _compact(value: Any, *, limit: int = 120) -> str:
    rendered = repr(_json_safe(value))
    return rendered if len(rendered) <= limit else f"{rendered[: limit - 3]}..."


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def summarize_live_production_evidence(pack: LiveAgentEvidencePack) -> LiveProductionEvidenceSummary:
    current = sum(1 for record in pack.records if record.definitive_current)
    excluded = sum(1 for record in pack.records if not record.agent_eligible)
    return LiveProductionEvidenceSummary(
        pack_id=pack.pack_id,
        captured_at=pack.captured_at,
        total_records=len(pack.records),
        current_records=current,
        excluded_raw_records=excluded,
        limitation_count=len(pack.limitations),
        plc_states=dict(pack.plc_states),
        all_plcs_connected=bool(pack.plc_states)
        and all(state == "CONNECTED" for state in pack.plc_states.values()),
    )


def augment_production_result(
    production_result: Any,
    pack: LiveAgentEvidencePack,
) -> LiveProductionAugmentedResult:
    """Attach live observations without mutating deterministic production proof/readiness."""

    return LiveProductionAugmentedResult(
        production_result=production_result,
        live_pack=pack,
        live_summary=summarize_live_production_evidence(pack),
    )


def _current_observation(record: LiveEvidenceRecord) -> dict[str, Any]:
    return {
        "evidence_id": record.evidence_id,
        "plc_id": record.plc_id,
        "plc_name": record.plc_name,
        "node_id": record.node_id,
        "value": _json_safe(record.value),
        "variant_type": record.variant_type,
        "status_code": record.status_code,
        "quality": record.quality,
        "trust": record.trust,
        "disposition": record.disposition.value,
        "source_timestamp": _iso(record.source_timestamp),
        "server_timestamp": _iso(record.server_timestamp),
        "received_at": _iso(record.received_at),
        "age_seconds": record.age_seconds,
        "replayed": record.replayed,
        "definitive_current": True,
    }


def _excluded_observation(record: LiveEvidenceRecord) -> dict[str, Any]:
    # The audit pack may retain the raw value, but customer/AI-facing production
    # artifacts deliberately omit it when the deterministic trust gate rejected it.
    return {
        "evidence_id": record.evidence_id,
        "plc_id": record.plc_id,
        "plc_name": record.plc_name,
        "node_id": record.node_id,
        "variant_type": record.variant_type,
        "status_code": record.status_code,
        "quality": record.quality,
        "trust": record.trust,
        "disposition": record.disposition.value,
        "source_timestamp": _iso(record.source_timestamp),
        "server_timestamp": _iso(record.server_timestamp),
        "received_at": _iso(record.received_at),
        "age_seconds": record.age_seconds,
        "replayed": record.replayed,
        "raw_value_included": False,
        "definitive_current": False,
    }


def build_live_customer_evidence_artifact(
    pack: LiveAgentEvidencePack,
) -> dict[str, Any]:
    summary = summarize_live_production_evidence(pack)
    current = [
        _current_observation(record)
        for record in pack.records
        if record.definitive_current
    ]
    excluded = [
        _excluded_observation(record)
        for record in pack.records
        if not record.agent_eligible
    ]
    return {
        "schema": "devagent-live-commissioning-evidence-v1",
        "pack_id": pack.pack_id,
        "captured_at": _iso(pack.captured_at),
        "mode": "READ_ONLY",
        "effect_on_release_readiness": "NONE",
        "verification_boundary": (
            "Live observations are commissioning evidence. They do not by themselves "
            "promote FAT tests to PASS, verify safety certification, or change release readiness."
        ),
        "counts": {
            "records": summary.total_records,
            "current": summary.current_records,
            "excluded_raw": summary.excluded_raw_records,
            "limitations": summary.limitation_count,
        },
        "plc_states": dict(pack.plc_states),
        "all_plcs_connected": summary.all_plcs_connected,
        "current_observations": current,
        "excluded_observations": excluded,
        "limitations": list(pack.limitations),
    }


def _markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        safe = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        lines.append("| " + " | ".join(safe) + " |")
    lines.append("")
    return lines


def render_live_commissioning_section(pack: LiveAgentEvidencePack) -> str:
    summary = summarize_live_production_evidence(pack)
    lines: list[str] = [
        "## Live Commissioning Evidence (Read Only)",
        "",
        "> This section is observational commissioning evidence only. It does not change the deterministic FAT execution status, safety/certification claims, release policy, signatures, or Release Readiness result in this report.",
        "",
        f"- Evidence pack: `{pack.pack_id}`",
        f"- Captured at: `{pack.captured_at.isoformat()}`",
        f"- PLC sessions: **{len(pack.plc_states)}**",
        f"- CURRENT trusted observations: **{summary.current_records}**",
        f"- Raw observations excluded by trust gate: **{summary.excluded_raw_records}**",
        f"- Evidence limitations: **{summary.limitation_count}**",
        f"- All requested PLC sessions connected: **{'YES' if summary.all_plcs_connected else 'NO'}**",
        "",
        "### PLC Connection State",
        "",
    ]
    lines += _markdown_table(
        ["PLC", "State"],
        [[plc_id, state] for plc_id, state in sorted(pack.plc_states.items())],
    )

    lines += ["### Trusted CURRENT Observations", ""]
    lines += _markdown_table(
        ["PLC", "Node", "Value", "Type", "Quality", "Trust", "Source timestamp", "Age (s)"],
        [
            [
                record.plc_name,
                record.node_id,
                _compact(record.value),
                record.variant_type or "-",
                record.quality,
                record.trust,
                _iso(record.source_timestamp) or "-",
                "-" if record.age_seconds is None else f"{record.age_seconds:.3f}",
            ]
            for record in pack.records
            if record.definitive_current
        ],
    )

    lines += ["### Withheld / Non-Current Observations", ""]
    lines += _markdown_table(
        ["PLC", "Node", "Disposition", "Quality", "Trust", "Replayed", "Raw value shown"],
        [
            [
                record.plc_name,
                record.node_id,
                record.disposition.value,
                record.quality,
                record.trust,
                "YES" if record.replayed else "NO",
                "NO",
            ]
            for record in pack.records
            if not record.agent_eligible
        ],
    )

    lines += ["### Live Evidence Limitations", ""]
    if pack.limitations:
        lines += [f"- {item}" for item in pack.limitations]
        lines.append("")
    else:
        lines += ["- None reported by the live trust layer.", ""]

    lines += [
        "### Live Verification Boundary",
        "",
        "- Only GOOD + CURRENT + non-stale + non-replayed values appear above as trusted raw observations.",
        "- STALE, UNCERTAIN, BAD/UNTRUSTED, REPLAYED, and unavailable observations cannot support definitive current-state claims.",
        "- Live values can support commissioning diagnosis and evidence traceability, but are not qualified FAT execution PASS evidence by themselves.",
        "- This integration remains read-only and adds no PLC write, force, reset, download, mode-change, or method-call path.",
        "",
    ]
    return "\n".join(lines)


def render_live_augmented_production_report(
    production_result: Any,
    pack: LiveAgentEvidencePack,
) -> str:
    """Render existing production report plus an additive live section.

    The underlying production result is not changed, so release readiness remains
    exactly what the deterministic production pipeline calculated.
    """

    from devagent.plc.production_report import render_production_report

    base = render_production_report(production_result)
    section = render_live_commissioning_section(pack).rstrip()
    marker = "\n## Release Readiness\n"
    if marker in base:
        return base.replace(marker, f"\n{section}\n\n## Release Readiness\n", 1)
    return base.rstrip() + "\n\n" + section + "\n"


def write_live_production_artifacts(
    output_dir: Path,
    production_result: Any,
    pack: LiveAgentEvidencePack,
) -> LiveProductionArtifacts:
    """Write additive live artifacts without modifying the canonical PLC run files."""

    output_dir = Path(output_dir).expanduser().resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    report = render_live_augmented_production_report(production_result, pack)
    evidence = build_live_customer_evidence_artifact(pack)
    report_path = output_dir / "fat_report_live_augmented.md"
    evidence_path = output_dir / "live_commissioning_evidence.json"
    manifest_path = output_dir / "live_commissioning_manifest.json"

    report_path.write_text(report, encoding="utf-8")
    evidence_bytes = (json.dumps(evidence, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    evidence_path.write_bytes(evidence_bytes)

    report_sha = _sha256_file(report_path)
    evidence_sha = _sha256_file(evidence_path)
    project = getattr(getattr(production_result, "engineering", None), "project", None)
    metadata = getattr(project, "metadata", None)
    readiness = getattr(production_result, "readiness", None)
    readiness_status = getattr(getattr(readiness, "status", None), "value", None)

    manifest = {
        "schema": "devagent-live-commissioning-artifacts-v1",
        "pack_id": pack.pack_id,
        "project_sha256": getattr(metadata, "source_sha256", None),
        "production_readiness": readiness_status or "NOT_EVALUATED",
        "production_readiness_modified_by_live_evidence": False,
        "mode": "READ_ONLY",
        "artifacts": {
            report_path.name: report_sha,
            evidence_path.name: evidence_sha,
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return LiveProductionArtifacts(
        report_path=report_path,
        evidence_path=evidence_path,
        manifest_path=manifest_path,
        report_sha256=report_sha,
        evidence_sha256=evidence_sha,
    )
