from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from devagent.plc.execution_trust import load_execution_backend_registry
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.rockwell_echo import (
    EXECUTION_REQUEST_SCHEMA,
    RUNTIME_BINDING_SCHEMA,
    RUNNER_SCHEMA,
    build_echo_execution_request,
    describe_echo_runner,
    load_runtime_binding,
    run_echo_execution,
)
from devagent.plc.signature_trust import load_trusted_signer_store


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="EchoTest" TargetType="Controller">
  <Controller Use="Target" Name="EchoTest" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Main" MainRoutineName="Logic"><Routines><Routine Name="Logic" Type="RLL"><RLLContent>
      <Rung Number="0"><Text><![CDATA[XIC(Start)XIC(Guard)OTE(Run);]]></Text></Rung>
    </RLLContent></Routine></Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="Main" /></ScheduledPrograms></Task></Tasks>
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
    result = dict(payload)
    result["signature"] = {
        "algorithm": "ED25519",
        "key_id": "root",
        "value_base64": base64.b64encode(private.sign(canonical)).decode("ascii"),
    }
    return result


def _trust_store(tmp_path: Path, private: Ed25519PrivateKey) -> Path:
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    path = tmp_path / "trust.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "Security",
                "approved_at": "2026-08-26T13:00:00Z",
                "signers": [
                    {
                        "id": "root",
                        "algorithm": "ED25519",
                        "public_key_base64": base64.b64encode(public).decode("ascii"),
                        "purposes": ["RUNTIME_PROJECT_BINDING", "EXECUTION_RESULTS"],
                        "status": "TRUSTED",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _runner(tmp_path: Path, private: Ed25519PrivateKey) -> Path:
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    encoded_private = base64.b64encode(private_bytes).decode("ascii")
    path = tmp_path / "echo_runner.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, hashlib, json, sys\n"
        "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey\n"
        f"KEY=Ed25519PrivateKey.from_private_bytes(base64.b64decode({encoded_private!r}))\n"
        "if '--describe' in sys.argv:\n"
        "  print(json.dumps({'schema':'devagent-rockwell-echo-runner-v1','adapter_id':'echo-qualified','adapter_version':'1.0','vendor':'Rockwell Automation','product':'FactoryTalk Logix Echo','backend_kind':'SIMULATOR','capabilities':['DOWNLOAD','SNAPSHOT','DATA_EXCHANGE','COSIMULATION'],'supported_controller_families':['ControlLogix 5580'],'default_time_quantum_us':1000}))\n"
        "  raise SystemExit(0)\n"
        "request_bytes=sys.stdin.buffer.read()\n"
        "request=json.loads(request_bytes)\n"
        "request_sha=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
        "rows=[]\n"
        "for test in request['tests']:\n"
        "  rows.append({'test_id':test['test_id'],'status':'PASS','observed':str(test['assertion']['expected']).lower(),'timestamp':'2026-08-26T13:00:01Z','evidence':['echo://trace/'+test['test_id']]})\n"
        "payload={'schema':'devagent-plc-execution-results-v1','project_sha256':request['analysis_project_sha256'],'test_plan_sha256':request['test_plan_sha256'],'backend_registry_sha256':request['backend_registry_sha256'],'backend':request['adapter']['id'],'run_id':'ECHO-RUN-1','runtime_project_sha256':request['runtime_project_sha256'],'runtime_binding_sha256':request['runtime_binding_sha256'],'runner_sha256':request['adapter']['runner_sha256'],'execution_request_sha256':request_sha,'results':rows}\n"
        "canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()\n"
        "payload['signature']={'algorithm':'ED25519','key_id':'root','value_base64':base64.b64encode(KEY.sign(canonical)).decode()}\n"
        "print(json.dumps(payload,sort_keys=True))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _project_and_runtime(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Machine.L5X"
    project.write_text(PROJECT, encoding="utf-8")
    runtime = tmp_path / "Machine.ACD"
    runtime.write_bytes(b"fake-runtime-project-for-protocol-test")
    return project, runtime


def _binding(
    tmp_path: Path,
    private: Ed25519PrivateKey,
    *,
    project_sha: str,
    runtime_sha: str,
    runner_sha: str,
) -> Path:
    path = tmp_path / "runtime-binding.json"
    payload = {
        "schema": RUNTIME_BINDING_SCHEMA,
        "analysis_project_sha256": project_sha,
        "runtime_project_sha256": runtime_sha,
        "controller_name": "EchoTest",
        "adapter_id": "echo-qualified",
        "runner_sha256": runner_sha,
        "approved_by": "Controls Engineer",
        "approved_at": "2026-08-26T13:00:00Z",
    }
    path.write_text(json.dumps(_sign(private, payload), sort_keys=True), encoding="utf-8")
    return path


def _registry(tmp_path: Path, project_sha: str, *, status: str = "QUALIFIED"):
    path = tmp_path / f"registry-{status.lower()}.json"
    path.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-execution-backend-registry-v1",
                "approved_by": "Controls Owner",
                "approved_at": "2026-08-26T13:00:00Z",
                "backends": [
                    {
                        "id": "echo-qualified",
                        "kind": "SIMULATOR",
                        "status": status,
                        "project_sha256": [project_sha],
                        "qualification_evidence": ["ECHO-QUAL-1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = load_execution_backend_registry(path)
    assert registry is not None
    return registry


def test_echo_runner_descriptor_requires_deterministic_capabilities(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    runner = _runner(tmp_path, private)
    descriptor = describe_echo_runner(runner)
    assert descriptor.adapter_id == "echo-qualified"
    assert descriptor.backend_kind == "SIMULATOR"
    assert {"DOWNLOAD", "SNAPSHOT", "DATA_EXCHANGE", "COSIMULATION"} <= set(descriptor.capabilities)
    assert descriptor.runner_sha256 == hashlib.sha256(runner.read_bytes()).hexdigest()


def test_runtime_binding_binds_analysis_runtime_and_exact_runner(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    trust_path = _trust_store(tmp_path, private)
    trust = load_trusted_signer_store(trust_path)
    assert trust is not None
    project, runtime = _project_and_runtime(tmp_path)
    runner = _runner(tmp_path, private)
    descriptor = describe_echo_runner(runner)
    project_sha = hashlib.sha256(project.read_bytes()).hexdigest()
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    binding_path = _binding(
        tmp_path,
        private,
        project_sha=project_sha,
        runtime_sha=runtime_sha,
        runner_sha=descriptor.runner_sha256,
    )
    binding, signature, signed_bytes = load_runtime_binding(
        binding_path,
        trust_store=trust,
        analysis_project_sha256=project_sha,
        runtime_project_path=runtime,
        controller_name="EchoTest",
        descriptor=descriptor,
    )
    assert binding.runtime_project_sha256 == runtime_sha
    assert signature["purpose"] == "RUNTIME_PROJECT_BINDING"
    assert signed_bytes == binding_path.read_bytes()

    runtime.write_bytes(b"changed")
    with pytest.raises(ValueError, match="runtime_project_sha256"):
        load_runtime_binding(
            binding_path,
            trust_store=trust,
            analysis_project_sha256=project_sha,
            runtime_project_path=runtime,
            controller_name="EchoTest",
            descriptor=descriptor,
        )


def test_echo_request_has_typed_assertions_and_no_physical_writes(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    trust = load_trusted_signer_store(_trust_store(tmp_path, private))
    assert trust is not None
    project, runtime = _project_and_runtime(tmp_path)
    runner = _runner(tmp_path, private)
    descriptor = describe_echo_runner(runner)
    preliminary = run_production_verification_v5(project)
    binding_path = _binding(
        tmp_path,
        private,
        project_sha=preliminary.engineering.project.metadata.source_sha256,
        runtime_sha=hashlib.sha256(runtime.read_bytes()).hexdigest(),
        runner_sha=descriptor.runner_sha256,
    )
    binding, _, _ = load_runtime_binding(
        binding_path,
        trust_store=trust,
        analysis_project_sha256=preliminary.engineering.project.metadata.source_sha256,
        runtime_project_path=runtime,
        controller_name="EchoTest",
        descriptor=descriptor,
    )
    request, request_sha = build_echo_execution_request(
        preliminary,
        runtime_project_path=runtime,
        descriptor=descriptor,
        binding=binding,
        backend_registry_sha256="a" * 64,
    )
    assert request["schema"] == EXECUTION_REQUEST_SCHEMA
    assert request["execution_policy"]["physical_controller_writes_allowed"] is False
    assert request["execution_policy"]["restore_snapshot_before_each_test"] is True
    assert request["execution_policy"]["runtime_project_hash_must_be_verified_before_download"] is True
    assert all(item["assertion"]["type"] == "BOOL" for item in request["tests"])
    assert len(request_sha) == 64


def test_echo_execution_response_is_bound_to_request_runtime_and_runner(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    trust = load_trusted_signer_store(_trust_store(tmp_path, private))
    assert trust is not None
    project, runtime = _project_and_runtime(tmp_path)
    runner = _runner(tmp_path, private)
    descriptor = describe_echo_runner(runner)
    preliminary = run_production_verification_v5(project)
    project_sha = preliminary.engineering.project.metadata.source_sha256
    binding_path = _binding(
        tmp_path,
        private,
        project_sha=project_sha,
        runtime_sha=hashlib.sha256(runtime.read_bytes()).hexdigest(),
        runner_sha=descriptor.runner_sha256,
    )
    registry = _registry(tmp_path, project_sha)
    package = run_echo_execution(
        preliminary,
        runner_path=runner,
        runtime_project_path=runtime,
        runtime_binding_path=binding_path,
        backend_registry=registry,
        trust_store=trust,
    )
    assert package.execution_results["schema"] == "devagent-plc-execution-results-v1"
    assert package.execution_results["runtime_project_sha256"] == package.binding.runtime_project_sha256
    assert package.execution_results["runner_sha256"] == package.descriptor.runner_sha256
    assert package.execution_results["execution_request_sha256"] == package.request_sha256
    assert package.binding_signature["purpose"] == "RUNTIME_PROJECT_BINDING"
    assert package.runtime_binding_bytes == binding_path.read_bytes()
    assert all(row["status"] == "PASS" for row in package.execution_results["results"])


def test_echo_execution_refuses_unqualified_backend_before_binding_or_execute(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    trust = load_trusted_signer_store(_trust_store(tmp_path, private))
    assert trust is not None
    project, runtime = _project_and_runtime(tmp_path)
    runner = _runner(tmp_path, private)
    descriptor = describe_echo_runner(runner)
    preliminary = run_production_verification_v5(project)
    project_sha = preliminary.engineering.project.metadata.source_sha256
    binding_path = _binding(
        tmp_path,
        private,
        project_sha=project_sha,
        runtime_sha=hashlib.sha256(runtime.read_bytes()).hexdigest(),
        runner_sha=descriptor.runner_sha256,
    )
    registry = _registry(tmp_path, project_sha, status="EXPERIMENTAL")
    with pytest.raises(ValueError, match="not QUALIFIED"):
        run_echo_execution(
            preliminary,
            runner_path=runner,
            runtime_project_path=runtime,
            runtime_binding_path=binding_path,
            backend_registry=registry,
            trust_store=trust,
        )


def test_echo_runner_rejects_missing_cosimulation_capability(tmp_path: Path) -> None:
    path = tmp_path / "runner.py"
    path.write_text(
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'schema':'%s','adapter_id':'echo','adapter_version':'1','vendor':'Rockwell Automation','product':'FactoryTalk Logix Echo','backend_kind':'SIMULATOR','capabilities':['DOWNLOAD','SNAPSHOT','DATA_EXCHANGE']}))\n" % RUNNER_SCHEMA,
        encoding="utf-8",
    )
    path.chmod(0o755)
    with pytest.raises(ValueError, match="COSIMULATION"):
        describe_echo_runner(path)
