from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping


class LiveProductionControlStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAIL = "FAIL"


class LiveProductionReadinessRating(str, Enum):
    PRODUCTION_QUALIFIED = "PRODUCTION_QUALIFIED"
    PRODUCTION_CANDIDATE = "PRODUCTION_CANDIDATE"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class LiveProductionControlResult:
    control_id: str
    title: str
    status: LiveProductionControlStatus
    detail: str


@dataclass(frozen=True)
class LiveProductionReadinessReport:
    generated_at: datetime
    controls: tuple[LiveProductionControlResult, ...]

    @property
    def score(self) -> int:
        return sum(item.status is LiveProductionControlStatus.PASS for item in self.controls)

    @property
    def max_score(self) -> int:
        return len(self.controls)

    @property
    def has_failures(self) -> bool:
        return any(item.status is LiveProductionControlStatus.FAIL for item in self.controls)

    @property
    def meets_nine_of_ten(self) -> bool:
        return self.max_score == 10 and self.score >= 9 and not self.has_failures

    @property
    def production_qualified(self) -> bool:
        return (
            self.max_score == 10
            and self.score == 10
            and all(item.status is LiveProductionControlStatus.PASS for item in self.controls)
        )

    @property
    def rating(self) -> LiveProductionReadinessRating:
        if self.production_qualified:
            return LiveProductionReadinessRating.PRODUCTION_QUALIFIED
        if self.meets_nine_of_ten:
            return LiveProductionReadinessRating.PRODUCTION_CANDIDATE
        return LiveProductionReadinessRating.NOT_READY

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(item.status is status for item in self.controls)
            for status in LiveProductionControlStatus
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "devagent-live-production-readiness-v1",
            "mode": "READ_ONLY",
            "generated_at": self.generated_at.isoformat(),
            "score": self.score,
            "max_score": self.max_score,
            "rating": self.rating.value,
            "meets_9_of_10_threshold": self.meets_nine_of_ten,
            "production_qualified": self.production_qualified,
            "counts": self.counts(),
            "controls": [
                {
                    "control_id": item.control_id,
                    "title": item.title,
                    "status": item.status.value,
                    "detail": item.detail,
                }
                for item in self.controls
            ],
        }


