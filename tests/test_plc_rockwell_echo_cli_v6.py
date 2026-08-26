from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from devagent.plc.cli import main as plc_main


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="EchoCLI" TargetType="Controller">
  <Controller Use="Target" Name="EchoCLI" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
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


def _signed(private: Ed25519PrivateKey, payload: dict) -> dict:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    result = dict(payload)
    result["signature"] = {
        "algorithm": "ED25519",
        "key_id": "root",
        "value_base64": base64.b64encode(private.sign(canonical)).decode(),
    }
    return result


def _runner(tmp_path: Path, private: Ed25519PrivateKey) -> Path:
    private_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    secret = base64.b64encode(private_bytes).decode()
    runner = tmp_path / "echo_runner.py"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, hashlib, json, sys\n"
        "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey\n"
        f"KEY=Ed25519PrivateKey.from_private_bytes(base64.b64decode({secret!r}))\n"
        "if '--describe' in sys.argv:\n"
        "  print(json.dumps({'schema':'devagent-rockwell-echo-runner-v1','adapter_id':'echo-qualified','adapter_version':'1.0','vendor':'Rockwell Automation','product':'FactoryTalk Logix Echo','backend_kind':'SIMULATOR','capabilities':['DOWNLOAD','SNAPSHOT','DATA_EXCHANGE','COSIMULATION'],'supported_controller_families':['ControlLogix 5580'],'default_time_quantum_us':1000}))\n"
        "  raise SystemExit(0)\n"
        "req=json.loads(sys.stdin.buffer.read())\n"
        "req_sha=hashlib.sha256(json.dumps(req,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
        "results=[{'test_id':t['test_id'],'status':'PASS','observed':str(t['assertion']['expected']).lower(),'timestamp':'2026-08-26T14:00:00Z','evidence':['echo://'+t['test_id']]} for t in req['tests']]\n"
        "payload={'schema':'devagent-plc-execution-results-v1','project_sha256':req['analysis_project_sha256'],'test_plan_sha256':req['test_plan_sha256'],'backend_registry_sha256':req['backend_registry_sha256'],'backend':req['adapter']['id'],'run_id':'ECHO-CLI-1','runtime_project_sha256':req['runtime_project_sha256'],'runtime_binding_sha256':req['runtime_binding_sha256'],'runner_sha256':req['adapter']['runner_sha256'],'execution_request_sha256':req_sha,'results':results}\n"
        "canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()\n"
        "payload['signature']={'algorithm':'ED25519','key_id':'root','value_base64':base64.b64encode(KEY.sign(canonical)).decode()}\n"
        "print(json.dumps(payload,sort_keys=True))\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    return runner


def test_cli_executes_echo_and_persists_signed_runtime_evidence(tmp_path: Path, capsys) -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    project = tmp_path / "Machine.L5X"
    project.write_text(PROJECT, encoding="utf-8")
    project_sha = hashlib.sha256(project.read_bytes()).hexdigest()
    runtime = tmp_path / "Machine.ACD"
    runtime.write_bytes(b"runtime-project")
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    runner = _runner(tmp_path, private)
    runner_sha = hashlib.sha256(runner.read_bytes()).hexdigest()

    trust = tmp_path / "trust.json"
    trust.write_text(
        json.dumps(
            {
                "schema": "devagent-plc-trusted-signers-v1",
                "approved_by": "Security",
                "approved_at": "2026-08-26T13:00:00Z",
                "signers": [
                    {
                        "id": "root",
                        "algorithm": "ED25519",
                        "public_key_base64": base64.b64encode(public).decode(),
                        "purposes": [
                            "EXECUTION_BACKEND_REGISTRY",
                            "EXECUTION_RESULTS",
                            "RUNTIME_PROJECT_BINDING",
                        ],
                        "status": "TRUSTED",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            _signed(
                private,
                {
                    "schema": "devagent-plc-execution-backend-registry-v1",
                    "approved_by": "Controls",
                    "approved_at": "2026-08-26T13:00:00Z",
                    "backends": [
                        {
                            "id": "echo-qualified",
                            "kind": "SIMULATOR",
                            "status": "QUALIFIED",
                            "project_sha256": [project_sha],
                            "qualification_evidence": ["Logix Echo adapter qualification test"],
                        }
                    ],
                },
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    binding = tmp_path / "runtime-binding.json"
    binding.write_text(
        json.dumps(
            _signed(
                private,
                {
                    "schema": "devagent-rockwell-runtime-binding-v1",
                    "analysis_project_sha256": project_sha,
                    "runtime_project_sha256": runtime_sha,
                    "controller_name": "EchoCLI",
                    "adapter_id": "echo-qualified",
                    "runner_sha256": runner_sha,
                    "approved_by": "Controls Engineer",
                    "approved_at": "2026-08-26T13:00:00Z",
                },
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements.json"
    requirements.write_text(
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "REQ-RUN",
                        "text": "When Start=TRUE and Guard=TRUE, Run shall be TRUE.",
                        "verification_mode": "STATIC",
                        "criticality": "LOW",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "run"

    code = plc_main(
        [
            str(project),
            "--requirements",
            str(requirements),
            "--execution-backend-registry",
            str(registry),
            "--trust-store",
            str(trust),
            "--rockwell-echo-runner",
            str(runner),
            "--rockwell-runtime-project",
            str(runtime),
            "--rockwell-runtime-binding",
            str(binding),
            "--output-dir",
            str(output),
        ]
    )
    stdout = capsys.readouterr().out
    assert code == 0
    assert "[ 9/15] TEST EXECUTION             PASS" in stdout
    assert "READY_FOR_ENGINEERING_APPROVAL" in stdout
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["production_profile"] == "ROCKWELL_V6_ECHO"
    assert manifest["rockwell_runtime_project_sha256"] == runtime_sha
    assert manifest["rockwell_echo_runner_sha256"] == runner_sha
    assert (output / "rockwell_echo_execution_request.json").is_file()
    signed_results = output / "rockwell_echo_execution_results_signed.json"
    assert signed_results.is_file()
    assert hashlib.sha256(signed_results.read_bytes()).hexdigest() == manifest["execution_results_sha256"]


def test_cli_rejects_approval_during_direct_echo_execution(tmp_path: Path, capsys) -> None:
    project = tmp_path / "Machine.L5X"
    project.write_text(PROJECT, encoding="utf-8")
    fake = tmp_path / "fake"
    code = plc_main(
        [
            str(project),
            "--rockwell-echo-runner",
            str(fake),
            "--rockwell-runtime-project",
            str(fake),
            "--rockwell-runtime-binding",
            str(fake),
            "--execution-backend-registry",
            str(fake),
            "--trust-store",
            str(fake),
            "--approval",
            str(fake),
        ]
    )
    assert code == 1
    assert "sign approval after the exact execution-results hash is generated" in capsys.readouterr().err
