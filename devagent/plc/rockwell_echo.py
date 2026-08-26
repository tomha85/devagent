from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devagent.plc.execution_trust import ExecutionBackendRegistry, require_qualified_backend
from devagent.plc.production_verification import compute_test_plan_sha256
from devagent.plc.trusted_snapshot import read_json_snapshot, verify_snapshot_signature

RUNNER_SCHEMA = "devagent-rockwell-echo-runner-v1"
RUNTIME_BINDING_SCHEMA = "devagent-rockwell-runtime-binding-v1"
EXECUTION_REQUEST_SCHEMA = "devagent-rockwell-echo-execution-request-v1"
_EXECUTION_RESULTS_SCHEMA = "devagent-plc-execution-results-v1"
_REQUIRED_CAPABILITIES = {"DOWNLOAD", "SNAPSHOT", "DATA_EXCHANGE", "COSIMULATION"}
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_RUNNER_BYTES = 64 * 1024 * 1024
_MAX_RUNTIME_PROJECT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_DESCRIPTOR_BYTES = 1024 * 1024
_MAX_EXECUTION_RESPONSE_BYTES = 25 * 1024 * 1024
_MAX_TESTS = 20_000
_MAX_TIMEOUT_SECONDS = 7200


@dataclass(frozen=True)
class EchoRunnerDescriptor:
    adapter_id: str
    adapter_version: str
    vendor: str
    product: str
    backend_kind: str
    capabilities: tuple[str, ...]
    supported_controller_families: tuple[str, ...]
    runner_path: str
    runner_sha256: str
    default_time_quantum_us: int | None = None

    def jsonable(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RockwellRuntimeBinding:
    analysis_project_sha256: str
    runtime_project_sha256: str
    controller_name: str
    adapter_id: str
    runner_sha256: str
    approved_by: str
    approved_at: str
    source_path: str
    source_sha256: str

    def jsonable(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EchoExecutionPackage:
    descriptor: EchoRunnerDescriptor
    binding: RockwellRuntimeBinding
    binding_signature: dict[str, Any]
    runtime_binding_bytes: bytes
    request: dict[str, Any]
    request_sha256: str
    execution_results_bytes: bytes
    execution_results: dict[str, Any]


def _sha256_file(path: Path, *, max_bytes: int, label: str) -> tuple[str, int]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"{label} cannot be a symlink")
    target = expanded.resolve(strict=True)
    if not target.is_file():
        raise ValueError(f"{label} must be a regular file: {target}")
    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} byte production limit")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest(), size


def _runner_path(path: Path) -> tuple[Path, str]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError("Rockwell Echo runner cannot be a symlink")
    target = expanded.resolve(strict=True)
    digest, _ = _sha256_file(target, max_bytes=_MAX_RUNNER_BYTES, label="Rockwell Echo runner")
    if os.name != "nt" and not os.access(target, os.X_OK):
        raise ValueError("Rockwell Echo runner is not executable")
    return target, digest


