from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import LiveConfigurationError
from .manager import PlcConnectionSpec
from .production_integration import build_live_customer_evidence_artifact
from .security import LiveSecurityConfig
from .workflow import LiveCommissioningPlcSpec, LiveCommissioningWorkflow, LiveCommissioningWorkflowResult

_SCHEMA = "devagent-live-commission-v1"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_PLCS = 32
_MAX_REQUIRED_TAGS = 200
_PLC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_TOP_LEVEL_KEYS = {"schema", "plcs"}
_PLC_KEYS = {
    "plc_id",
    "plc_name",
    "endpoint",
    "engineering_project",
    "required_tag_ids",
    "explicit_node_map",
    "require_all_mappings",
    "browse_max_depth",
    "browse_max_nodes",
    "timeout_seconds",
    "auto_reconnect",
    "reconnect_max_delay_seconds",
    "reconnect_request_timeout_seconds",
    "stale_after_seconds",
    "security",
}
_SECURITY_KEYS = {
    "username",
    "password_env",
    "allow_insecure_username_password",
    "security_policy",
    "security_mode",
    "client_certificate",
    "client_private_key",
    "private_key_password_env",
    "server_certificate",
    "user_certificate",
    "user_private_key",
    "user_private_key_password_env",
    "application_uri",
}
_FORBIDDEN_SECRET_KEYS = {
    "password",
    "private_key_password",
    "user_private_key_password",
}

ProjectLoader = Callable[[Path], Any]
WorkflowFactory = Callable[..., LiveCommissioningWorkflow]


@dataclass(frozen=True)
class LoadedCommissioningConfig:
    source_path: Path
    source_sha256: str
    specs: tuple[LiveCommissioningPlcSpec, ...]


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LiveConfigurationError(f"{label} must be a JSON object")
    return dict(value)


def _unknown_keys(value: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LiveConfigurationError(
            f"{label} contains unsupported field(s): {', '.join(unknown)}"
        )


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiveConfigurationError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label=label)


