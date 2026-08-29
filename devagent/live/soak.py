from __future__ import annotations

import asyncio
import hashlib
import json
import resource
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Any

from .agent_integration import LiveDataTrustLayer, LiveEvidenceDisposition
from .commission import LoadedCommissioningConfig
from .manager import MultiPlcConnectionManager, PlcSessionState
from .runtime_environment import detect_live_opcua_runtime
from .tag_reconciliation import reconcile_connected_project_tags


class LiveSoakStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class LiveSoakPlcResult:
    plc_id: str
    plc_name: str
    cycles: int
    total_values: int
    current_values: int
    noncurrent_values: int
    read_error_cycles: int
    max_consecutive_error_cycles: int
    final_state: str
    current_ratio: float
    status: LiveSoakStatus
    detail: str


@dataclass(frozen=True)
class LiveSoakReport:
    started_at: datetime
    finished_at: datetime
    requested_duration_seconds: float
    actual_duration_seconds: float
    interval_seconds: float
    min_current_ratio: float
    max_consecutive_error_cycles_allowed: int
    max_memory_growth_mb_allowed: float
    memory_start_mb: float
    memory_peak_mb: float
    memory_growth_mb: float
    plcs: tuple[LiveSoakPlcResult, ...]
    setup_error: str | None = None
    runtime_version: str | None = None

    @property
    def status(self) -> LiveSoakStatus:
        if self.setup_error:
            return (
                LiveSoakStatus.BLOCKED
                if self.setup_error.startswith("DEPENDENCY:")
                else LiveSoakStatus.FAIL
            )
        if any(item.status is LiveSoakStatus.FAIL for item in self.plcs):
            return LiveSoakStatus.FAIL
        if any(item.status is LiveSoakStatus.BLOCKED for item in self.plcs):
            return LiveSoakStatus.BLOCKED
        if self.memory_growth_mb > self.max_memory_growth_mb_allowed:
            return LiveSoakStatus.FAIL
        return LiveSoakStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "devagent-live-soak-v1",
            "mode": "READ_ONLY",
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "runtime_version": self.runtime_version,
            "requested_duration_seconds": self.requested_duration_seconds,
            "actual_duration_seconds": round(self.actual_duration_seconds, 6),
            "interval_seconds": self.interval_seconds,
            "thresholds": {
                "min_current_ratio": self.min_current_ratio,
                "max_consecutive_error_cycles": self.max_consecutive_error_cycles_allowed,
                "max_memory_growth_mb": self.max_memory_growth_mb_allowed,
            },
            "memory": {
                "start_mb": round(self.memory_start_mb, 3),
                "peak_mb": round(self.memory_peak_mb, 3),
                "growth_mb": round(self.memory_growth_mb, 3),
            },
            "status": self.status.value,
            "setup_error": self.setup_error,
            "plcs": [
                {
                    "plc_id": item.plc_id,
                    "plc_name": item.plc_name,
                    "cycles": item.cycles,
                    "total_values": item.total_values,
                    "current_values": item.current_values,
                    "noncurrent_values": item.noncurrent_values,
                    "read_error_cycles": item.read_error_cycles,
                    "max_consecutive_error_cycles": item.max_consecutive_error_cycles,
                    "final_state": item.final_state,
                    "current_ratio": round(item.current_ratio, 6),
                    "status": item.status.value,
                    "detail": item.detail,
                }
                for item in self.plcs
            ],
        }


@dataclass
class _Stats:
    cycles: int = 0
    total_values: int = 0
    current_values: int = 0
    noncurrent_values: int = 0
    read_error_cycles: int = 0
    consecutive_errors: int = 0
    max_consecutive_errors: int = 0
    setup_error: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rss_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return value / (1024.0 * 1024.0)
    return value / 1024.0


def _safe_error(config: LoadedCommissioningConfig, plc_id: str, exc: BaseException) -> str:
    for spec in config.specs:
        if spec.connection.plc_id == plc_id:
            return spec.connection.security.redact(str(exc))
    return str(exc)