ControlCheck = Callable[[], str]
_TEST_SECRET = "devagent-readiness-secret"
_PROHIBITED_METHODS = (
    "write",
    "write_value",
    "set_value",
    "call_method",
    "force",
    "reset",
    "download",
    "change_mode",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_detail(value: Any) -> str:
    return str(value).replace(_TEST_SECRET, "<redacted>")


def _control_read_only() -> str:
    from .manager import MultiPlcConnectionManager
    from .opcua_client import ReadOnlyOpcUaClient
    from .workflow import LiveCommissioningWorkflow

    exposed = [
        f"{target.__name__}.{name}"
        for target in (ReadOnlyOpcUaClient, MultiPlcConnectionManager, LiveCommissioningWorkflow)
        for name in _PROHIBITED_METHODS
        if hasattr(target, name)
    ]
    if exposed:
        raise AssertionError("PLC control surface exposed: " + ", ".join(exposed))
    return "Client, multi-PLC manager, and commissioning workflow expose no PLC control methods."


def _control_security() -> str:
    from .errors import LiveConfigurationError
    from .security import LiveSecurityConfig, SUPPORTED_SECURITY_POLICIES, validate_opcua_endpoint

    try:
        LiveSecurityConfig(username="operator", password=_TEST_SECRET)
    except LiveConfigurationError:
        pass
    else:
        raise AssertionError("username/password was accepted without SignAndEncrypt")

    try:
        validate_opcua_endpoint(f"opc.tcp://operator:{_TEST_SECRET}@127.0.0.1:4840/")
    except LiveConfigurationError:
        pass
    else:
        raise AssertionError("credentials embedded in endpoint were accepted")

    secure = LiveSecurityConfig(
        username="operator",
        password=_TEST_SECRET,
        security_policy="Basic256Sha256",
        security_mode="SignAndEncrypt",
        client_certificate="client.der",
        client_private_key="client-key.pem",
        server_certificate="server.der",
    )
    if _TEST_SECRET in repr(secure):
        raise AssertionError("password leaked through security configuration repr")
    if _TEST_SECRET in secure.redact(f"authentication failed: {_TEST_SECRET}"):
        raise AssertionError("password redaction failed")
    if "Basic256Sha256" not in SUPPORTED_SECURITY_POLICIES:
        raise AssertionError("required production security policy is unavailable")
    return "Credentials require SignAndEncrypt, endpoint credentials are rejected, certificates are pinned, and secret repr/redaction checks pass."


def _runtime_value(*, quality: Any, stale: bool = False, replayed: bool = False) -> Any:
    from .models import RuntimeValue

    now = _now()
    return RuntimeValue(
        node_id="ns=2;s=Readiness.Value",
        value=True,
        variant_type="Boolean",
        status_code=getattr(quality, "value", str(quality)),
        quality=quality,
        source_timestamp=now,
        server_timestamp=now,
        received_at=now,
        age_seconds=10.0 if stale else 0.0,
        stale=stale,
        replayed=replayed,
    )


def _control_trust() -> str:
    from .agent_integration import LiveDataTrustLayer, LiveEvidenceDisposition
    from .models import Quality

    layer = LiveDataTrustLayer()
    samples = (
        (_runtime_value(quality=Quality.GOOD), LiveEvidenceDisposition.CURRENT),
        (_runtime_value(quality=Quality.GOOD, stale=True), LiveEvidenceDisposition.STALE),
        (_runtime_value(quality=Quality.UNCERTAIN), LiveEvidenceDisposition.UNCERTAIN),
        (_runtime_value(quality=Quality.BAD), LiveEvidenceDisposition.UNTRUSTED),
        (_runtime_value(quality=Quality.GOOD, replayed=True), LiveEvidenceDisposition.REPLAYED),
    )
    for value, expected in samples:
        actual = layer.classify(value)
        if actual is not expected:
            raise AssertionError(f"trust mismatch: expected {expected.value}, got {actual.value}")
        record = layer.record(plc_id="readiness", plc_name="Readiness", value=value)
        should_be_current = expected is LiveEvidenceDisposition.CURRENT
        if record.agent_eligible is not should_be_current:
            raise AssertionError(f"agent eligibility mismatch for {expected.value}")
    return "Only GOOD/CURRENT/non-stale/non-replayed observations are agent-eligible."


def _control_reconnect() -> str:
    from .opcua_client import ReadOnlyOpcUaClient

    init_params = inspect.signature(ReadOnlyOpcUaClient.__init__).parameters
    required_params = {
        "auto_reconnect",
        "reconnect_max_delay_seconds",
        "reconnect_request_timeout_seconds",
    }
    missing_params = sorted(required_params.difference(init_params))
    required_methods = {"wait_until_connected", "collect_changes", "connect", "disconnect"}
    missing_methods = sorted(name for name in required_methods if not hasattr(ReadOnlyOpcUaClient, name))
    if missing_params or missing_methods:
        raise AssertionError(
            f"reconnect/subscription surface incomplete; params={missing_params}, methods={missing_methods}"
        )
    return "Auto-reconnect configuration, connection-state wait, and subscription collection/recovery surfaces are present."


def _control_multi_plc() -> str:
    from .manager import MultiPlcConnectionManager, PlcSessionState

    required = {"connect_all", "read_many", "browse", "disconnect_all", "statuses", "status"}
    missing = sorted(name for name in required if not hasattr(MultiPlcConnectionManager, name))
    if missing:
        raise AssertionError("multi-PLC manager surface incomplete: " + ", ".join(missing))
    if any(hasattr(MultiPlcConnectionManager, name) for name in _PROHIBITED_METHODS):
        raise AssertionError("multi-PLC manager exposes a prohibited control method")
    expected_states = {"CONNECTED", "RECONNECTING", "DEGRADED", "FAILED", "DISCONNECTED"}
    if not expected_states.issubset({item.value for item in PlcSessionState}):
        raise AssertionError("multi-PLC failure-isolation states are incomplete")
    return "Concurrent connect/read/browse/disconnect and per-PLC degraded/failed/reconnecting states are present with no write surface."


def _control_reconciliation() -> str:
    from .models import BrowseNode
    from .tag_reconciliation import LiveTagMappingStatus, reconcile_engineering_tags

    tag = SimpleNamespace(
        id="controller:RunCmd",
        name="RunCmd",
        scope="controller",
        data_type="BOOL",
        alias_for=None,
        external_access="Read/Write",
    )
    exact = BrowseNode(
        path="Objects/Controller/RunCmd",
        node_id="ns=2;s=RunCmd",
        browse_name="RunCmd",
        display_name="RunCmd",
        node_class="Variable",
        data_type="Boolean",
        user_access=("CurrentRead",),
        readable=True,
        writable=False,
    )
    result = reconcile_engineering_tags("plc-a", (tag,), (exact,))
    mapping = result.mappings[0]
    if mapping.status is not LiveTagMappingStatus.AUTO_BOUND or mapping.selected_node_id != exact.node_id:
        raise AssertionError("unique exact compatible tag did not auto-bind")

    fuzzy = BrowseNode(
        path="Objects/Controller/RunCommand",
        node_id="ns=2;s=RunCommand",
        browse_name="RunCommand",
        display_name="RunCommand",
        node_class="Variable",
        data_type="Boolean",
        user_access=("CurrentRead",),
        readable=True,
        writable=False,
    )
    fuzzy_result = reconcile_engineering_tags("plc-a", (tag,), (fuzzy,))
    if fuzzy_result.mappings[0].status is not LiveTagMappingStatus.UNMATCHED:
        raise AssertionError("fuzzy/non-exact tag name was auto-accepted")
    return "Exact compatible readable tags bind deterministically; fuzzy names remain unresolved."


def _control_workflow() -> str:
    from .workflow import LiveCommissioningState, LiveCommissioningWorkflow

    if not hasattr(LiveCommissioningWorkflow, "run"):
        raise AssertionError("commissioning workflow has no run surface")
    required_states = {
        "COMPLETE",
        "LIMITED",
        "CONNECT_FAILED",
        "MAPPING_BLOCKED",
        "CAPTURE_FAILED",
    }
    if not required_states.issubset({item.value for item in LiveCommissioningState}):
        raise AssertionError("commissioning fail-closed states are incomplete")
    if any(hasattr(LiveCommissioningWorkflow, name) for name in _PROHIBITED_METHODS):
        raise AssertionError("commissioning workflow exposes a prohibited control method")
    return "Unified commissioning workflow distinguishes complete, limited, connection, mapping, and capture failures while remaining read-only."


def _control_artifacts() -> str:
    from .commission import write_commissioning_artifacts
    from .production_integration import write_live_production_artifacts
    from .qualification import write_live_release_qualification_artifacts

    writers = (
        write_commissioning_artifacts,
        write_live_production_artifacts,
        write_live_release_qualification_artifacts,
    )
    if not all(callable(writer) for writer in writers):
        raise AssertionError("required evidence/manifest artifact writer is unavailable")
    return "Commissioning, customer production sidecar, and release qualification artifact writers are available with SHA-bound manifests."


def _control_cli() -> str:
    from .cli import _build_parser

    parser = _build_parser()
    subparser_action = next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )
    if subparser_action is None:
        raise AssertionError("Live CLI has no subcommands")
    required_commands = {"plan", "commission", "qualify"}
    missing = sorted(required_commands.difference(subparser_action.choices))
    if missing:
        raise AssertionError("production CLI commands missing: " + ", ".join(missing))

    option_strings: set[str] = set()
    for candidate in (parser, *subparser_action.choices.values()):
        for action in candidate._actions:
            option_strings.update(action.option_strings)
    if "--password" in option_strings:
        raise AssertionError("literal --password CLI option is exposed")
    if "--password-env" not in option_strings:
        raise AssertionError("environment-only password option is missing")
    return "Offline plan, validate/commission, and qualification commands are present; literal password argv input remains unavailable."


