from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .engineering_context import LiveEngineeringContext, LiveEngineeringTag
from .history import LiveHistoricalSample, LiveTimelineStore
from .stateful_context import (
    LiveStatefulDiagnosisStatus,
    LiveStatefulKind,
    LiveStatefulModel,
    LiveStatefulTransition,
    diagnose_live_stateful_model,
)
from .vendor_qualification import REQUIRED_VENDOR_FAMILIES


class LiveCommercialGateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class LiveCommercialGateResult:
    gate_id: str
    title: str
    status: LiveCommercialGateStatus
    detail: str


@dataclass(frozen=True)
class LiveCommercialReadinessReport:
    generated_at: datetime
    gates: tuple[LiveCommercialGateResult, ...]
    min_soak_hours: float

    @property
    def status(self) -> LiveCommercialGateStatus:
        if any(item.status is LiveCommercialGateStatus.FAIL for item in self.gates):
            return LiveCommercialGateStatus.FAIL
        if any(item.status is LiveCommercialGateStatus.BLOCKED for item in self.gates):
            return LiveCommercialGateStatus.BLOCKED
        return LiveCommercialGateStatus.PASS

    @property
    def commercial_v1_ready(self) -> bool:
        return len(self.gates) == 5 and self.status is LiveCommercialGateStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "devagent-live-commercial-readiness-v1",
            "mode": "READ_ONLY",
            "generated_at": self.generated_at.isoformat(),
            "status": self.status.value,
            "commercial_v1_ready": self.commercial_v1_ready,
            "min_soak_hours": self.min_soak_hours,
            "gates": [
                {
                    "gate_id": item.gate_id,
                    "title": item.title,
                    "status": item.status.value,
                    "detail": item.detail,
                }
                for item in self.gates
            ],
        }


_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


def _load_artifact(path: Path | None, schema: str) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "artifact not supplied"
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_file():
        return None, f"artifact does not exist: {target}"
    payload = target.read_bytes()
    if len(payload) > _MAX_ARTIFACT_BYTES:
        return None, f"artifact exceeds {_MAX_ARTIFACT_BYTES} bytes"
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"artifact is not valid UTF-8 JSON: {exc}"
    if not isinstance(data, dict):
        return None, "artifact root is not a JSON object"
    if data.get("schema") != schema:
        return None, f"artifact schema must be {schema!r}"
    if data.get("mode") != "READ_ONLY":
        return None, "artifact mode must be READ_ONLY"
    return data, None


def _runtime_gate(path: Path | None) -> LiveCommercialGateResult:
    data, error = _load_artifact(path, "devagent-live-release-qualification-v1")
    if error:
        return LiveCommercialGateResult(
            "CV1-001",
            "Real OPC UA runtime qualification",
            LiveCommercialGateStatus.BLOCKED,
            error,
        )
    assert data is not None
    counts = data.get("counts")
    cases = data.get("cases")
    valid = (
        data.get("status") == "PASS"
        and isinstance(counts, dict)
        and counts.get("PASS") == 14
        and counts.get("FAIL") == 0
        and counts.get("BLOCKED") == 0
        and isinstance(cases, list)
        and len(cases) == 14
        and all(isinstance(item, dict) and item.get("status") == "PASS" for item in cases)
    )
    return LiveCommercialGateResult(
        "CV1-001",
        "Real OPC UA runtime qualification",
        LiveCommercialGateStatus.PASS if valid else LiveCommercialGateStatus.FAIL,
        (
            "Exact read-only runtime matrix is PASS=14 FAIL=0 BLOCKED=0."
            if valid
            else "Runtime qualification artifact is present but does not prove exact 14/14 PASS."
        ),
    )