def _blocked_runtime_report(
    config: LoadedCommissioningConfig,
    *,
    started_at: datetime,
    started_clock: float,
    duration_seconds: float,
    interval_seconds: float,
    min_current_ratio: float,
    max_consecutive_error_cycles: int,
    max_memory_growth_mb: float,
    memory_start: float,
    runtime_version: str | None,
    detail: str,
) -> LiveSoakReport:
    memory_peak = _rss_mb()
    return LiveSoakReport(
        started_at=started_at,
        finished_at=_now(),
        requested_duration_seconds=duration_seconds,
        actual_duration_seconds=perf_counter() - started_clock,
        interval_seconds=interval_seconds,
        min_current_ratio=min_current_ratio,
        max_consecutive_error_cycles_allowed=max_consecutive_error_cycles,
        max_memory_growth_mb_allowed=max_memory_growth_mb,
        memory_start_mb=memory_start,
        memory_peak_mb=memory_peak,
        memory_growth_mb=max(0.0, memory_peak - memory_start),
        plcs=tuple(
            LiveSoakPlcResult(
                plc_id=spec.connection.plc_id,
                plc_name=spec.connection.display_name,
                cycles=0,
                total_values=0,
                current_values=0,
                noncurrent_values=0,
                read_error_cycles=0,
                max_consecutive_error_cycles=0,
                final_state="NOT_RUN",
                current_ratio=0.0,
                status=LiveSoakStatus.BLOCKED,
                detail="Soak was not executed because the supported OPC UA runtime prerequisite is unavailable.",
            )
            for spec in config.specs
        ),
        setup_error="DEPENDENCY:" + detail,
        runtime_version=runtime_version,
    )