def _parse_timestamp(value: str, *, label: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _json_stdout(completed: subprocess.CompletedProcess[bytes], *, label: str, max_bytes: int) -> dict[str, Any]:
    stdout = completed.stdout or b""
    if len(stdout) > max_bytes:
        raise ValueError(f"{label} output exceeds {max_bytes} byte production limit")
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace")[:4096]
        raise ValueError(f"{label} failed with exit {completed.returncode}: {stderr}")
    try:
        loaded = json.loads(stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must return exactly one UTF-8 JSON object on stdout") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} must return a JSON object")
    return loaded


def describe_echo_runner(path: Path, *, timeout_seconds: int = 15) -> EchoRunnerDescriptor:
    target, runner_sha = _runner_path(path)
    try:
        completed = subprocess.run(
            [str(target), "--describe"],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(target.parent),
            timeout=max(1, min(int(timeout_seconds), 60)),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Rockwell Echo runner --describe timed out") from exc
    loaded = _json_stdout(completed, label="Rockwell Echo runner --describe", max_bytes=_MAX_DESCRIPTOR_BYTES)
    if loaded.get("schema") != RUNNER_SCHEMA:
        raise ValueError(f"Rockwell Echo runner schema must be {RUNNER_SCHEMA}")
    adapter_id = str(loaded.get("adapter_id") or "").strip()
    adapter_version = str(loaded.get("adapter_version") or "").strip()
    vendor = str(loaded.get("vendor") or "").strip()
    product = str(loaded.get("product") or "").strip()
    backend_kind = str(loaded.get("backend_kind") or "").strip().upper()
    if _ID.fullmatch(adapter_id) is None or not adapter_version:
        raise ValueError("Rockwell Echo runner requires a valid adapter_id and adapter_version")
    if vendor.casefold() != "rockwell automation":
        raise ValueError("Rockwell Echo runner vendor must be Rockwell Automation")
    if "logix echo" not in product.casefold():
        raise ValueError("Rockwell execution runner product must identify FactoryTalk Logix Echo")
    if backend_kind != "SIMULATOR":
        raise ValueError("Rockwell Echo runner backend_kind must be SIMULATOR")
    raw_capabilities = loaded.get("capabilities")
    if not isinstance(raw_capabilities, list):
        raise ValueError("Rockwell Echo runner capabilities must be a list")
    capabilities = tuple(dict.fromkeys(str(item).strip().upper() for item in raw_capabilities if str(item).strip()))
    missing = sorted(_REQUIRED_CAPABILITIES - set(capabilities))
    if missing:
        raise ValueError("Rockwell Echo runner lacks required capabilities: " + ", ".join(missing))
    raw_families = loaded.get("supported_controller_families", [])
    if not isinstance(raw_families, list):
        raise ValueError("Rockwell Echo runner supported_controller_families must be a list")
    families = tuple(str(item).strip() for item in raw_families if str(item).strip())
    quantum = loaded.get("default_time_quantum_us")
    if quantum is not None and (isinstance(quantum, bool) or not isinstance(quantum, int) or quantum <= 0):
        raise ValueError("Rockwell Echo runner default_time_quantum_us must be a positive integer")
    return EchoRunnerDescriptor(
        adapter_id=adapter_id,
        adapter_version=adapter_version[:128],
        vendor=vendor,
        product=product,
        backend_kind=backend_kind,
        capabilities=capabilities,
        supported_controller_families=families,
        runner_path=str(target),
        runner_sha256=runner_sha,
        default_time_quantum_us=quantum,
    )


def load_runtime_binding(
    path: Path,
    *,
    trust_store,
    analysis_project_sha256: str,
    runtime_project_path: Path,
    controller_name: str,
    descriptor: EchoRunnerDescriptor,
) -> tuple[RockwellRuntimeBinding, dict[str, Any], bytes]:
    snapshot = read_json_snapshot(path, max_bytes=1024 * 1024, purpose="RUNTIME_PROJECT_BINDING")
    signature = verify_snapshot_signature(snapshot, purpose="RUNTIME_PROJECT_BINDING", trust_store=trust_store, required=True)
    assert signature is not None
    loaded = snapshot.data
    if loaded.get("schema") != RUNTIME_BINDING_SCHEMA:
        raise ValueError(f"Rockwell runtime binding schema must be {RUNTIME_BINDING_SCHEMA}")
    runtime_sha, _ = _sha256_file(runtime_project_path, max_bytes=_MAX_RUNTIME_PROJECT_BYTES, label="Rockwell runtime project")
    expected = {
        "analysis_project_sha256": analysis_project_sha256,
        "runtime_project_sha256": runtime_sha,
        "controller_name": controller_name,
        "adapter_id": descriptor.adapter_id,
        "runner_sha256": descriptor.runner_sha256,
    }
    for field, value in expected.items():
        if str(loaded.get(field) or "") != value:
            raise ValueError(f"Rockwell runtime binding {field} does not match the current execution context")
    approved_by = str(loaded.get("approved_by") or "").strip()
    approved_at = str(loaded.get("approved_at") or "").strip()
    if not approved_by or not approved_at:
        raise ValueError("Rockwell runtime binding requires approved_by and approved_at")
    _parse_timestamp(approved_at, label="Rockwell runtime binding approved_at")
    binding = RockwellRuntimeBinding(
        analysis_project_sha256=analysis_project_sha256,
        runtime_project_sha256=runtime_sha,
        controller_name=controller_name,
        adapter_id=descriptor.adapter_id,
        runner_sha256=descriptor.runner_sha256,
        approved_by=approved_by,
        approved_at=approved_at,
        source_path=snapshot.source_path,
        source_sha256=snapshot.sha256,
    )
    return binding, signature, snapshot.payload


def _boolean_expectation(test) -> bool | None:
    pattern = re.compile(rf"\b{re.escape(test.output_tag)}\s*=\s*(TRUE|FALSE)\b", re.IGNORECASE)
    match = pattern.search(test.expected)
    return None if match is None else match.group(1).upper() == "TRUE"


def _expected_boolean(test) -> bool:
    expected = _boolean_expectation(test)
    if expected is None:
        raise ValueError(
            f"FAT test {test.id} does not have a typed Boolean expectation and cannot be executed by the V6 Echo adapter"
        )
    return expected


def _compatible_boolean_tests(tests):
    selected: list[tuple[Any, bool]] = []
    excluded: list[dict[str, str]] = []
    for test in tests:
        expected = _boolean_expectation(test)
        if expected is None:
            excluded.append(
                {
                    "test_id": test.id,
                    "scenario": test.scenario,
                    "reason": "ECHO_V6_REQUIRES_TYPED_BOOLEAN_ASSERTION",
                }
            )
        else:
            selected.append((test, expected))
    return selected, excluded


def echo_v6_test_compatibility(tests) -> dict[str, Any]:
    selected, excluded = _compatible_boolean_tests(tests)
    return {
        "schema": "devagent-rockwell-echo-v6-test-compatibility-v1",
        "full_plan_count": len(tests),
        "selected_count": len(selected),
        "excluded_count": len(excluded),
        "selected_test_ids": [test.id for test, _ in selected],
        "excluded": excluded,
    }


def build_echo_execution_request(
    result,
    *,
    runtime_project_path: Path,
    descriptor: EchoRunnerDescriptor,
    binding: RockwellRuntimeBinding,
    backend_registry_sha256: str,
    time_quantum_us: int | None = None,
) -> tuple[dict[str, Any], str]:
    tests = result.engineering.fat_tests
    if not tests:
        raise ValueError("Rockwell Echo execution requires at least one generated FAT test")
    if len(tests) > _MAX_TESTS:
        raise ValueError(f"Rockwell Echo execution plan exceeds {_MAX_TESTS} tests")

    compatible, _excluded = _compatible_boolean_tests(tests)
    if not compatible:
        raise ValueError(
            "Rockwell Echo V6 has no compatible typed-Boolean FAT tests in this plan; non-Boolean/action/stateful tests remain NOT_RUN until a compatible qualified runner is supplied"
        )

    quantum = time_quantum_us if time_quantum_us is not None else descriptor.default_time_quantum_us
    if quantum is None:
        raise ValueError("Rockwell Echo execution requires --rockwell-time-quantum-us or a runner-declared default_time_quantum_us")
    if isinstance(quantum, bool) or not isinstance(quantum, int) or quantum <= 0 or quantum > 60_000_000:
        raise ValueError("Rockwell Echo time quantum must be an integer from 1 to 60000000 microseconds")
    runtime_target = runtime_project_path.expanduser().resolve(strict=True)
    request = {
        "schema": EXECUTION_REQUEST_SCHEMA,
        "analysis_project_sha256": result.engineering.project.metadata.source_sha256,
        "runtime_project_path": str(runtime_target),
        "runtime_project_sha256": binding.runtime_project_sha256,
        "runtime_binding_sha256": binding.source_sha256,
        "controller_name": result.engineering.project.metadata.controller_name,
        # The signed/hash-bound plan remains the complete engineering plan. Echo
        # V6 executes only the typed-Boolean subset it is qualified to assert.
        "test_plan_sha256": compute_test_plan_sha256(tests),
        "backend_registry_sha256": backend_registry_sha256,
        "adapter": {
            "id": descriptor.adapter_id,
            "version": descriptor.adapter_version,
            "runner_sha256": descriptor.runner_sha256,
            "product": descriptor.product,
        },
        "execution_policy": {
            "restore_snapshot_before_each_test": True,
            "advance_mode": "COSIMULATION_TIME_QUANTUM",
            "time_quantum_us": quantum,
            "physical_controller_writes_allowed": False,
            "runtime_project_hash_must_be_verified_before_download": True,
        },
        "tests": [
            {
                "test_id": test.id,
                "scenario": test.scenario,
                "preconditions": dict(sorted(test.preconditions.items())),
                "assertion": {
                    "tag": test.output_tag,
                    "operator": "EQUALS",
                    "type": "BOOL",
                    "expected": expected,
                },
                "source": test.source.locator,
            }
            for test, expected in compatible
        ],
    }
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return request, hashlib.sha256(encoded).hexdigest()


def execute_echo_runner(
    runner_path: Path,
    request: dict[str, Any],
    *,
    timeout_seconds: int = 900,
) -> tuple[bytes, dict[str, Any]]:
    target, current_runner_sha = _runner_path(runner_path)
    expected_runner_sha = str((request.get("adapter") or {}).get("runner_sha256") or "")
    if current_runner_sha != expected_runner_sha:
        raise ValueError("Rockwell Echo runner changed after capability/binding validation; execution is refused")
    timeout = int(timeout_seconds)
    if timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"Rockwell Echo execution timeout must be 1..{_MAX_TIMEOUT_SECONDS} seconds")
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    try:
        completed = subprocess.run(
            [str(target), "--execute"],
            input=encoded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(target.parent),
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Rockwell Echo execution runner timed out") from exc
    stdout = completed.stdout or b""
    loaded = _json_stdout(completed, label="Rockwell Echo execution runner", max_bytes=_MAX_EXECUTION_RESPONSE_BYTES)
    if loaded.get("schema") != _EXECUTION_RESULTS_SCHEMA:
        raise ValueError(f"Rockwell Echo runner execution response schema must be {_EXECUTION_RESULTS_SCHEMA}")
    return stdout, loaded


def validate_echo_execution_response(
    loaded: dict[str, Any],
    *,
    result,
    descriptor: EchoRunnerDescriptor,
    binding: RockwellRuntimeBinding,
    request_sha256: str,
    backend_registry_sha256: str,
) -> None:
    expected = {
        "project_sha256": result.engineering.project.metadata.source_sha256,
        "test_plan_sha256": compute_test_plan_sha256(result.engineering.fat_tests),
        "backend_registry_sha256": backend_registry_sha256,
        "backend": descriptor.adapter_id,
        "runtime_project_sha256": binding.runtime_project_sha256,
        "runtime_binding_sha256": binding.source_sha256,
        "runner_sha256": descriptor.runner_sha256,
        "execution_request_sha256": request_sha256,
    }
    for field, value in expected.items():
        if str(loaded.get(field) or "") != value:
            raise ValueError(f"Rockwell Echo execution response {field} does not match the execution request")
    if not isinstance(loaded.get("signature"), dict):
        raise ValueError("Rockwell Echo execution response must include a trusted Ed25519 signature")


def run_echo_execution(
    preliminary_result,
    *,
    runner_path: Path,
    runtime_project_path: Path,
    runtime_binding_path: Path,
    backend_registry: ExecutionBackendRegistry,
    trust_store,
    time_quantum_us: int | None = None,
    timeout_seconds: int = 900,
) -> EchoExecutionPackage:
    descriptor = describe_echo_runner(runner_path)
    qualification = require_qualified_backend(
        backend_registry,
        descriptor.adapter_id,
        preliminary_result.engineering.project.metadata.source_sha256,
    )
    if qualification.kind != "SIMULATOR":
        raise ValueError(f"Rockwell Echo adapter {descriptor.adapter_id!r} must be qualified as SIMULATOR")
    binding, binding_signature, binding_bytes = load_runtime_binding(
        runtime_binding_path,
        trust_store=trust_store,
        analysis_project_sha256=preliminary_result.engineering.project.metadata.source_sha256,
        runtime_project_path=runtime_project_path,
        controller_name=preliminary_result.engineering.project.metadata.controller_name,
        descriptor=descriptor,
    )
    request, request_sha = build_echo_execution_request(
        preliminary_result,
        runtime_project_path=runtime_project_path,
        descriptor=descriptor,
        binding=binding,
        backend_registry_sha256=backend_registry.source_sha256,
        time_quantum_us=time_quantum_us,
    )
    response_bytes, response = execute_echo_runner(runner_path, request, timeout_seconds=timeout_seconds)
    validate_echo_execution_response(
        response,
        result=preliminary_result,
        descriptor=descriptor,
        binding=binding,
        request_sha256=request_sha,
        backend_registry_sha256=backend_registry.source_sha256,
    )
    return EchoExecutionPackage(
        descriptor=descriptor,
        binding=binding,
        binding_signature=binding_signature,
        runtime_binding_bytes=binding_bytes,
        request=request,
        request_sha256=request_sha,
        execution_results_bytes=response_bytes,
        execution_results=response,
    )


__all__ = [
    "EchoExecutionPackage",
    "EchoRunnerDescriptor",
    "EXECUTION_REQUEST_SCHEMA",
    "RUNTIME_BINDING_SCHEMA",
    "RUNNER_SCHEMA",
    "build_echo_execution_request",
    "describe_echo_runner",
    "echo_v6_test_compatibility",
    "execute_echo_runner",
    "load_runtime_binding",
    "run_echo_execution",
    "validate_echo_execution_response",
]
