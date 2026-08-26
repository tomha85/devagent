from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from devagent.plc.execution_trust import (
    ExecutionBackendQualification,
    ExecutionBackendRegistry,
    REGISTRY_SCHEMA,
    require_qualified_backend,
)
from devagent.plc.production_models import ExecutionStatus, TestExecutionEvidence
from devagent.plc.release_policy import (
    PLCReleasePolicy,
    RELEASE_POLICY_SCHEMA,
    _backend_kinds,
    _boolean,
    _criticalities,
    _parse_timestamp as _parse_policy_timestamp,
    _risk_limit,
    _signature_purposes,
)
from devagent.plc.signature_trust import TrustedSignerStore

_EXECUTION_SCHEMA = "devagent-plc-execution-results-v1"
_BACKEND_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_BACKEND_KINDS = {"SIMULATOR", "HIL", "CONTROLLER"}
_ALLOWED_BACKEND_STATUS = {"QUALIFIED", "EXPERIMENTAL", "REVOKED"}


@dataclass(frozen=True)
class JSONArtifactSnapshot:
    source_path: str
    payload: bytes
    sha256: str
    data: dict[str, Any]


def read_json_snapshot(path: Path, *, max_bytes: int, purpose: str) -> JSONArtifactSnapshot:
    target = path.expanduser().resolve(strict=True)
    payload = target.read_bytes()
    if len(payload) > max_bytes:
        raise ValueError(f"PLC artifact for {purpose} exceeds {max_bytes} byte production limit")
    try:
        loaded = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"PLC artifact for {purpose} must be valid UTF-8 JSON") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"PLC artifact for {purpose} must be a JSON object")
    return JSONArtifactSnapshot(
        source_path=str(target),
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        data=loaded,
    )


def _canonical_signed_payload(loaded: dict[str, Any]) -> bytes:
    unsigned = dict(loaded)
    unsigned.pop("signature", None)
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def verify_snapshot_signature(
    snapshot: JSONArtifactSnapshot | None,
    *,
    purpose: str,
    trust_store: TrustedSignerStore | None,
    required: bool,
) -> dict[str, Any] | None:
    if snapshot is None:
        if required:
            raise ValueError(f"Signed PLC artifact required for {purpose}, but no artifact was supplied")
        return None
    signature = snapshot.data.get("signature")
    if signature is None:
        if required:
            raise ValueError(f"PLC artifact for {purpose} requires an Ed25519 signature")
        return None
    if trust_store is None:
        raise ValueError(f"PLC artifact for {purpose} is signed but no --trust-store was supplied")
    if not isinstance(signature, dict):
        raise ValueError(f"PLC artifact signature for {purpose} must be an object")
    if str(signature.get("algorithm") or "").upper() != "ED25519":
        raise ValueError(f"PLC artifact signature for {purpose} must use ED25519")
    key_id = str(signature.get("key_id") or "").strip()
    signer = next((item for item in trust_store.signers if item.id == key_id), None)
    if signer is None:
        raise ValueError(f"PLC artifact signer {key_id!r} is not in the supplied trust store")
    if signer.status != "TRUSTED":
        raise ValueError(f"PLC artifact signer {key_id!r} is not trusted (status={signer.status})")
    normalized_purpose = purpose.upper()
    if "*" not in signer.purposes and normalized_purpose not in signer.purposes:
        raise ValueError(f"PLC artifact signer {key_id!r} is not trusted for purpose {normalized_purpose}")
    try:
        signature_bytes = base64.b64decode(str(signature.get("value_base64") or "").strip(), validate=True)
        public_bytes = base64.b64decode(signer.public_key_base64, validate=True)
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature_bytes,
            _canonical_signed_payload(snapshot.data),
        )
    except (ValueError, InvalidSignature) as exc:
        raise ValueError(f"PLC artifact signature verification failed for {purpose}") from exc
    return {
        "purpose": normalized_purpose,
        "algorithm": "ED25519",
        "key_id": key_id,
        "artifact_sha256": snapshot.sha256,
        "trust_store_sha256": trust_store.source_sha256,
        "source_path": snapshot.source_path,
    }


