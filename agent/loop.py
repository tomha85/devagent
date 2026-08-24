"""Compatibility wrapper around the deterministic DevAgent orchestrator."""

from pathlib import Path
from typing import Optional

from devagent.config import ProviderConfig, load_config
from devagent.orchestrator import run_devagent
from devagent.providers import create_provider


def run_bugfix_loop(repo_path: str, task: str, max_steps: int = 8, provider: Optional[str] = None) -> str:
    del max_steps  # The deterministic lifecycle owns bounded correction budgets.
    configured = load_config()
    selected = ProviderConfig(provider or configured.provider, configured.model, configured.base_url, configured.api_key_env, configured.timeout_seconds)
    _, report = run_devagent(Path(repo_path), task, create_provider(selected))
    return report
