from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REGISTRY_SCHEMA = "devagent-plc-execution-backend-registry-v1"
_BACKEND_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_KINDS = {"SIMULATOR", "HIL", "CONTROLLER"}
_ALLOWED_STATUS = {"QUALIFIED", "EXPERIMENTAL", "REVOKED"}
_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_BACKENDS = 100


@dataclass(frozen=True)
class ExecutionBackendQualification:
    id: str
    kind: str
    status: str
    project_sha256: tuple[str, ...]
    qualification_evidence: tuple[str, ...]
    expires_at: str | None = None


@dataclass(frozen=True)
class ExecutionBackendRegistry:
    source_path: str
    source_sha256: str
    approved_by: str
    approved_at: str
    backends: tuple[ExecutionBackendQualification, ...]

    def jsonable(self) -> dict[str, Any]:
        return {
            "schema": REGISTRY_SCHEMA,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "backends": [asdict(item) for item in self.backends],
        }


def _parse_timestamp(value: str, *, field: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Execution backend registry {field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Execution backend registry {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _project_scope(value: Any, backend_id: str) -> tuple[str, ...]:
    if value is None:
        return ("*",)
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
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


def load_execution_backend_registry(path: Path | None) -> ExecutionBackendRegistry | None:
    if path is None:
        return None
    target = path.expanduser().resolve(strict=True)
    payload = target.read_bytes()
    if len(payload) > _MAX_REGISTRY_BYTES:
        raise ValueError("Execution backend registry exceeds 1 MiB production limit")
    source_sha = hashlib.sha256(payload).hexdigest()
    loaded = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError("Execution backend registry must be a JSON object")
    if loaded.get("schema") != REGISTRY_SCHEMA:
        raise ValueError(f"Execution backend registry schema must be {REGISTRY_SCHEMA}")
    approved_by = str(loaded.get("approved_by") or "").strip()
    approved_at = str(loaded.get("approved_at") or "").strip()
    if not approved_by or not approved_at:
        raise ValueError("Execution backend registry requires approved_by and approved_at")
    _parse_timestamp(approved_at, field="approved_at")
    rows = loaded.get("backends")
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_BACKENDS:
        raise ValueError(f"Execution backend registry requires 1..{_MAX_BACKENDS} backend entries")

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
        if kind not in _ALLOWED_KINDS:
            raise ValueError(f"Backend {backend_id} has unsupported kind: {kind}")
        if status not in _ALLOWED_STATUS:
            raise ValueError(f"Backend {backend_id} has unsupported qualification status: {status}")
        evidence = raw.get("qualification_evidence")
        if not isinstance(evidence, list) or not evidence or len(evidence) > 32:
            raise ValueError(f"Backend {backend_id} requires 1..32 qualification_evidence entries")
        evidence_items = tuple(str(item).strip() for item in evidence if str(item).strip())
        if not evidence_items or any(len(item) > 2048 for item in evidence_items):
            raise ValueError(f"Backend {backend_id} qualification evidence is empty or too long")
        expires_at = str(raw.get("expires_at") or "").strip() or None
        if expires_at is not None:
            _parse_timestamp(expires_at, field=f"backend {backend_id} expires_at")
        backends.append(
            ExecutionBackendQualification(
                id=backend_id,
                kind=kind,
                status=status,
                project_sha256=_project_scope(raw.get("project_sha256"), backend_id),
                qualification_evidence=evidence_items,
                expires_at=expires_at,
            )
        )
    return ExecutionBackendRegistry(
        source_path=str(target),
        source_sha256=source_sha,
        approved_by=approved_by,
        approved_at=approved_at,
        backends=tuple(backends),
    )


def require_qualified_backend(
    registry: ExecutionBackendRegistry | None,
    backend_id: str,
    project_sha256: str,
) -> ExecutionBackendQualification:
    if registry is None:
        raise ValueError(
            "Execution evidence requires --execution-backend-registry with a QUALIFIED backend policy artifact"
        )
    match = next((item for item in registry.backends if item.id == backend_id), None)
    if match is None:
        raise ValueError(f"Execution backend {backend_id!r} is not present in the supplied qualification registry")
    if match.status != "QUALIFIED":
        raise ValueError(f"Execution backend {backend_id!r} is not QUALIFIED (status={match.status})")
    normalized_project = project_sha256.lower()
    if "*" not in match.project_sha256 and normalized_project not in match.project_sha256:
        raise ValueError(f"Execution backend {backend_id!r} is not qualified for this project SHA-256")
    if match.expires_at is not None:
        expires = _parse_timestamp(match.expires_at, field=f"backend {backend_id} expires_at")
        if expires <= datetime.now(timezone.utc):
            raise ValueError(f"Execution backend {backend_id!r} qualification has expired")
    return match