def parse_release_policy_snapshot(snapshot: JSONArtifactSnapshot) -> PLCReleasePolicy:
    loaded = snapshot.data
    if loaded.get("schema") != RELEASE_POLICY_SCHEMA:
        raise ValueError(f"PLC release policy schema must be {RELEASE_POLICY_SCHEMA}")
    policy_id = str(loaded.get("policy_id") or "").strip()
    approved_by = str(loaded.get("approved_by") or "").strip()
    approved_at = str(loaded.get("approved_at") or "").strip()
    if not policy_id or not approved_by or not approved_at:
        raise ValueError("PLC release policy requires policy_id, approved_by, and approved_at")
    _parse_policy_timestamp(approved_at, field="approved_at")
    if not _boolean(loaded.get("require_human_approval"), field="require_human_approval", default=True):
        raise ValueError("PLC release policy cannot disable human engineering approval")
    require_all_tests = _boolean(
        loaded.get("require_all_generated_tests_pass"),
        field="require_all_generated_tests_pass",
        default=True,
    )
    limits = loaded.get("max_deterministic_risks")
    return PLCReleasePolicy(
        policy_id=policy_id,
        approved_by=approved_by,
        approved_at=approved_at,
        require_baseline_for=_criticalities(loaded.get("require_baseline_for", []), field="require_baseline_for"),
        require_dynamic_for=_criticalities(loaded.get("require_dynamic_for", ["CRITICAL", "HIGH"]), field="require_dynamic_for"),
        allowed_backend_kinds=_backend_kinds(loaded.get("allowed_backend_kinds", ["SIMULATOR", "HIL", "CONTROLLER"])),
        max_deterministic_critical=_risk_limit(limits, "CRITICAL", 0),
        max_deterministic_high=_risk_limit(limits, "HIGH", 0),
        max_deterministic_medium=_risk_limit(limits, "MEDIUM", 10_000),
        require_all_generated_tests_pass=require_all_tests,
        require_human_approval=True,
        require_signatures_for=_signature_purposes(loaded.get("require_signatures_for", [])),
        source_path=snapshot.source_path,
        source_sha256=snapshot.sha256,
        builtin=False,
    )