async def run_live_soak(
    config: LoadedCommissioningConfig,
    *,
    duration_seconds: float = 8 * 3600.0,
    interval_seconds: float = 1.0,
    min_current_ratio: float = 0.95,
    max_consecutive_error_cycles: int = 5,
    max_memory_growth_mb: float = 256.0,
) -> LiveSoakReport:
    if duration_seconds < 0.1:
        raise ValueError("duration_seconds must be >= 0.1")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if not 0.0 < min_current_ratio <= 1.0:
        raise ValueError("min_current_ratio must be in (0, 1]")
    if max_consecutive_error_cycles < 0:
        raise ValueError("max_consecutive_error_cycles must be >= 0")
    if max_memory_growth_mb < 0:
        raise ValueError("max_memory_growth_mb must be >= 0")

    started_at = _now()
    started_clock = perf_counter()
    memory_start = _rss_mb()
    runtime = detect_live_opcua_runtime()
    if not runtime.supported:
        return _blocked_runtime_report(
            config,
            started_at=started_at,
            started_clock=started_clock,
            duration_seconds=duration_seconds,
            interval_seconds=interval_seconds,
            min_current_ratio=min_current_ratio,
            max_consecutive_error_cycles=max_consecutive_error_cycles,
            max_memory_growth_mb=max_memory_growth_mb,
            memory_start=memory_start,
            runtime_version=runtime.version,
            detail=runtime.detail,
        )

    memory_peak = memory_start
    manager = MultiPlcConnectionManager([spec.connection for spec in config.specs])
    stats = {spec.connection.plc_id: _Stats() for spec in config.specs}
    requests: dict[str, tuple[str, ...]] = {}
    setup_error: str | None = None
    trust = LiveDataTrustLayer()

    try:
        connect_statuses = await manager.connect_all()
        for spec in config.specs:
            plc_id = spec.connection.plc_id
            connect = connect_statuses[plc_id]
            if not connect.connected or connect.state is not PlcSessionState.CONNECTED:
                stats[plc_id].setup_error = connect.last_error or "initial connection did not reach CONNECTED"
                continue
            try:
                reconciliation = await reconcile_connected_project_tags(
                    manager,
                    plc_id,
                    spec.engineering_project,
                    explicit_node_map=spec.explicit_node_map,
                    max_depth=spec.browse_max_depth,
                    max_nodes=spec.browse_max_nodes,
                )
                requests.update(
                    reconciliation.node_request_map(
                        required_tag_ids=spec.required_tag_ids,
                        require_all=spec.require_all_mappings,
                    )
                )
            except Exception as exc:
                stats[plc_id].setup_error = _safe_error(config, plc_id, exc)

        runnable = {
            plc_id: nodes
            for plc_id, nodes in requests.items()
            if nodes and stats[plc_id].setup_error is None
        }
        if not runnable:
            setup_error = "No PLC has a safely reconciled read set for the soak run."
        else:
            deadline = started_clock + duration_seconds
            while perf_counter() < deadline:
                cycle_started = perf_counter()
                results = await manager.read_many(runnable)
                for plc_id, result in results.items():
                    item = stats[plc_id]
                    item.cycles += 1
                    if result.error:
                        item.read_error_cycles += 1
                        item.consecutive_errors += 1
                        item.max_consecutive_errors = max(
                            item.max_consecutive_errors,
                            item.consecutive_errors,
                        )
                    else:
                        item.consecutive_errors = 0
                    for value in result.values:
                        item.total_values += 1
                        if trust.classify(value) is LiveEvidenceDisposition.CURRENT:
                            item.current_values += 1
                        else:
                            item.noncurrent_values += 1
                memory_peak = max(memory_peak, _rss_mb())
                remaining = interval_seconds - (perf_counter() - cycle_started)
                if remaining > 0 and perf_counter() < deadline:
                    await asyncio.sleep(min(remaining, max(0.0, deadline - perf_counter())))
    except Exception as exc:
        setup_error = str(exc)
    finally:
        final_statuses = {
            plc_id: manager.status(plc_id)
            for plc_id in manager.plc_ids
        }
        try:
            await manager.disconnect_all()
        except Exception:
            pass

    actual_duration = perf_counter() - started_clock
    memory_peak = max(memory_peak, _rss_mb())
    memory_growth = max(0.0, memory_peak - memory_start)
    plc_results: list[LiveSoakPlcResult] = []

    for spec in config.specs:
        plc_id = spec.connection.plc_id
        item = stats[plc_id]
        final = final_statuses[plc_id]
        ratio = item.current_values / item.total_values if item.total_values else 0.0
        reasons: list[str] = []
        if item.setup_error:
            reasons.append("setup: " + item.setup_error)
        if item.cycles == 0:
            reasons.append("no read cycles completed")
        if ratio < min_current_ratio:
            reasons.append(
                f"CURRENT ratio {ratio:.3f} < required {min_current_ratio:.3f}"
            )
        if item.max_consecutive_errors > max_consecutive_error_cycles:
            reasons.append(
                f"max consecutive error cycles {item.max_consecutive_errors} > allowed {max_consecutive_error_cycles}"
            )
        if not final.connected or final.state is not PlcSessionState.CONNECTED:
            reasons.append(f"final session did not recover to CONNECTED ({final.state.value})")
        status = LiveSoakStatus.FAIL if reasons else LiveSoakStatus.PASS
        plc_results.append(
            LiveSoakPlcResult(
                plc_id=plc_id,
                plc_name=spec.connection.display_name,
                cycles=item.cycles,
                total_values=item.total_values,
                current_values=item.current_values,
                noncurrent_values=item.noncurrent_values,
                read_error_cycles=item.read_error_cycles,
                max_consecutive_error_cycles=item.max_consecutive_errors,
                final_state=final.state.value,
                current_ratio=ratio,
                status=status,
                detail=(
                    "; ".join(reasons)
                    if reasons
                    else "Long-running read-only session stayed within trust, recovery, and quality thresholds."
                ),
            )
        )

    if memory_growth > max_memory_growth_mb:
        suffix = (
            f"RSS high-water growth {memory_growth:.1f} MiB exceeds allowed {max_memory_growth_mb:.1f} MiB."
        )
        setup_error = f"{setup_error}; {suffix}" if setup_error else suffix

    return LiveSoakReport(
        started_at=started_at,
        finished_at=_now(),
        requested_duration_seconds=duration_seconds,
        actual_duration_seconds=actual_duration,
        interval_seconds=interval_seconds,
        min_current_ratio=min_current_ratio,
        max_consecutive_error_cycles_allowed=max_consecutive_error_cycles,
        max_memory_growth_mb_allowed=max_memory_growth_mb,
        memory_start_mb=memory_start,
        memory_peak_mb=memory_peak,
        memory_growth_mb=memory_growth,
        plcs=tuple(plc_results),
        setup_error=setup_error,
        runtime_version=runtime.version,
    )


def _write_json(path: Path, value: dict[str, Any]) -> tuple[str, int]:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def write_live_soak_artifacts(output_dir: Path, report: LiveSoakReport) -> Path:
    target = Path(output_dir).expanduser().resolve(strict=False)
    target.mkdir(parents=True, exist_ok=False)
    try:
        report_path = target / "live_soak_report.json"
        sha, size = _write_json(report_path, report.as_dict())
        _write_json(
            target / "manifest.json",
            {
                "schema": "devagent-live-soak-manifest-v1",
                "mode": "READ_ONLY",
                "status": report.status.value,
                "artifacts": {
                    report_path.name: {"sha256": sha, "bytes": size}
                },
            },
        )
        return target
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


__all__ = [
    "LiveSoakStatus",
    "LiveSoakPlcResult",
    "LiveSoakReport",
    "run_live_soak",
    "write_live_soak_artifacts",
]
