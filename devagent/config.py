from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]


ROLE_NAMES = ("investigator", "planner", "implementer", "reviewer")

_PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai": ("gpt-5", "OPENAI_API_KEY"),
    "anthropic": ("claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
    "claude": ("claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
    "xai": ("grok-4", "XAI_API_KEY"),
    "grok": ("grok-4", "XAI_API_KEY"),
    "gemini": ("gemini-3.7-flash", "GEMINI_API_KEY"),
    "google": ("gemini-3.7-flash", "GEMINI_API_KEY"),
    "compatible": ("local-model", "DEVAGENT_API_KEY"),
    "fake": ("fake", ""),
}

_PROVIDER_QUALIFICATION: dict[str, str] = {
    "openai": "CONTRACT-QUALIFIED",
    "anthropic": "CONTRACT-QUALIFIED",
    "claude": "CONTRACT-QUALIFIED",
    "xai": "CONTRACT-QUALIFIED",
    "grok": "CONTRACT-QUALIFIED",
    "gemini": "CONTRACT-QUALIFIED",
    "google": "CONTRACT-QUALIFIED",
    "compatible": "SUPPORTED",
    "fake": "TEST-ONLY",
}


@dataclass(frozen=True)
class ProviderConfig:
    provider: str = "openai"
    model: str = "gpt-5"
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: int = 120


def config_path() -> Path:
    override = os.getenv("DEVAGENT_CONFIG")
    if override:
        return Path(override).expanduser()
    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "devagent" / "config.toml"


def provider_defaults(provider: str) -> tuple[str, str]:
    return _PROVIDER_DEFAULTS.get(provider.lower(), _PROVIDER_DEFAULTS["compatible"])


def provider_qualification(provider: str) -> str:
    """Return the bounded qualification level for a provider adapter.

    CONTRACT-QUALIFIED means deterministic provider-contract tests exist. It is not a
    claim that a specific paid model/API key is currently reachable; `doctor --live`
    performs that explicit runtime check.
    """

    return _PROVIDER_QUALIFICATION.get(provider.lower(), "EXPERIMENTAL")


def _read_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    return loaded if isinstance(loaded, dict) else {}


def _from_table(
    data: dict[str, Any],
    *,
    inherited: ProviderConfig | None = None,
) -> ProviderConfig:
    provider = str(data.get("name", inherited.provider if inherited else "openai")).lower()
    default_model, default_env = provider_defaults(provider)
    model_fallback = inherited.model if inherited and provider == inherited.provider else default_model
    base_url_fallback = inherited.base_url if inherited and provider == inherited.provider else None
    env_fallback = inherited.api_key_env if inherited and provider == inherited.provider else default_env
    timeout_fallback = inherited.timeout_seconds if inherited else 120
    return ProviderConfig(
        provider=provider,
        model=str(data.get("model", model_fallback)),
        base_url=data.get("base_url", base_url_fallback),
        api_key_env=str(data.get("api_key_env", env_fallback)),
        timeout_seconds=int(data.get("timeout_seconds", timeout_fallback)),
    )


def load_config(path: Path | None = None) -> ProviderConfig:
    target = path or config_path()
    loaded = _read_raw(target)
    data = loaded.get("provider", loaded)
    if not isinstance(data, dict):
        data = {}
    file_config = _from_table(data)
    provider = os.getenv("DEVAGENT_PROVIDER", file_config.provider).lower()
    default_model, default_env = provider_defaults(provider)
    same_provider = provider == file_config.provider
    return ProviderConfig(
        provider=provider,
        model=os.getenv("DEVAGENT_MODEL", file_config.model if same_provider else default_model),
        base_url=os.getenv("DEVAGENT_BASE_URL") or (file_config.base_url if same_provider else None),
        api_key_env=os.getenv(
            "DEVAGENT_API_KEY_ENV",
            file_config.api_key_env if same_provider else default_env,
        ),
        timeout_seconds=int(os.getenv("DEVAGENT_TIMEOUT_SECONDS", str(file_config.timeout_seconds))),
    )


def load_role_configs(path: Path | None = None) -> dict[str, ProviderConfig]:
    """Load optional model/provider overrides for stable engineering roles.

    A role is configured only when its TOML table exists or at least one role-specific
    DEVAGENT_<ROLE>_* environment variable is present. Unconfigured roles fall back to
    the default provider at routing time.
    """

    target = path or config_path()
    loaded = _read_raw(target)
    roles_data = loaded.get("roles", {})
    if not isinstance(roles_data, dict):
        roles_data = {}
    default = load_config(target)
    result: dict[str, ProviderConfig] = {}
    for role in ROLE_NAMES:
        raw = roles_data.get(role, {})
        if not isinstance(raw, dict):
            raw = {}
        prefix = f"DEVAGENT_{role.upper()}_"
        env_present = any(
            os.getenv(prefix + suffix) is not None
            for suffix in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY_ENV", "TIMEOUT_SECONDS")
        )
        if not raw and not env_present:
            continue
        file_config = _from_table(raw, inherited=default)
        provider = os.getenv(prefix + "PROVIDER", file_config.provider).lower()
        default_model, default_env = provider_defaults(provider)
        same_provider = provider == file_config.provider
        result[role] = ProviderConfig(
            provider=provider,
            model=os.getenv(prefix + "MODEL", file_config.model if same_provider else default_model),
            base_url=os.getenv(prefix + "BASE_URL") or (file_config.base_url if same_provider else None),
            api_key_env=os.getenv(
                prefix + "API_KEY_ENV",
                file_config.api_key_env if same_provider else default_env,
            ),
            timeout_seconds=int(
                os.getenv(prefix + "TIMEOUT_SECONDS", str(file_config.timeout_seconds))
            ),
        )
    return result


def _render_section(name: str, config: ProviderConfig) -> list[str]:
    lines = [
        f"[{name}]",
        f"name = {json.dumps(config.provider)}",
        f"model = {json.dumps(config.model)}",
        f"api_key_env = {json.dumps(config.api_key_env)}",
        f"timeout_seconds = {config.timeout_seconds}",
    ]
    if config.base_url:
        lines.append(f"base_url = {json.dumps(config.base_url)}")
    return lines


def _raw_role_configs(path: Path, default: ProviderConfig) -> dict[str, ProviderConfig]:
    loaded = _read_raw(path)
    roles_data = loaded.get("roles", {})
    if not isinstance(roles_data, dict):
        return {}
    result: dict[str, ProviderConfig] = {}
    for role in ROLE_NAMES:
        raw = roles_data.get(role)
        if isinstance(raw, dict):
            result[role] = _from_table(raw, inherited=default)
    return result


def _write_config(
    target: Path,
    default: ProviderConfig,
    roles: dict[str, ProviderConfig],
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = _render_section("provider", default)
    for role in ROLE_NAMES:
        config = roles.get(role)
        if config is not None:
            lines.extend(["", *_render_section(f"roles.{role}", config)])
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def save_config(config: ProviderConfig, path: Path | None = None) -> Path:
    """Save the default provider without deleting existing per-role routing."""

    target = path or config_path()
    roles = _raw_role_configs(target, config)
    return _write_config(target, config, roles)


def save_role_config(
    role: str,
    config: ProviderConfig,
    path: Path | None = None,
) -> Path:
    if role not in ROLE_NAMES:
        raise ValueError(f"Unsupported model role: {role}")
    target = path or config_path()
    loaded = _read_raw(target)
    raw_default = loaded.get("provider", loaded)
    if not isinstance(raw_default, dict):
        raw_default = {}
    default = _from_table(raw_default)
    roles = _raw_role_configs(target, default)
    roles[role] = config
    return _write_config(target, default, roles)
