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


TRUST_STORE_SCHEMA = "devagent-plc-trusted-signers-v1"
_ALLOWED_PURPOSES = {
    "RELEASE_POLICY",
    "EXECUTION_BACKEND_REGISTRY",
    "EXECUTION_RESULTS",
    "HUMAN_APPROVAL",
    "RUNTIME_PROJECT_BINDING",
}
_KEY_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_TRUST_STORE_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
_MAX_SIGNERS = 100


@dataclass(frozen=True)
class TrustedSigner:
    id: str
    public_key_base64: str
    purposes: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class TrustedSignerStore:
    source_path: str
    source_sha256: str
    approved_by: str
    approved_at: str
    signers: tuple[TrustedSigner, ...]

    def jsonable(self) -> dict[str, Any]:
        return {
            "schema": TRUST_STORE_SCHEMA,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "signers": [
                {
                    "id": item.id,
                    "algorithm": "ED25519",
                    "public_key_base64": item.public_key_base64,
                    "purposes": list(item.purposes),
                    "status": item.status,
                }
                for item in self.signers
            ],
        }


def _parse_timestamp(value: str, *, field: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"PLC trusted signer store {field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PLC trusted signer store {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_trusted_signer_store(path: Path | None) -> TrustedSignerStore | None:
    if path is None:
        return None
    target = path.expanduser().resolve(strict=True)
    payload = target.read_bytes()
    if len(payload) > _MAX_TRUST_STORE_BYTES:
        raise ValueError("PLC trusted signer store exceeds 1 MiB production limit")
    loaded = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError("PLC trusted signer store must be a JSON object")
    if loaded.get("schema") != TRUST_STORE_SCHEMA:
        raise ValueError(f"PLC trusted signer store schema must be {TRUST_STORE_SCHEMA}")
    approved_by = str(loaded.get("approved_by") or "").strip()
    approved_at = str(loaded.get("approved_at") or "").strip()
    if not approved_by or not approved_at:
        raise ValueError("PLC trusted signer store requires approved_by and approved_at")
    _parse_timestamp(approved_at, field="approved_at")
    rows = loaded.get("signers")
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_SIGNERS:
        raise ValueError(f"PLC trusted signer store requires 1..{_MAX_SIGNERS} signers")
    signers: list[TrustedSigner] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("PLC trusted signer entries must be JSON objects")
        signer_id = str(raw.get("id") or "").strip()
        if _KEY_ID.fullmatch(signer_id) is None:
            raise ValueError("PLC trusted signer store contains invalid signer id")
        if signer_id.casefold() in seen:
            raise ValueError(f"PLC trusted signer store contains duplicate signer id: {signer_id}")
        seen.add(signer_id.casefold())
        if str(raw.get("algorithm") or "").upper() != "ED25519":
            raise ValueError(f"Trusted signer {signer_id} must use ED25519")
        status = str(raw.get("status") or "TRUSTED").upper()
        if status not in {"TRUSTED", "REVOKED"}:
            raise ValueError(f"Trusted signer {signer_id} has unsupported status: {status}")
        purposes_raw = raw.get("purposes")
        if not isinstance(purposes_raw, list) or not purposes_raw:
            raise ValueError(f"Trusted signer {signer_id} requires at least one purpose")
        purposes: list[str] = []
        for value in purposes_raw:
            purpose = str(value).upper()
            if purpose != "*" and purpose not in _ALLOWED_PURPOSES:
                raise ValueError(f"Trusted signer {signer_id} has unsupported purpose: {purpose}")
            if purpose not in purposes:
                purposes.append(purpose)
        public_key_base64 = str(raw.get("public_key_base64") or "").strip()
        try:
            key_bytes = base64.b64decode(public_key_base64, validate=True)
            Ed25519PublicKey.from_public_bytes(key_bytes)
        except Exception as exc:
            raise ValueError(f"Trusted signer {signer_id} has invalid Ed25519 public key") from exc
        signers.append(
            TrustedSigner(
                id=signer_id,
                public_key_base64=public_key_base64,
                purposes=tuple(purposes),
                status=status,
            )
        )
    return TrustedSignerStore(
        source_path=str(target),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        approved_by=approved_by,
        approved_at=approved_at,
        signers=tuple(signers),
    )


def _canonical_signed_payload(loaded: dict[str, Any]) -> bytes:
    payload = dict(loaded)
    payload.pop("signature", None)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def verify_signed_json_artifact(
    path: Path | None,
    *,
    purpose: str,
    trust_store: TrustedSignerStore | None,
    required: bool,
) -> dict[str, Any] | None:
    if path is None:
        if required:
            raise ValueError(f"Signed PLC artifact required for {purpose}, but no artifact was supplied")
        return None
    target = path.expanduser().resolve(strict=True)
    payload = target.read_bytes()
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"Signed PLC artifact exceeds {_MAX_ARTIFACT_BYTES} byte limit: {target}")
    loaded = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Signed PLC artifact for {purpose} must be a JSON object")
    signature = loaded.get("signature")
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
    signature_base64 = str(signature.get("value_base64") or "").strip()
    try:
        signature_bytes = base64.b64decode(signature_base64, validate=True)
        public_bytes = base64.b64decode(signer.public_key_base64, validate=True)
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature_bytes,
            _canonical_signed_payload(loaded),
        )
    except (ValueError, InvalidSignature) as exc:
        raise ValueError(f"PLC artifact signature verification failed for {purpose}") from exc
    return {
        "purpose": normalized_purpose,
        "algorithm": "ED25519",
        "key_id": key_id,
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "trust_store_sha256": trust_store.source_sha256,
        "source_path": str(target),
    }
