from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from devagent.plc.signature_trust import (
    load_trusted_signer_store,
    verify_signed_json_artifact,
)


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(public).decode("ascii")


def _write_store(
    tmp_path: Path,
    public_key: str,
    *,
    purposes: list[str] | None = None,
    status: str = "TRUSTED",
) -> Path:
    path = tmp_path / "trust-store.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "Plant Security Owner",
                "approved_at": "2026-08-26T13:00:00Z",
                "signers": [
                    {
                        "id": "controls-release-key",
                        "algorithm": "ED25519",
                        "public_key_base64": public_key,
                        "purposes": purposes or ["RELEASE_POLICY"],
                        "status": status,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _write_signed(
    tmp_path: Path,
    private: Ed25519PrivateKey,
    payload: dict,
    *,
    name: str = "artifact.json",
) -> Path:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    signed = dict(payload)
    signed["signature"] = {
        "algorithm": "ED25519",
        "key_id": "controls-release-key",
        "value_base64": base64.b64encode(private.sign(canonical)).decode("ascii"),
    }
    path = tmp_path / name
    path.write_text(json.dumps(signed, sort_keys=True), encoding="utf-8")
    return path


def test_ed25519_signed_plc_artifact_is_verified_against_operator_trust_store(tmp_path: Path) -> None:
    private, public = _keypair()
    store = load_trusted_signer_store(_write_store(tmp_path, public))
    artifact = _write_signed(
        tmp_path,
        private,
        {"schema": "devagent-plc-release-policy-v1", "policy_id": "plant-prod"},
    )

    record = verify_signed_json_artifact(
        artifact,
        purpose="RELEASE_POLICY",
        trust_store=store,
        required=True,
    )

    assert record is not None
    assert record["algorithm"] == "ED25519"
    assert record["key_id"] == "controls-release-key"
    assert len(record["artifact_sha256"]) == 64


def test_tampering_signed_plc_artifact_fails_closed(tmp_path: Path) -> None:
    private, public = _keypair()
    store = load_trusted_signer_store(_write_store(tmp_path, public))
    artifact = _write_signed(
        tmp_path,
        private,
        {"schema": "devagent-plc-release-policy-v1", "policy_id": "plant-prod"},
    )
    loaded = json.loads(artifact.read_text(encoding="utf-8"))
    loaded["policy_id"] = "attacker-modified-policy"
    artifact.write_text(json.dumps(loaded, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="signature verification failed"):
        verify_signed_json_artifact(
            artifact,
            purpose="RELEASE_POLICY",
            trust_store=store,
            required=True,
        )


def test_signer_purpose_and_revocation_are_enforced(tmp_path: Path) -> None:
    private, public = _keypair()
    artifact = _write_signed(
        tmp_path,
        private,
        {"schema": "devagent-plc-release-policy-v1", "policy_id": "plant-prod"},
    )

    wrong_purpose_store = load_trusted_signer_store(
        _write_store(tmp_path, public, purposes=["EXECUTION_RESULTS"])
    )
    with pytest.raises(ValueError, match="not trusted for purpose RELEASE_POLICY"):
        verify_signed_json_artifact(
            artifact,
            purpose="RELEASE_POLICY",
            trust_store=wrong_purpose_store,
            required=True,
        )

    revoked_store = load_trusted_signer_store(
        _write_store(tmp_path, public, purposes=["RELEASE_POLICY"], status="REVOKED")
    )
    with pytest.raises(ValueError, match="is not trusted"):
        verify_signed_json_artifact(
            artifact,
            purpose="RELEASE_POLICY",
            trust_store=revoked_store,
            required=True,
        )


def test_unsigned_required_artifact_is_rejected(tmp_path: Path) -> None:
    _, public = _keypair()
    store = load_trusted_signer_store(_write_store(tmp_path, public))
    artifact = tmp_path / "unsigned.json"
    artifact.write_text(json.dumps({"policy_id": "unsigned"}), encoding="utf-8")

    with pytest.raises(ValueError, match="requires an Ed25519 signature"):
        verify_signed_json_artifact(
            artifact,
            purpose="RELEASE_POLICY",
            trust_store=store,
            required=True,
        )