_CONTROL_SPECS: tuple[tuple[str, str, ControlCheck], ...] = (
    ("PR-001", "Read-only PLC safety boundary", _control_read_only),
    ("PR-002", "OPC UA security and secret hygiene", _control_security),
    ("PR-003", "Live data trust and freshness gate", _control_trust),
    ("PR-004", "Reconnect and subscription recovery capability", _control_reconnect),
    ("PR-005", "Multi-PLC connection and failure isolation", _control_multi_plc),
    ("PR-006", "Engineering tag to OPC UA reconciliation", _control_reconciliation),
    ("PR-007", "Unified commissioning workflow", _control_workflow),
    ("PR-008", "Evidence, report, and manifest integrity", _control_artifacts),
    ("PR-009", "Production user CLI and secret-safe configuration", _control_cli),
)


def _run_control(control_id: str, title: str, checker: ControlCheck) -> LiveProductionControlResult:
    try:
        detail = checker()
    except Exception as exc:
        return LiveProductionControlResult(
            control_id=control_id,
            title=title,
            status=LiveProductionControlStatus.FAIL,
            detail=_safe_detail(f"{type(exc).__name__}: {exc}"),
        )
    return LiveProductionControlResult(
        control_id=control_id,
        title=title,
        status=LiveProductionControlStatus.PASS,
        detail=_safe_detail(detail),
    )


