from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .commission import LoadedCommissioningConfig, run_loaded_commissioning_config
from .workflow import LiveCommissioningState, LiveCommissioningWorkflowResult


REQUIRED_VENDOR_FAMILIES = ("ROCKWELL", "SIEMENS", "SCHNEIDER")


class LiveVendorQualificationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class LiveVendorQualificationResult:
    vendor: str
    status: LiveVendorQualificationStatus
    plc_ids: tuple[str, ...]
    complete_plcs: int
    definitive_current_evidence: int
    accepted_mappings: int
    unresolved_mappings: int
    detail: str


@dataclass(frozen=True)
class LiveVendorQualificationReport:
    started_at: datetime
    finished_at: datetime
    config_sha256: str
    vendors: tuple[LiveVendorQualificationResult, ...]

    @property
    def status(self) -> LiveVendorQualificationStatus:
        if any(item.status is LiveVendorQualificationStatus.FAIL for item in self.vendors):
            return LiveVendorQualificationStatus.FAIL
        if any(item.status is LiveVendorQualificationStatus.BLOCKED for item in self.vendors):
            return LiveVendorQualificationStatus.BLOCKED
        return LiveVendorQualificationStatus.PASS

    @property
    def all_required_vendors_pass(self) -> bool:
        return (
            len(self.vendors) == len(REQUIRED_VENDOR_FAMILIES)
            and self.status is LiveVendorQualificationStatus.PASS
        )

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(item.status is status for item in self.vendors)
            for status in LiveVendorQualificationStatus
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "devagent-live-vendor-qualification-v1",
            "mode": "READ_ONLY",
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "config_sha256": self.config_sha256,
            "required_vendors": list(REQUIRED_VENDOR_FAMILIES),
            "status": self.status.value,
            "all_required_vendors_pass": self.all_required_vendors_pass,
            "counts": self.counts(),
            "vendors": [
                {
                    "vendor": item.vendor,
                    "status": item.status.value,
                    "plc_ids": list(item.plc_ids),
                    "complete_plcs": item.complete_plcs,
                    "definitive_current_evidence": item.definitive_current_evidence,
                    "accepted_mappings": item.accepted_mappings,
                    "unresolved_mappings": item.unresolved_mappings,
                    "detail": item.detail,
                }
                for item in self.vendors
            ],
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_vendor_name(project: Any) -> str:
    metadata = getattr(project, "metadata", None)
    raw = str(getattr(metadata, "vendor", "") or "").strip().upper()
    if "ROCKWELL" in raw or "ALLEN" in raw:
        return "ROCKWELL"
    if "SIEMENS" in raw or "TIA" in raw:
        return "SIEMENS"
    if "SCHNEIDER" in raw or "CONTROL EXPERT" in raw or "UNITY" in raw:
        return "SCHNEIDER"
    return raw or "UNKNOWN"


def _summarize_vendor(
    vendor: str,
    config: LoadedCommissioningConfig,
    result: LiveCommissioningWorkflowResult,
) -> LiveVendorQualificationResult:
    specs = [
        spec
        for spec in config.specs
        if canonical_vendor_name(spec.engineering_project) == vendor
    ]
    if not specs:
        return LiveVendorQualificationResult(
            vendor=vendor,
            status=LiveVendorQualificationStatus.BLOCKED,
            plc_ids=(),
            complete_plcs=0,
            definitive_current_evidence=0,
            accepted_mappings=0,
            unresolved_mappings=0,
            detail=(
                f"No {vendor} engineering project + real OPC UA endpoint is present in the commissioning config."
            ),
        )

    plc_ids = tuple(spec.connection.plc_id for spec in specs)
    complete = 0
    current = 0
    accepted = 0
    unresolved = 0
    failures: list[str] = []

    for spec in specs:
        item = result.plc_results[spec.connection.plc_id]
        if item.state is LiveCommissioningState.COMPLETE:
            complete += 1
        else:
            failures.append(
                f"{spec.connection.plc_id}={item.state.value}"
                + (f" ({item.error})" if item.error else "")
            )
        if item.reconciliation is not None:
            accepted += len(item.reconciliation.accepted_mappings())
            unresolved += len(item.reconciliation.unresolved_mappings())
        if item.evidence is not None:
            current += len(item.evidence.live_pack.definitive_current_evidence_ids)

    if failures:
        return LiveVendorQualificationResult(
            vendor=vendor,
            status=LiveVendorQualificationStatus.FAIL,
            plc_ids=plc_ids,
            complete_plcs=complete,
            definitive_current_evidence=current,
            accepted_mappings=accepted,
            unresolved_mappings=unresolved,
            detail=(
                f"{vendor} real commissioning qualification failed: "
                + "; ".join(failures)
            ),
        )
    if complete != len(specs) or current < len(specs):
        return LiveVendorQualificationResult(
            vendor=vendor,
            status=LiveVendorQualificationStatus.FAIL,
            plc_ids=plc_ids,
            complete_plcs=complete,
            definitive_current_evidence=current,
            accepted_mappings=accepted,
            unresolved_mappings=unresolved,
            detail=(
                f"{vendor} did not produce at least one trusted CURRENT evidence item per configured PLC."
            ),
        )

    return LiveVendorQualificationResult(
        vendor=vendor,
        status=LiveVendorQualificationStatus.PASS,
        plc_ids=plc_ids,
        complete_plcs=complete,
        definitive_current_evidence=current,
        accepted_mappings=accepted,
        unresolved_mappings=unresolved,
        detail=(
            f"{vendor} project parsing, real OPC UA connection, exact reconciliation, and trusted CURRENT capture passed for all configured PLCs."
        ),
    )


async def run_live_vendor_qualification(
    config: LoadedCommissioningConfig,
) -> LiveVendorQualificationReport:
    started = _now()
    workflow_result = await run_loaded_commissioning_config(config)
    vendors = tuple(
        _summarize_vendor(vendor, config, workflow_result)
        for vendor in REQUIRED_VENDOR_FAMILIES
    )
    return LiveVendorQualificationReport(
        started_at=started,
        finished_at=_now(),
        config_sha256=config.source_sha256,
        vendors=vendors,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> tuple[str, int]:
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest(), len(data)


def write_live_vendor_qualification_artifacts(
    output_dir: Path,
    report: LiveVendorQualificationReport,
) -> Path:
    target = Path(output_dir).expanduser().resolve(strict=False)
    target.mkdir(parents=True, exist_ok=False)
    try:
        report_path = target / "live_vendor_qualification.json"
        report_sha, report_bytes = _write_json(report_path, report.as_dict())
        manifest = {
            "schema": "devagent-live-vendor-qualification-manifest-v1",
            "mode": "READ_ONLY",
            "status": report.status.value,
            "all_required_vendors_pass": report.all_required_vendors_pass,
            "artifacts": {
                report_path.name: {
                    "sha256": report_sha,
                    "bytes": report_bytes,
                }
            },
        }
        _write_json(target / "manifest.json", manifest)
        return target
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


__all__ = [
    "REQUIRED_VENDOR_FAMILIES",
    "LiveVendorQualificationStatus",
    "LiveVendorQualificationResult",
    "LiveVendorQualificationReport",
    "canonical_vendor_name",
    "run_live_vendor_qualification",
    "write_live_vendor_qualification_artifacts",
]
