from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from devagent.plc.production_models import RequirementCriticality


RELEASE_POLICY_SCHEMA = "devagent-plc-release-policy-v1"
_MAX_POLICY_BYTES = 1024 * 1024
_ALLOWED_BACKEND_KINDS = {"SIMULATOR", "HIL", "CONTROLLER"}
_ALLOWED_SIGNATURE_PURPOSES = {
    "RELEASE_POLICY",
    "EXECUTION_BACKEND_REGISTRY",
    "EXECUTION_RESULTS",
    "HUMAN_APPROVAL",
}


@dataclass(frozen=True)
class PLCReleasePolicy:
    policy_id: str
    approved_by: str
    approved_at: str
    require_baseline_for: tuple[RequirementCriticality, ...]
    require_dynamic_for: tuple[RequirementCriticality, ...]
    allowed_backend_kinds: tuple[str, ...]
    max_deterministic_critical: int
    max_deterministic_high: int
    max_deterministic_medium: int
    require_all_generated_tests_pass: bool
    require_human_approval: bool
    require_signatures_for: tuple[str, ...]
    source_path: str
    source_sha256: str
    builtin: bool = False

    def jsonable(self) -> dict[str, Any]:
        return {
            "schema": RELEASE_POLICY_SCHEMA,
            "policy_id": self.policy_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "require_baseline_for": [item.value for item in self.require_baseline_for],
            "require_dynamic_for": [item.value for item in self.require_dynamic_for],
            "allowed_backend_kinds": list(self.allowed_backend_kinds),
            "max_deterministic_risks": {
                "CRITICAL": self.max_deterministic_critical,
                "HIGH": self.max_deterministic_high,
                "MEDIUM": self.max_deterministic_medium,
            },
            "require_all_generated_tests_pass": self.require_all_generated_tests_pass,
            "require_human_approval": self.require_human_approval,
            "require_signatures_for": list(self.require_signatures_for),
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "builtin": self.builtin,
        }


def _criticalities(value: Any, *, field: str) -> tuple[RequirementCriticality, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"PLC release policy {field} must be a list")
    result: list[RequirementCriticality] = []
    for raw in value:
        try:
            item = RequirementCriticality(str(raw).upper())
        except ValueError as exc:
            raise ValueError(f"PLC release policy {field} contains invalid criticality: {raw!r}") from exc
        if item not in result:
            result.append(item)
    return tuple(result)


def _backend_kinds(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("PLC release policy allowed_backend_kinds must contain at least one backend kind")
    result: list[str] = []
    for raw in value:
        item = str(raw).upper()
        if item not in _ALLOWED_BACKEND_KINDS:
            raise ValueError(f"PLC release policy contains unsupported backend kind: {item}")
        if item not in result:
            result.append(item)
    return tuple(result)


def _risk_limit(value: Any, key: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, dict):
        raise ValueError("PLC release policy max_deterministic_risks must be an object")
    raw = value.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 10_000:
        raise ValueError(f"PLC release policy risk limit {key} must be an integer from 0 to 10000")
    return raw


def _signature_purposes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("PLC release policy require_signatures_for must be a list")
    result: list[str] = []
    for raw in value:
        item = str(raw).upper()
        if item not in _ALLOWED_SIGNATURE_PURPOSES:
            raise ValueError(f"PLC release policy contains unsupported signature purpose: {item}")
        if item not in result:
            result.append(item)
    return tuple(result)


def _builtin_policy() -> PLCReleasePolicy:
    canonical = {
        "schema": RELEASE_POLICY_SCHEMA,
        "policy_id": "builtin-production-default-v1",
        "approved_by": "DevAgent built-in policy",
        "approved_at": "2026-08-26T00:00:00Z",
        "require_baseline_for": [],
        "require_dynamic_for": ["CRITICAL", "HIGH"],
        "allowed_backend_kinds": ["SIMULATOR", "HIL", "CONTROLLER"],
        "max_deterministic_risks": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 10000},
        "require_all_generated_tests_pass": True,
        "require_human_approval": True,
        "require_signatures_for": [],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return PLCReleasePolicy(
        policy_id=canonical["policy_id"],
        approved_by=canonical["approved_by"],
        approved_at=canonical["approved_at"],
        require_baseline_for=(),
        require_dynamic_for=(RequirementCriticality.CRITICAL, RequirementCriticality.HIGH),
        allowed_backend_kinds=("SIMULATOR", "HIL", "CONTROLLER"),
        max_deterministic_critical=0,
        max_deterministic_high=0,
        max_deterministic_medium=10_000,
        require_all_generated_tests_pass=True,
        require_human_approval=True,
        require_signatures_for=(),
        source_path="<builtin>",
        source_sha256=digest,
        builtin=True,
    )


def load_release_policy(path: Path | None) -> PLCReleasePolicy:
    if path is None:
        return _builtin_policy()
    target = path.expanduser().resolve(strict=True)
    payload = target.read_bytes()
    if len(payload) > _MAX_POLICY_BYTES:
        raise ValueError("PLC release policy exceeds 1 MiB production limit")
    loaded = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError("PLC release policy must be a JSON object")
    if loaded.get("schema") != RELEASE_POLICY_SCHEMA:
        raise ValueError(f"PLC release policy schema must be {RELEASE_POLICY_SCHEMA}")
    policy_id = str(loaded.get("policy_id") or "").strip()
    approved_by = str(loaded.get("approved_by") or "").strip()
    approved_at = str(loaded.get("approved_at") or "").strip()
    if not policy_id or not approved_by or not approved_at:
        raise ValueError("PLC release policy requires policy_id, approved_by, and approved_at")
    if loaded.get("require_human_approval") is False:
        raise ValueError("PLC release policy cannot disable human engineering approval")
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
        require_all_generated_tests_pass=bool(loaded.get("require_all_generated_tests_pass", True)),
        require_human_approval=True,
        require_signatures_for=_signature_purposes(loaded.get("require_signatures_for", [])),
        source_path=str(target),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        builtin=False,
    )
