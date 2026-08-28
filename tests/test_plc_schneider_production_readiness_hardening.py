from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from devagent.plc.schneider_production_readiness import qualify_schneider_production_corpus


_SOURCE = '''<?xml version="1.0" encoding="UTF-8"?>
<STExchangeFile>
  <fileHeader company="Schneider Automation" product="EcoStruxure Control Expert V16" DTDVersion="41" />
  <contentHeader name="SharedIdentityNegative" version="1.0" />
  <program>
    <identProgram name="Main" type="section" task="MAST" />
    <STSource>
Run := Start;
    </STSource>
  </program>
  <dataBlock>
    <variables name="Start" typeName="BOOL" topologicalAddress="%I0.0" />
    <variables name="Run" typeName="BOOL" topologicalAddress="%Q0.0" />
  </dataBlock>
</STExchangeFile>
'''


def _signed_manifest(root: Path, cases: list[dict[str, object]]) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    payload: dict[str, object] = {
        "schema": "devagent-schneider-production-corpus-v1",
        "corpus_id": "hardware-hash-overlap-negative",
        "cases": cases,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload["signature"] = {
        "algorithm": "ED25519",
        "key_id": "schneider-hash-test",
        "value_base64": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }
    manifest = root / "corpus.json"
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust = root / "trust.json"
    trust.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "test-engineering-authority",
                "approved_at": "2026-08-28T12:00:00-04:00",
                "signers": [
                    {
                        "id": "schneider-hash-test",
                        "algorithm": "ED25519",
                        "public_key_base64": base64.b64encode(public_key).decode("ascii"),
                        "purposes": ["SCHNEIDER_PRODUCTION_CORPUS"],
                        "status": "TRUSTED",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, trust


def test_same_real_bundle_cannot_be_reused_for_different_hardware_families(tmp_path: Path) -> None:
    cases: list[dict[str, object]] = []
    for family, filename in (("M340", "m340.xst"), ("M580", "m580.xst"), ("UNITY_LEGACY", "unity.xst")):
        source = tmp_path / filename
        source.write_text(_SOURCE, encoding="utf-8")
        cases.append(
            {
                "id": family.lower(),
                "path": filename,
                "source_kind": "REAL_CONTROL_EXPERT_EXPORT",
                "origin": "LAB",
                "controller_family": family,
                "families": [family],
                "bundle_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "attested_by": "qualified-engineer@example.invalid",
                "exported_at": "2026-08-28T12:00:00-04:00",
            }
        )

    manifest, trust = _signed_manifest(tmp_path, cases)
    result = qualify_schneider_production_corpus(manifest, trust_store_path=trust)

    assert all(item.status == "PASS" for item in result.cases)
    assert result.status == "FAIL"
    assert result.commercial_static_ready is False
    assert any("reuse the same real export bundle identity" in item for item in result.blocking_findings)
