from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]


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


def load_config(path: Path | None = None) -> ProviderConfig:
    target = path or config_path()
    data: dict[str, Any] = {}
    if target.is_file():
        with target.open("rb") as handle:
            loaded = tomllib.load(handle)
        data = loaded.get("provider", loaded)
    provider = os.getenv("DEVAGENT_PROVIDER", str(data.get("name", "openai"))).lower()
    defaults = {
        "openai": ("gpt-5", "OPENAI_API_KEY"),
        "anthropic": ("claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
        "claude": ("claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
        "xai": ("grok-4", "XAI_API_KEY"),
        "grok": ("grok-4", "XAI_API_KEY"),
        "compatible": ("local-model", "DEVAGENT_API_KEY"),
        "fake": ("fake", ""),
    }
    default_model, default_env = defaults.get(provider, defaults["compatible"])
    return ProviderConfig(
        provider=provider,
        model=os.getenv("DEVAGENT_MODEL", str(data.get("model", default_model))),
        base_url=os.getenv("DEVAGENT_BASE_URL") or data.get("base_url"),
        api_key_env=str(data.get("api_key_env", default_env)),
        timeout_seconds=int(data.get("timeout_seconds", 120)),
    )


def save_config(config: ProviderConfig, path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[provider]",
        f"name = {json.dumps(config.provider)}",
        f"model = {json.dumps(config.model)}",
        f"api_key_env = {json.dumps(config.api_key_env)}",
        f"timeout_seconds = {config.timeout_seconds}",
    ]
    if config.base_url:
        lines.append(f"base_url = {json.dumps(config.base_url)}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