def _bool(value: Any, *, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise LiveConfigurationError(f"{label} must be true or false")
    return value


def _positive_float(value: Any, *, label: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveConfigurationError(f"{label} must be a number")
    result = float(value)
    if result <= 0:
        raise LiveConfigurationError(f"{label} must be > 0")
    return result


def _bounded_int(
    value: Any,
    *,
    label: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveConfigurationError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise LiveConfigurationError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return value


def _resolve_path(base_dir: Path, value: Any, *, label: str) -> Path:
    text = _required_text(value, label=label)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def _secret_from_env(
    env: Mapping[str, str],
    variable_name: Any,
    *,
    label: str,
) -> str | None:
    if variable_name is None:
        return None
    name = _required_text(variable_name, label=f"{label} environment variable")
    if name not in env:
        raise LiveConfigurationError(
            f"{label} environment variable {name!r} is not set"
        )
    return env[name]


def _security_from_json(
    raw: Any,
    *,
    base_dir: Path,
    env: Mapping[str, str],
    validate_files: bool,
) -> LiveSecurityConfig:
    if raw is None:
        return LiveSecurityConfig()
    data = _require_object(raw, label="PLC security")
    forbidden = sorted(set(data) & _FORBIDDEN_SECRET_KEYS)
    if forbidden:
        raise LiveConfigurationError(
            "PLC security config must not contain secret value field(s): "
            + ", ".join(forbidden)
            + "; use environment-variable references instead"
        )
    _unknown_keys(data, _SECURITY_KEYS, label="PLC security")

    def optional_path(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        return str(_resolve_path(base_dir, value, label=f"security.{key}"))

    security = LiveSecurityConfig(
        username=_optional_text(data.get("username"), label="security.username"),
        password=_secret_from_env(
            env,
            data.get("password_env"),
            label="OPC UA password",
        ),
        allow_insecure_username_password=_bool(
            data.get("allow_insecure_username_password"),
            label="security.allow_insecure_username_password",
            default=False,
        ),
        security_policy=_optional_text(
            data.get("security_policy"), label="security.security_policy"
        ),
        security_mode=_optional_text(
            data.get("security_mode"), label="security.security_mode"
        ),
        client_certificate=optional_path("client_certificate"),
        client_private_key=optional_path("client_private_key"),
        private_key_password=_secret_from_env(
            env,
            data.get("private_key_password_env"),
            label="OPC UA private-key password",
        ),
        server_certificate=optional_path("server_certificate"),
        user_certificate=optional_path("user_certificate"),
        user_private_key=optional_path("user_private_key"),
        user_private_key_password=_secret_from_env(
            env,
            data.get("user_private_key_password_env"),
            label="OPC UA user private-key password",
        ),
        application_uri=_optional_text(
            data.get("application_uri"), label="security.application_uri"
        )
        or "urn:devagent:live:client",
    )
    if validate_files:
        security.validate_files()
    return security


def _required_tag_ids(raw: Any, *, plc_id: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise LiveConfigurationError(
            f"PLC {plc_id} required_tag_ids must be a non-empty JSON array"
        )
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag_id = _required_text(item, label=f"PLC {plc_id} required tag id")
        if tag_id not in seen:
            seen.add(tag_id)
            values.append(tag_id)
    if len(values) > _MAX_REQUIRED_TAGS:
        raise LiveConfigurationError(
            f"PLC {plc_id} supports at most {_MAX_REQUIRED_TAGS} required tags"
        )
    return tuple(values)


def _explicit_node_map(raw: Any, *, plc_id: str) -> dict[str, str]:
    if raw is None:
        return {}
    data = _require_object(raw, label=f"PLC {plc_id} explicit_node_map")
    if len(data) > _MAX_REQUIRED_TAGS:
        raise LiveConfigurationError(
            f"PLC {plc_id} explicit_node_map supports at most {_MAX_REQUIRED_TAGS} entries"
        )
    result: dict[str, str] = {}
    for key, value in data.items():
        clean_key = _required_text(key, label=f"PLC {plc_id} explicit mapping key")
        result[clean_key] = _required_text(
            value, label=f"PLC {plc_id} explicit NodeId"
        )
    return result


def _default_project_loader(path: Path) -> Any:
    # Keep the large vendor PLC production stack lazy. `devagent live` commands
    # unrelated to commissioning should remain lightweight.
    from devagent.plc.plc_dispatch import analyze_plc_project

    return analyze_plc_project(path)


def _canonical_project(loaded: Any, *, plc_id: str) -> Any:
    project = getattr(loaded, "project", loaded)
    tags = getattr(project, "tags", None)
    if tags is None:
        raise LiveConfigurationError(
            f"Engineering analysis for PLC {plc_id} did not produce a canonical tag list"
        )
    return project


def _validate_required_tags(project: Any, required: tuple[str, ...], *, plc_id: str) -> None:
    tag_ids: set[str] = set()
    duplicates: set[str] = set()
    for tag in project.tags:
        tag_id = str(getattr(tag, "id", "")).strip()
        if not tag_id:
            continue
        if tag_id in tag_ids:
            duplicates.add(tag_id)
        tag_ids.add(tag_id)
    if duplicates:
        raise LiveConfigurationError(
            f"PLC {plc_id} engineering project contains duplicate canonical tag id(s): "
            + ", ".join(sorted(duplicates))
        )
    missing = [tag_id for tag_id in required if tag_id not in tag_ids]
    if missing:
        raise LiveConfigurationError(
            f"PLC {plc_id} required engineering tag id(s) were not found: "
            + ", ".join(missing)
        )


def load_commissioning_config(
    path: Path,
    *,
    env: Mapping[str, str] | None = None,
    project_loader: ProjectLoader | None = None,
    validate_security_files: bool = True,
) -> LoadedCommissioningConfig:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise LiveConfigurationError(f"Commissioning config is not a file: {source}")
    payload = source.read_bytes()
    if len(payload) > _MAX_CONFIG_BYTES:
        raise LiveConfigurationError(
            f"Commissioning config exceeds {_MAX_CONFIG_BYTES} bytes"
        )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveConfigurationError(f"Commissioning config is not valid UTF-8 JSON: {exc}") from None

    root = _require_object(decoded, label="Commissioning config")
    _unknown_keys(root, _TOP_LEVEL_KEYS, label="Commissioning config")
    if root.get("schema") != _SCHEMA:
        raise LiveConfigurationError(
            f"Commissioning config schema must be {_SCHEMA!r}"
        )
    raw_plcs = root.get("plcs")
    if not isinstance(raw_plcs, list) or not raw_plcs:
        raise LiveConfigurationError("Commissioning config plcs must be a non-empty JSON array")
    if len(raw_plcs) > _MAX_PLCS:
        raise LiveConfigurationError(
            f"Commissioning config supports at most {_MAX_PLCS} PLCs"
        )

    base_dir = source.parent
    environment = os.environ if env is None else env
    loader = project_loader or _default_project_loader
    specs: list[LiveCommissioningPlcSpec] = []
    seen_plc_ids: set[str] = set()

    for index, raw_plc in enumerate(raw_plcs, start=1):
        data = _require_object(raw_plc, label=f"PLC entry {index}")
        _unknown_keys(data, _PLC_KEYS, label=f"PLC entry {index}")
        plc_id = _required_text(data.get("plc_id"), label=f"PLC entry {index} plc_id")
        if not _PLC_ID.fullmatch(plc_id):
            raise LiveConfigurationError(
                f"PLC id {plc_id!r} must match {_PLC_ID.pattern}"
            )
        if plc_id in seen_plc_ids:
            raise LiveConfigurationError(f"Duplicate PLC id in commissioning config: {plc_id}")
        seen_plc_ids.add(plc_id)

        project_path = _resolve_path(
            base_dir,
            data.get("engineering_project"),
            label=f"PLC {plc_id} engineering_project",
        )
        if not project_path.exists():
            raise LiveConfigurationError(
                f"PLC {plc_id} engineering project does not exist: {project_path}"
            )
        required = _required_tag_ids(data.get("required_tag_ids"), plc_id=plc_id)
        project = _canonical_project(loader(project_path), plc_id=plc_id)
        _validate_required_tags(project, required, plc_id=plc_id)
        security = _security_from_json(
            data.get("security"),
            base_dir=base_dir,
            env=environment,
            validate_files=validate_security_files,
        )

        connection = PlcConnectionSpec(
            plc_id=plc_id,
            plc_name=_optional_text(data.get("plc_name"), label=f"PLC {plc_id} plc_name"),
            endpoint=_required_text(data.get("endpoint"), label=f"PLC {plc_id} endpoint"),
            security=security,
            timeout_seconds=_positive_float(
                data.get("timeout_seconds"), label=f"PLC {plc_id} timeout_seconds", default=4.0
            ),
            auto_reconnect=_bool(
                data.get("auto_reconnect"), label=f"PLC {plc_id} auto_reconnect", default=True
            ),
            reconnect_max_delay_seconds=_positive_float(
                data.get("reconnect_max_delay_seconds"),
                label=f"PLC {plc_id} reconnect_max_delay_seconds",
                default=5.0,
            ),
            reconnect_request_timeout_seconds=_positive_float(
                data.get("reconnect_request_timeout_seconds"),
                label=f"PLC {plc_id} reconnect_request_timeout_seconds",
                default=30.0,
            ),
            stale_after_seconds=_positive_float(
                data.get("stale_after_seconds"),
                label=f"PLC {plc_id} stale_after_seconds",
                default=5.0,
            ),
        )
        specs.append(
            LiveCommissioningPlcSpec(
                connection=connection,
                engineering_project=project,
                required_tag_ids=required,
                explicit_node_map=_explicit_node_map(
                    data.get("explicit_node_map"), plc_id=plc_id
                ),
                require_all_mappings=_bool(
                    data.get("require_all_mappings"),
                    label=f"PLC {plc_id} require_all_mappings",
                    default=True,
                ),
                browse_max_depth=_bounded_int(
                    data.get("browse_max_depth"),
                    label=f"PLC {plc_id} browse_max_depth",
                    default=4,
                    minimum=0,
                    maximum=16,
                ),
                browse_max_nodes=_bounded_int(
                    data.get("browse_max_nodes"),
                    label=f"PLC {plc_id} browse_max_nodes",
                    default=500,
                    minimum=1,
                    maximum=5000,
                ),
            )
        )

    return LoadedCommissioningConfig(
        source_path=source,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        specs=tuple(specs),
    )


def commissioning_summary(
    config: LoadedCommissioningConfig,
    result: LiveCommissioningWorkflowResult,
) -> dict[str, Any]:
    plc_results: list[dict[str, Any]] = []
    for spec in config.specs:
        plc_id = spec.connection.plc_id
        item = result.plc_results[plc_id]
        reconciliation = item.reconciliation
        mappings = tuple(reconciliation.mappings) if reconciliation is not None else ()
        evidence = item.evidence
        pack = evidence.live_pack if evidence is not None else None
        plc_results.append(
            {
                "plc_id": plc_id,
                "plc_name": spec.connection.display_name,
                "state": item.state.value,
                "connection_state": item.connection_status.state.value,
                "connected_during_capture": item.connection_status.connected,
                "authentication_mode": item.connection_status.authentication_mode,
                "security": item.connection_status.security_summary,
                "error": (
                    spec.connection.security.redact(item.error)
                    if item.error
                    else None
                ),
                "mapping": {
                    "total": len(mappings),
                    "accepted": sum(1 for mapping in mappings if mapping.accepted),
                    "unresolved": sum(1 for mapping in mappings if not mapping.accepted),
                },
                "live_pack_id": getattr(pack, "pack_id", None),
                "definitive_current_evidence": len(
                    getattr(pack, "definitive_current_evidence_ids", ())
                ),
                "excluded_raw_evidence": len(
                    getattr(pack, "excluded_raw_evidence_ids", ())
                ),
                "limitations": list(getattr(pack, "limitations", ())),
            }
        )
    return {
        "schema": "devagent-live-commission-run-v1",
        "mode": "READ_ONLY",
        "config_sha256": config.source_sha256,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "all_complete": result.all_complete,
        "plcs": plc_results,
    }


def _mapping_artifact(plc_id: str, reconciliation: Any) -> dict[str, Any]:
    mappings = () if reconciliation is None else reconciliation.mappings
    return {
        "schema": "devagent-live-tag-reconciliation-v1",
        "plc_id": plc_id,
        "mappings": [
            {
                "tag_id": mapping.tag_id,
                "tag_name": mapping.tag_name,
                "tag_scope": mapping.tag_scope,
                "tag_data_type": mapping.tag_data_type,
                "status": mapping.status.value,
                "reason": mapping.reason,
                "accepted": mapping.accepted,
                "selected_node_id": mapping.selected_node_id,
                "selected_path": mapping.selected_path,
                "evidence_id": mapping.evidence_id,
            }
            for mapping in mappings
        ],
    }


def _write_json(path: Path, value: Any) -> str:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def write_commissioning_artifacts(
    output_dir: Path,
    config: LoadedCommissioningConfig,
    result: LiveCommissioningWorkflowResult,
) -> Path:
    target = Path(output_dir).expanduser().resolve(strict=False)
    target.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, str] = {}

    summary_path = target / "live_commissioning_summary.json"
    hashes[summary_path.name] = _write_json(
        summary_path, commissioning_summary(config, result)
    )

    for spec in config.specs:
        plc_id = spec.connection.plc_id
        item = result.plc_results[plc_id]
        mapping_name = f"{plc_id}.tag_reconciliation.json"
        hashes[mapping_name] = _write_json(
            target / mapping_name,
            _mapping_artifact(plc_id, item.reconciliation),
        )
        if item.evidence is not None:
            evidence_name = f"{plc_id}.live_evidence.json"
            hashes[evidence_name] = _write_json(
                target / evidence_name,
                build_live_customer_evidence_artifact(item.evidence.live_pack),
            )

    manifest = {
        "schema": "devagent-live-commission-artifacts-v1",
        "mode": "READ_ONLY",
        "config_sha256": config.source_sha256,
        "all_complete": result.all_complete,
        "artifacts": hashes,
    }
    _write_json(target / "manifest.json", manifest)
    return target


async def run_loaded_commissioning_config(
    config: LoadedCommissioningConfig,
    *,
    workflow_factory: WorkflowFactory = LiveCommissioningWorkflow,
) -> LiveCommissioningWorkflowResult:
    workflow = workflow_factory(config.specs, disconnect_when_done=True)
    return await workflow.run()


__all__ = [
    "LoadedCommissioningConfig",
    "commissioning_summary",
    "load_commissioning_config",
    "run_loaded_commissioning_config",
    "write_commissioning_artifacts",
]