def _vendor_gate(path: Path | None) -> LiveCommercialGateResult:
    data, error = _load_artifact(path, "devagent-live-vendor-qualification-v1")
    if error:
        return LiveCommercialGateResult(
            "CV1-002",
            "Rockwell + Siemens + Schneider real endpoint qualification",
            LiveCommercialGateStatus.BLOCKED,
            error,
        )
    assert data is not None
    vendors = data.get("vendors")
    names = {
        str(item.get("vendor"))
        for item in vendors
        if isinstance(item, dict) and item.get("status") == "PASS"
    } if isinstance(vendors, list) else set()
    valid = (
        data.get("status") == "PASS"
        and data.get("all_required_vendors_pass") is True
        and names == set(REQUIRED_VENDOR_FAMILIES)
    )
    return LiveCommercialGateResult(
        "CV1-002",
        "Rockwell + Siemens + Schneider real endpoint qualification",
        LiveCommercialGateStatus.PASS if valid else LiveCommercialGateStatus.FAIL,
        (
            "All three required vendor families passed project parse + real OPC UA + exact mapping + trusted CURRENT capture."
            if valid
            else "Vendor artifact does not prove PASS for exactly Rockwell, Siemens, and Schneider."
        ),
    )


def _stateful_self_check() -> LiveCommercialGateResult:
    timer = LiveStatefulModel(
        id="SELF-TON",
        vendor="ROCKWELL",
        kind=LiveStatefulKind.TIMER,
        name="T1",
        instruction="TON",
        semantic_state="RUNTIME_REQUIRED",
        source_locator="self:timer",
        guard_paths=((('StartPermissive', True),),),
    )
    timer_result = diagnose_live_stateful_model(timer, {"StartPermissive": False})
    transition = LiveStatefulTransition(
        source_state="1",
        target_state="2",
        guard_paths=((('Ready', True),),),
        runtime_dependencies=(),
        source_locator="self:state:2",
    )
    machine = LiveStatefulModel(
        id="SELF-SM",
        vendor="SIEMENS",
        kind=LiveStatefulKind.STATE_MACHINE,
        name="State",
        instruction="CASE_STATE_MACHINE",
        semantic_state="FULL",
        source_locator="self:state",
        states=("1", "2"),
        transitions=(transition,),
    )
    state_result = diagnose_live_stateful_model(
        machine,
        {"State": 1, "Ready": True},
    )
    valid = (
        timer_result.status is LiveStatefulDiagnosisStatus.TRANSITION_BLOCKED
        and state_result.status is LiveStatefulDiagnosisStatus.TRANSITION_READY
        and state_result.candidate_targets == ("2",)
    )
    return LiveCommercialGateResult(
        "CV1-003",
        "Stateful timer/counter and sequence diagnosis",
        LiveCommercialGateStatus.PASS if valid else LiveCommercialGateStatus.FAIL,
        (
            "Deterministic stateful engine blocks a disabled timer path and proves a bounded ready state transition without inferring time/retentive behavior."
            if valid
            else "Deterministic stateful/sequence self-check failed."
        ),
    )


def _history_self_check() -> LiveCommercialGateResult:
    now = datetime.now(timezone.utc)
    target = LiveEngineeringTag(
        id="TAG-OUT",
        name="ConveyorRun",
        scope="Controller",
        data_type="BOOL",
        description=None,
        external_access="Read Only",
        alias_for=None,
    )
    dependency = LiveEngineeringTag(
        id="TAG-PE",
        name="JamPhotoeye",
        scope="Controller",
        data_type="BOOL",
        description=None,
        external_access="Read Only",
        alias_for=None,
    )
    context = LiveEngineeringContext(
        vendor="SELF",
        engineering_tool="SELF",
        controller_name="SELF",
        source_path="self",
        source_sha256="self",
        full_project=True,
        tags=(target, dependency),
        rules=(),
        statements=(),
        limitations=(),
    )
    store = LiveTimelineStore(retention_seconds=120.0)
    samples = (
        LiveHistoricalSample(now - timedelta(seconds=6), "p", "TAG-PE", "JamPhotoeye", "n1", False, True, "GOOD", "CURRENT"),
        LiveHistoricalSample(now - timedelta(seconds=4), "p", "TAG-OUT", "ConveyorRun", "n2", True, True, "GOOD", "CURRENT"),
        LiveHistoricalSample(now - timedelta(seconds=3), "p", "TAG-PE", "JamPhotoeye", "n1", True, True, "GOOD", "CURRENT"),
        LiveHistoricalSample(now - timedelta(seconds=2), "p", "TAG-OUT", "ConveyorRun", "n2", False, True, "GOOD", "CURRENT"),
    )
    store.append_many(samples)
    diagnosis = store.diagnose_recent_transition(
        context,
        "ConveyorRun",
        dependency_tag_ids=("TAG-PE",),
        lookback_seconds=30,
        now=now,
    )
    valid = (
        diagnosis.transition is not None
        and diagnosis.transition.new_value is False
        and len(diagnosis.preceding_changes) == 1
        and diagnosis.preceding_changes[0].tag_id == "TAG-PE"
    )
    return LiveCommercialGateResult(
        "CV1-004",
        "Historical fault timeline",
        LiveCommercialGateStatus.PASS if valid else LiveCommercialGateStatus.FAIL,
        (
            "Trusted ring-buffer timeline identifies a dependency transition before a target stop while explicitly withholding physical-causation proof."
            if valid
            else "Historical transition/candidate self-check failed."
        ),
    )