def _parse_utc_timestamp(value: str, *, label: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _project_scope(value: Any, backend_id: str) -> tuple[str, ...]:
    if value is None:
        raise ValueError(
            f"Backend {backend_id} requires explicit project_sha256 scope; use '*' only for intentional global qualification"
        )
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else None
    if values is None:
        raise ValueError(f"Backend {backend_id} project_sha256 must be '*' or a list of SHA-256 values")
    result: list[str] = []
    for raw in values:
        item = str(raw).strip()
        if item != "*" and _HASH.fullmatch(item) is None:
            raise ValueError(f"Backend {backend_id} contains an invalid project SHA-256 scope")
        normalized = item.lower() if item != "*" else item
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError(f"Backend {backend_id} project scope cannot be empty")
    return tuple(result)


def parse_backend_registry_snapshot(snapshot: JSONArtifactSnapshot) -> ExecutionBackendRegistry:
    loaded = snapshot.data
    if loaded.get("schema") != REGISTRY_SCHEMA:
        raise ValueError(f"Execution backend registry schema must be {REGISTRY_SCHEMA}")
    approved_by = str(loaded.get("approved_by") or "").strip()
    approved_at = str(loaded.get("approved_at") or "").strip()
    if not approved_by or not approved_at:
        raise ValueError("Execution backend registry requires approved_by and approved_at")
    _parse_utc_timestamp(approved_at, label="Execution backend registry approved_at")
    rows = loaded.get("backends")
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise ValueError("Execution backend registry requires 1..100 backend entries")
    backends: list[ExecutionBackendQualification] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("Execution backend registry entries must be JSON objects")
        backend_id = str(raw.get("id") or "").strip()
        if _BACKEND_ID.fullmatch(backend_id) is None:
            raise ValueError("Execution backend registry contains an invalid backend id")
        if backend_id.casefold() in seen:
            raise ValueError(f"Execution backend registry contains duplicate backend id: {backend_id}")
        seen.add(backend_id.casefold())
        kind = str(raw.get("kind") or "").upper()
        status = str(raw.get("status") or "").upper()
        if kind not in _ALLOWED_BACKEND_KINDS:
            raise ValueError(f"Backend {backend_id} has unsupported kind: {kind}")
        if status not in _ALLOWED_BACKEND_STATUS:
            raise ValueError(f"Backend {backend_id} has unsupported qualification status: {status}")
        raw_evidence = raw.get("qualification_evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence or len(raw_evidence) > 32:
            raise ValueError(f"Backend {backend_id} requires 1..32 qualification_evidence entries")
        evidence = tuple(str(item).strip() for item in raw_evidence if str(item).strip())
        if not evidence or any(len(item) > 2048 for item in evidence):
            raise ValueError(f"Backend {backend_id} qualification evidence is empty or too long")
        expires_at = str(raw.get("expires_at") or "").strip() or None
        if expires_at is not None:
            _parse_utc_timestamp(expires_at, label=f"Execution backend {backend_id} expires_at")
        backends.append(
            ExecutionBackendQualification(
                id=backend_id,
                kind=kind,
                status=status,
                project_sha256=_project_scope(raw.get("project_sha256"), backend_id),
                qualification_evidence=evidence,
                expires_at=expires_at,
            )
        )
    return ExecutionBackendRegistry(
        source_path=snapshot.source_path,
        source_sha256=snapshot.sha256,
        approved_by=approved_by,
        approved_at=approved_at,
        backends=tuple(backends),
    )


def parse_execution_results_snapshot(
    snapshot: JSONArtifactSnapshot,
    *,
    project_sha256: str,
    test_plan_sha256: str,
    test_ids: set[str],
    backend_registry: ExecutionBackendRegistry | None,
) -> list[TestExecutionEvidence]:
    loaded = snapshot.data
    if loaded.get("schema") != _EXECUTION_SCHEMA:
        raise ValueError(f"Execution evidence schema must be {_EXECUTION_SCHEMA}")
    if str(loaded.get("project_sha256", "")) != project_sha256:
        raise ValueError("Execution evidence project_sha256 does not match the analyzed PLC artifact")
    if str(loaded.get("test_plan_sha256", "")) != test_plan_sha256:
        raise ValueError("Execution evidence test_plan_sha256 does not match the generated FAT plan")
    backend = str(loaded.get("backend") or "").strip()
    run_id = str(loaded.get("run_id") or "").strip()
    if not backend or not run_id:
        raise ValueError("Execution evidence requires non-empty backend and run_id")
    qualification = require_qualified_backend(backend_registry, backend, project_sha256)
    if loaded.get("backend_registry_sha256") != backend_registry.source_sha256:
        raise ValueError("Execution evidence backend_registry_sha256 does not match the supplied qualification registry")
    rows = loaded.get("results")
    if not isinstance(rows, list):
        raise ValueError("Execution evidence requires a results list")
    result: list[TestExecutionEvidence] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Execution evidence result entries must be objects")
        test_id = str(row.get("test_id") or "")
        if test_id not in test_ids:
            raise ValueError(f"Execution evidence references unknown test_id: {test_id}")
        if test_id in seen:
            raise ValueError(f"Execution evidence contains duplicate test_id: {test_id}")
        seen.add(test_id)
        try:
            status = ExecutionStatus(str(row.get("status") or "").upper())
        except ValueError as exc:
            raise ValueError(f"Unsupported execution status for {test_id}: {row.get('status')}") from exc
        raw_evidence = row.get("evidence", [])
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 32:
            raise ValueError(f"Execution evidence for {test_id} requires an evidence list with at most 32 items")
        evidence = tuple(str(item) for item in raw_evidence if str(item).strip())
        if any(len(item) > 2048 for item in evidence):
            raise ValueError(f"Execution evidence reference for {test_id} exceeds 2048 characters")
        result.append(
            TestExecutionEvidence(
                test_id,
                status,
                qualification.id[:256],
                run_id[:256],
                str(row.get("observed"))[:8192] if row.get("observed") is not None else None,
                str(row.get("timestamp"))[:128] if row.get("timestamp") is not None else None,
                evidence,
            )
        )
    return result


def parse_approval_snapshot(snapshot: JSONArtifactSnapshot, *, expected: dict[str, Any]) -> dict[str, Any]:
    loaded = snapshot.data
    for field, value in expected.items():
        if loaded.get(field) != value:
            raise ValueError(f"Approval {field} does not match the current PLC verification context")
    if str(loaded.get("decision", "")).upper() != "APPROVE":
        raise ValueError("Approval artifact decision must be APPROVE")
    approved_by = str(loaded.get("approved_by", "")).strip()
    approved_at = str(loaded.get("approved_at", "")).strip()
    if not approved_by or not approved_at:
        raise ValueError("Approval artifact requires approved_by and approved_at")
    _parse_utc_timestamp(approved_at, label="Approval approved_at")
    return {
        "decision": "APPROVE",
        "approved_by": approved_by,
        "approved_at": approved_at,
        **expected,
        "source_path": snapshot.source_path,
    }
