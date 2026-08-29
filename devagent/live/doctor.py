from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .engineering_context import load_live_engineering_context
from .manager import MultiPlcConnectionManager
from .opcua_client import ReadOnlyOpcUaClient
from .security import LiveSecurityConfig
from .workflow import LiveCommissioningWorkflow


class LiveDoctorStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class LiveDoctorCheck:
    check_id: str
    title: str
    status: LiveDoctorStatus
    detail: str


@dataclass(frozen=True)
class LiveDoctorReport:
    generated_at: datetime
    checks: tuple[LiveDoctorCheck, ...]

    @property
    def status(self) -> LiveDoctorStatus:
        if any(item.status is LiveDoctorStatus.FAIL for item in self.checks):
            return LiveDoctorStatus.FAIL
        if any(item.status is LiveDoctorStatus.BLOCKED for item in self.checks):
            return LiveDoctorStatus.BLOCKED
        return LiveDoctorStatus.PASS

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(item.status is status for item in self.checks)
            for status in LiveDoctorStatus
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "devagent-live-doctor-v1",
            "mode": "READ_ONLY",
            "generated_at": self.generated_at.isoformat(),
            "status": self.status.value,
            "counts": self.counts(),
            "checks": [
                {
                    "check_id": item.check_id,
                    "title": item.title,
                    "status": item.status.value,
                    "detail": item.detail,
                }
                for item in self.checks
            ],
        }


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _major(version: str | None) -> int | None:
    if not version:
        return None
    try:
        return int(version.split(".", 1)[0])
    except ValueError:
        return None


def _read_only_check() -> LiveDoctorCheck:
    prohibited = (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
        "download",
        "change_mode",
    )
    exposed = [
        f"{target.__name__}.{name}"
        for target in (ReadOnlyOpcUaClient, MultiPlcConnectionManager, LiveCommissioningWorkflow)
        for name in prohibited
        if hasattr(target, name)
    ]
    if exposed:
        return LiveDoctorCheck(
            "DR-005",
            "Read-only PLC boundary",
            LiveDoctorStatus.FAIL,
            "PLC control surface is exposed: " + ", ".join(exposed),
        )
    return LiveDoctorCheck(
        "DR-005",
        "Read-only PLC boundary",
        LiveDoctorStatus.PASS,
        "Client, manager, and commissioning workflow expose no PLC write/control methods.",
    )


def _filesystem_check(output_parent: Path | None) -> LiveDoctorCheck:
    parent = Path(output_parent or Path.cwd()).expanduser().resolve(strict=False)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        created = Path(tempfile.mkdtemp(prefix=".devagent-live-doctor-", dir=parent))
        probe = created / "probe.json"
        probe.write_text('{"mode":"READ_ONLY"}\n', encoding="utf-8")
        probe.read_text(encoding="utf-8")
        shutil.rmtree(created)
        return LiveDoctorCheck(
            "DR-006",
            "Evidence filesystem",
            LiveDoctorStatus.PASS,
            f"Evidence directory is writable/readable: {parent}",
        )
    except Exception as exc:
        return LiveDoctorCheck(
            "DR-006",
            "Evidence filesystem",
            LiveDoctorStatus.FAIL,
            f"Evidence directory check failed: {exc}",
        )


