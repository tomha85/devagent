from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.release_policy import load_release_policy
from devagent.plc.signature_trust import load_trusted_signer_store


def _public_key_base64() -> str:
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _trust_store(tmp_path: Path, *, approved_at: str = "2026-08-26T13:00:00Z") -> Path:
    path = tmp_path / "trust.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "Plant Security Owner",
                "approved_at": approved_at,
                "signers": [
                    {
                        "id": "root",
                        "algorithm": "ED25519",
                        "public_key_base64": _public_key_base64(),
                        "purposes": ["*"],
                        "status": "TRUSTED",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_v5_unsigned_backend_registry_is_rejected_even_under_builtin_policy(tmp_path: Path) -> None:
    trust_store = _trust_store(tmp_path)
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-execution-backend-registry-v1",
                "approved_by": "Controls Owner",
                "approved_at": "2026-08-26T13:00:00Z",
                "backends": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires an Ed25519 signature"):
        run_production_verification_v5(
            tmp_path / "not-read-because-trust-fails-first.L5X",
            execution_backend_registry_path=registry,
            trust_store_path=trust_store,
        )


def test_release_policy_rejects_string_boolean_and_naive_timestamp(tmp_path: Path) -> None:
    base = {
        "schema": "devagent-plc-release-policy-v1",
        "policy_id": "strict-policy",
        "approved_by": "Controls Manager",
        "approved_at": "2026-08-26T13:00:00Z",
        "allowed_backend_kinds": ["SIMULATOR"],
        "require_human_approval": True,
    }
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({**base, "require_all_generated_tests_pass": "false"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a JSON boolean"):
        load_release_policy(policy)

    policy.write_text(
        json.dumps({**base, "approved_at": "2026-08-26T13:00:00"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must include a timezone"):
        load_release_policy(policy)


def test_trust_store_approval_timestamp_requires_timezone(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must include a timezone"):
        load_trusted_signer_store(
            _trust_store(tmp_path, approved_at="2026-08-26T13:00:00")
        )