def _ops_gate(
    doctor_path: Path | None,
    soak_path: Path | None,
    *,
    min_soak_hours: float,
) -> LiveCommercialGateResult:
    doctor, doctor_error = _load_artifact(doctor_path, "devagent-live-doctor-v1")
    soak, soak_error = _load_artifact(soak_path, "devagent-live-soak-v1")
    missing = [item for item in (doctor_error, soak_error) if item]
    if missing:
        return LiveCommercialGateResult(
            "CV1-005",
            "Production install/doctor and long-running soak",
            LiveCommercialGateStatus.BLOCKED,
            "; ".join(missing),
        )
    assert doctor is not None and soak is not None
    duration = float(soak.get("actual_duration_seconds", 0.0) or 0.0)
    required = min_soak_hours * 3600.0
    valid = (
        doctor.get("status") == "PASS"
        and soak.get("status") == "PASS"
        and duration >= required
    )
    detail = (
        f"Doctor PASS and soak PASS for {duration / 3600.0:.2f}h (required >= {min_soak_hours:.2f}h)."
        if valid
        else (
            f"Operations evidence is insufficient: doctor={doctor.get('status')} soak={soak.get('status')} "
            f"duration={duration / 3600.0:.2f}h required={min_soak_hours:.2f}h."
        )
    )
    return LiveCommercialGateResult(
        "CV1-005",
        "Production install/doctor and long-running soak",
        LiveCommercialGateStatus.PASS if valid else LiveCommercialGateStatus.FAIL,
        detail,
    )


def evaluate_live_commercial_readiness(
    *,
    runtime_qualification_path: Path | None = None,
    vendor_qualification_path: Path | None = None,
    doctor_path: Path | None = None,
    soak_path: Path | None = None,
    min_soak_hours: float = 8.0,
) -> LiveCommercialReadinessReport:
    if min_soak_hours <= 0:
        raise ValueError("min_soak_hours must be > 0")
    return LiveCommercialReadinessReport(
        generated_at=datetime.now(timezone.utc),
        min_soak_hours=min_soak_hours,
        gates=(
            _runtime_gate(runtime_qualification_path),
            _vendor_gate(vendor_qualification_path),
            _stateful_self_check(),
            _history_self_check(),
            _ops_gate(doctor_path, soak_path, min_soak_hours=min_soak_hours),
        ),
    )


def _write_json(path: Path, value: dict[str, Any]) -> tuple[str, int]:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def write_live_commercial_readiness_artifacts(
    output_dir: Path,
    report: LiveCommercialReadinessReport,
) -> Path:
    target = Path(output_dir).expanduser().resolve(strict=False)
    target.mkdir(parents=True, exist_ok=False)
    try:
        report_path = target / "live_commercial_readiness.json"
        sha, size = _write_json(report_path, report.as_dict())
        _write_json(
            target / "manifest.json",
            {
                "schema": "devagent-live-commercial-readiness-manifest-v1",
                "mode": "READ_ONLY",
                "status": report.status.value,
                "commercial_v1_ready": report.commercial_v1_ready,
                "artifacts": {report_path.name: {"sha256": sha, "bytes": size}},
            },
        )
        return target
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


__all__ = [
    "LiveCommercialGateStatus",
    "LiveCommercialGateResult",
    "LiveCommercialReadinessReport",
    "evaluate_live_commercial_readiness",
    "write_live_commercial_readiness_artifacts",
]
