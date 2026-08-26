from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import devagent.plc.production_v5 as production_v5


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="TOCTOU" TargetType="Controller">
  <Controller Use="Target" Name="TOCTOU" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags><Tag Name="A" TagType="Base" DataType="BOOL" /><Tag Name="O" TagType="Base" DataType="BOOL" /></Tags>
    <Programs><Program Name="Main"><Routines><Routine Name="Logic" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[XIC(A)OTE(O);]]></Text></Rung>
    </RLLContent></Routine></Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS" /></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _sign(private: Ed25519PrivateKey, payload: dict) -> dict:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    signed = dict(payload)
    signed["signature"] = {
        "algorithm": "ED25519",
        "key_id": "root",
        "value_base64": base64.b64encode(private.sign(canonical)).decode("ascii"),
    }
    return signed


def test_v5_detects_backend_registry_change_after_signature_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust = tmp_path / "trust.json"
    trust.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "Security Owner",
                "approved_at": "2026-08-26T13:00:00Z",
                "signers": [
                    {
                        "id": "root",
                        "algorithm": "ED25519",
                        "public_key_base64": base64.b64encode(public).decode("ascii"),
                        "purposes": ["*"],
                        "status": "TRUSTED",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "Machine.L5X"
    project.write_text(PROJECT, encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry_payload = {
        "schema": "devagent-plc-execution-backend-registry-v1",
        "approved_by": "Controls Owner",
        "approved_at": "2026-08-26T13:00:00Z",
        "backends": [
            {
                "id": "sim",
                "kind": "SIMULATOR",
                "status": "QUALIFIED",
                "project_sha256": "*",
                "qualification_evidence": ["QUAL-1"],
            }
        ],
    }
    registry.write_text(json.dumps(_sign(private, registry_payload), sort_keys=True), encoding="utf-8")

    original_run = production_v5.run_v4_verification

    def mutate_then_run(*args, **kwargs):
        tampered = json.loads(registry.read_text(encoding="utf-8"))
        tampered["approved_by"] = "Changed after signature verification"
        registry.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(production_v5, "run_v4_verification", mutate_then_run)

    with pytest.raises(ValueError, match="changed between signature verification and deterministic use"):
        production_v5.run_production_verification_v5(
            project,
            execution_backend_registry_path=registry,
            trust_store_path=trust,
        )