def _qualification_mapping(source: Any) -> Mapping[str, Any] | None:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source
    if isinstance(source, (str, Path)):
        path = Path(source)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("qualification artifact must contain a JSON object")
        return payload
    if hasattr(source, "as_dict") and callable(source.as_dict):
        payload = source.as_dict()
        if not isinstance(payload, Mapping):
            raise ValueError("qualification report as_dict() did not return an object")
        return payload
    raise TypeError("qualification must be a report object, mapping, JSON path, or None")


def _runtime_control(qualification: Any) -> LiveProductionControlResult:
    title = "Real OPC UA runtime release qualification"
    if qualification is None:
        return LiveProductionControlResult(
            control_id="PR-010",
            title=title,
            status=LiveProductionControlStatus.BLOCKED,
            detail="No real-runtime qualification artifact supplied; 14/14 socket qualification remains required for 10/10.",
        )
    try:
        payload = _qualification_mapping(qualification)
        assert payload is not None
        if payload.get("schema") != "devagent-live-release-qualification-v1":
            raise ValueError("unexpected qualification artifact schema")
        if payload.get("mode") != "READ_ONLY":
            raise ValueError("qualification artifact is not READ_ONLY")
        status = str(payload.get("status", "")).strip().upper()
        counts = payload.get("counts")
        if not isinstance(counts, Mapping):
            raise ValueError("qualification artifact counts are missing")
        passed = int(counts.get("PASS", -1))
        failed = int(counts.get("FAIL", -1))
        blocked = int(counts.get("BLOCKED", -1))
        total = passed + failed + blocked
        if total != 14:
            raise ValueError(f"qualification artifact must account for exactly 14 cases, got {total}")
        if status == "PASS":
            if (passed, failed, blocked) != (14, 0, 0):
                raise ValueError("PASS qualification does not contain PASS=14/FAIL=0/BLOCKED=0")
            return LiveProductionControlResult(
                control_id="PR-010",
                title=title,
                status=LiveProductionControlStatus.PASS,
                detail="Real qualification artifact proves PASS=14, FAIL=0, BLOCKED=0.",
            )
        if status == "BLOCKED":
            if failed != 0:
                raise ValueError("BLOCKED qualification contains failed cases")
            return LiveProductionControlResult(
                control_id="PR-010",
                title=title,
                status=LiveProductionControlStatus.BLOCKED,
                detail=f"Runtime qualification is BLOCKED (PASS={passed}, FAIL={failed}, BLOCKED={blocked}); no runtime PASS is claimed.",
            )
        if status == "FAIL":
            return LiveProductionControlResult(
                control_id="PR-010",
                title=title,
                status=LiveProductionControlStatus.FAIL,
                detail=f"Runtime qualification failed (PASS={passed}, FAIL={failed}, BLOCKED={blocked}).",
            )
        raise ValueError(f"unknown qualification status: {status or '<blank>'}")
    except Exception as exc:
        return LiveProductionControlResult(
            control_id="PR-010",
            title=title,
            status=LiveProductionControlStatus.FAIL,
            detail=_safe_detail(f"Invalid qualification evidence: {type(exc).__name__}: {exc}"),
        )


def evaluate_live_production_readiness(
    qualification: Any = None,
    *,
    _control_checks: Mapping[str, ControlCheck] | None = None,
) -> LiveProductionReadinessReport:
    overrides = dict(_control_checks or {})
    controls = tuple(
        _run_control(control_id, title, overrides.get(control_id, checker))
        for control_id, title, checker in _CONTROL_SPECS
    ) + (_runtime_control(qualification),)
    return LiveProductionReadinessReport(generated_at=_now(), controls=controls)


def write_live_production_readiness_artifacts(
    output_dir: str | Path,
    report: LiveProductionReadinessReport,
) -> Path:
    destination = Path(output_dir)
    if destination.exists():
        raise FileExistsError(f"Production-readiness output already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        report_path = destination / "live_production_readiness.json"
        payload = json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n"
        report_path.write_text(payload, encoding="utf-8")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        manifest = {
            "schema": "devagent-live-production-readiness-manifest-v1",
            "mode": "READ_ONLY",
            "score": report.score,
            "max_score": report.max_score,
            "rating": report.rating.value,
            "production_qualified": report.production_qualified,
            "artifacts": {
                report_path.name: {
                    "sha256": digest,
                    "bytes": len(payload.encode("utf-8")),
                }
            },
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination
