from __future__ import annotations

import json
from pathlib import Path

import pytest

from devagent.plc.production_models import RequirementCriticality
from devagent.plc.release_policy import load_release_policy


def _write_policy(tmp_path: Path, **overrides) -> Path:
    payload = {
        "schema": "devagent-plc-release-policy-v1",
        "policy_id": "warehouse-prod-v1",
        "approved_by": "Controls Engineering Manager",
        "approved_at": "2026-08-26T13:00:00Z",
        "require_baseline_for": ["CRITICAL"],
        "require_dynamic_for": ["CRITICAL", "HIGH"],
        "allowed_backend_kinds": ["SIMULATOR", "HIL"],
        "max_deterministic_risks": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 3},
        "require_all_generated_tests_pass": True,
        "require_human_approval": True,
        "require_signatures_for": [
            "EXECUTION_BACKEND_REGISTRY",
            "EXECUTION_RESULTS",
            "HUMAN_APPROVAL",
        ],
    }
    payload.update(overrides)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def test_release_policy_is_versioned_hashed_and_criticality_aware(tmp_path: Path) -> None:
    policy = load_release_policy(_write_policy(tmp_path))

    assert policy.policy_id == "warehouse-prod-v1"
    assert policy.require_baseline_for == (RequirementCriticality.CRITICAL,)
    assert policy.require_dynamic_for == (
        RequirementCriticality.CRITICAL,
        RequirementCriticality.HIGH,
    )
    assert policy.allowed_backend_kinds == ("SIMULATOR", "HIL")
    assert policy.max_deterministic_critical == 0
    assert policy.max_deterministic_high == 0
    assert policy.max_deterministic_medium == 3
    assert len(policy.source_sha256) == 64


def test_release_policy_cannot_disable_human_engineering_approval(tmp_path: Path) -> None:
    path = _write_policy(tmp_path, require_human_approval=False)

    with pytest.raises(ValueError, match="cannot disable human engineering approval"):
        load_release_policy(path)


def test_release_policy_rejects_unknown_backend_and_signature_purposes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported backend kind"):
        load_release_policy(_write_policy(tmp_path, allowed_backend_kinds=["UNTRUSTED_SCRIPT"]))

    with pytest.raises(ValueError, match="unsupported signature purpose"):
        load_release_policy(_write_policy(tmp_path, require_signatures_for=["AI_APPROVAL"]))


def test_builtin_policy_is_conservative_and_stable() -> None:
    first = load_release_policy(None)
    second = load_release_policy(None)

    assert first.builtin is True
    assert first.source_sha256 == second.source_sha256
    assert RequirementCriticality.CRITICAL in first.require_dynamic_for
    assert RequirementCriticality.HIGH in first.require_dynamic_for
    assert first.max_deterministic_critical == 0
    assert first.max_deterministic_high == 0
    assert first.require_all_generated_tests_pass is True
    assert first.require_human_approval is True