async def run_live_doctor(
    *,
    project_path: Path | None = None,
    endpoint: str | None = None,
    security: LiveSecurityConfig | None = None,
    output_parent: Path | None = None,
) -> LiveDoctorReport:
    checks: list[LiveDoctorCheck] = []

    py_ok = sys.version_info >= (3, 10)
    checks.append(
        LiveDoctorCheck(
            "DR-001",
            "Python runtime",
            LiveDoctorStatus.PASS if py_ok else LiveDoctorStatus.FAIL,
            f"Python {sys.version.split()[0]}; DevAgent requires Python >=3.10.",
        )
    )

    asyncua_version = _version("asyncua") if importlib.util.find_spec("asyncua") is not None else None
    asyncua_ok = _major(asyncua_version) == 2
    checks.append(
        LiveDoctorCheck(
            "DR-002",
            "asyncua production runtime",
            LiveDoctorStatus.PASS if asyncua_ok else LiveDoctorStatus.BLOCKED,
            (
                f"asyncua {asyncua_version} is inside supported >=2,<3 range."
                if asyncua_ok
                else "asyncua 2.x is not installed/qualified; install the DevAgent live extra before real OPC UA use."
            ),
        )
    )

    crypto_version = _version("cryptography")
    crypto_ok = _major(crypto_version) is not None and _major(crypto_version) >= 42
    checks.append(
        LiveDoctorCheck(
            "DR-003",
            "cryptography runtime",
            LiveDoctorStatus.PASS if crypto_ok else LiveDoctorStatus.FAIL,
            (
                f"cryptography {crypto_version} satisfies >=42."
                if crypto_ok
                else f"cryptography {crypto_version or 'missing'} does not satisfy the production dependency."
            ),
        )
    )

    package_version = _version("devagent-ai")
    checks.append(
        LiveDoctorCheck(
            "DR-004",
            "Installed DevAgent package",
            LiveDoctorStatus.PASS if package_version else LiveDoctorStatus.BLOCKED,
            (
                f"Installed devagent-ai package version: {package_version}."
                if package_version
                else "devagent-ai package metadata is unavailable; editable/source execution may be in use."
            ),
        )
    )
    checks.append(_read_only_check())
    checks.append(_filesystem_check(output_parent))

    if project_path is None:
        checks.append(
            LiveDoctorCheck(
                "DR-007",
                "Engineering project parse",
                LiveDoctorStatus.BLOCKED,
                "No onsite engineering project was supplied to doctor.",
            )
        )
    else:
        try:
            loaded = load_live_engineering_context(Path(project_path))
            checks.append(
                LiveDoctorCheck(
                    "DR-007",
                    "Engineering project parse",
                    LiveDoctorStatus.PASS,
                    (
                        f"Parsed vendor={loaded.context.vendor or 'UNKNOWN'} controller={loaded.context.controller_name or 'UNKNOWN'} "
                        f"tags={len(loaded.context.tags)} rules={len(loaded.context.rules)}."
                    ),
                )
            )
        except Exception as exc:
            checks.append(
                LiveDoctorCheck(
                    "DR-007",
                    "Engineering project parse",
                    LiveDoctorStatus.FAIL,
                    f"Engineering project parse failed: {exc}",
                )
            )

    if endpoint is None:
        checks.append(
            LiveDoctorCheck(
                "DR-008",
                "Real OPC UA endpoint",
                LiveDoctorStatus.BLOCKED,
                "No real OPC UA endpoint was supplied to doctor.",
            )
        )
    elif not asyncua_ok:
        checks.append(
            LiveDoctorCheck(
                "DR-008",
                "Real OPC UA endpoint",
                LiveDoctorStatus.BLOCKED,
                "Endpoint check cannot run until supported asyncua 2.x is installed.",
            )
        )
    else:
        client = ReadOnlyOpcUaClient(
            endpoint,
            security=security or LiveSecurityConfig(),
            auto_reconnect=False,
        )
        try:
            endpoints = await client.discover_endpoints()
            await client.connect()
            checks.append(
                LiveDoctorCheck(
                    "DR-008",
                    "Real OPC UA endpoint",
                    LiveDoctorStatus.PASS,
                    (
                        f"Endpoint discovery/connect succeeded; discovered={len(endpoints)} "
                        f"auth={client.authentication_mode} security={client.security_summary}."
                    ),
                )
            )
        except Exception as exc:
            safe = (security or LiveSecurityConfig()).redact(str(exc))
            checks.append(
                LiveDoctorCheck(
                    "DR-008",
                    "Real OPC UA endpoint",
                    LiveDoctorStatus.FAIL,
                    f"Endpoint discovery/connect failed: {safe}",
                )
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    return LiveDoctorReport(
        generated_at=datetime.now(timezone.utc),
        checks=tuple(checks),
    )


def _write_json(path: Path, value: dict[str, Any]) -> tuple[str, int]:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def write_live_doctor_artifacts(output_dir: Path, report: LiveDoctorReport) -> Path:
    target = Path(output_dir).expanduser().resolve(strict=False)
    target.mkdir(parents=True, exist_ok=False)
    try:
        report_path = target / "live_doctor.json"
        sha, size = _write_json(report_path, report.as_dict())
        _write_json(
            target / "manifest.json",
            {
                "schema": "devagent-live-doctor-manifest-v1",
                "mode": "READ_ONLY",
                "status": report.status.value,
                "artifacts": {report_path.name: {"sha256": sha, "bytes": size}},
            },
        )
        return target
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


__all__ = [
    "LiveDoctorStatus",
    "LiveDoctorCheck",
    "LiveDoctorReport",
    "run_live_doctor",
    "write_live_doctor_artifacts",
]
