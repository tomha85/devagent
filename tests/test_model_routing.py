from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from devagent.cli import main
from devagent.config import (
    ProviderConfig,
    load_config,
    load_role_configs,
    save_config,
    save_role_config,
)
from devagent.routing import create_routed_provider, model_role_for_request, routing_lines


class RecordingProvider:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    def request(
        self,
        *,
        role: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(role)
        return {"provider": self.label, "role": role}


def test_internal_requests_map_to_stable_engineering_roles() -> None:
    assert model_role_for_request("understand") == "investigator"
    assert model_role_for_request("plan") == "planner"
    assert model_role_for_request("replan") == "planner"
    assert model_role_for_request("implement") == "implementer"
    assert model_role_for_request("diagnose") == "implementer"
    assert model_role_for_request("implement_replan") == "implementer"
    assert model_role_for_request("implement_review_fixes") == "implementer"
    assert model_role_for_request("review") == "reviewer"
    assert model_role_for_request("future_internal_role") is None


def test_role_router_uses_selected_provider_and_default_fallback() -> None:
    default_config = ProviderConfig("openai", "default-model")
    role_configs = {
        "investigator": ProviderConfig("anthropic", "investigator-model", api_key_env="ANTHROPIC_API_KEY"),
        "planner": ProviderConfig("xai", "planner-model", api_key_env="XAI_API_KEY"),
        "implementer": ProviderConfig("openai", "implementer-model"),
        "reviewer": ProviderConfig("compatible", "reviewer-model", "http://local/v1", "DEVAGENT_API_KEY"),
    }
    created: dict[str, RecordingProvider] = {}

    def factory(config: ProviderConfig) -> RecordingProvider:
        label = f"{config.provider}/{config.model}"
        provider = RecordingProvider(label)
        created[label] = provider
        return provider

    routed = create_routed_provider(default_config, role_configs, provider_factory=factory)
    schema = {"type": "object"}
    assert routed.request(role="understand", payload={}, schema=schema)["provider"] == "anthropic/investigator-model"
    assert routed.request(role="replan", payload={}, schema=schema)["provider"] == "xai/planner-model"
    assert routed.request(role="diagnose", payload={}, schema=schema)["provider"] == "openai/implementer-model"
    assert routed.request(role="review", payload={}, schema=schema)["provider"] == "compatible/reviewer-model"
    assert routed.request(role="future_internal_role", payload={}, schema=schema)["provider"] == "openai/default-model"


def test_role_config_round_trip_and_default_update_preserves_roles(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    default = ProviderConfig("openai", "gpt-default", api_key_env="OPENAI_API_KEY")
    reviewer = ProviderConfig("anthropic", "claude-review", api_key_env="ANTHROPIC_API_KEY")
    save_config(default, path)
    save_role_config("reviewer", reviewer, path)

    assert load_config(path) == default
    assert load_role_configs(path) == {"reviewer": reviewer}

    updated_default = ProviderConfig("xai", "grok-default", api_key_env="XAI_API_KEY")
    save_config(updated_default, path)
    assert load_config(path) == updated_default
    assert load_role_configs(path) == {"reviewer": reviewer}


def test_role_environment_override_does_not_require_role_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    save_config(ProviderConfig("openai", "gpt-default"), path)
    monkeypatch.setenv("DEVAGENT_REVIEWER_PROVIDER", "anthropic")
    monkeypatch.setenv("DEVAGENT_REVIEWER_MODEL", "claude-env-review")
    monkeypatch.setenv("DEVAGENT_REVIEWER_API_KEY_ENV", "ANTHROPIC_REVIEW_KEY")

    reviewer = load_role_configs(path)["reviewer"]
    assert reviewer.provider == "anthropic"
    assert reviewer.model == "claude-env-review"
    assert reviewer.api_key_env == "ANTHROPIC_REVIEW_KEY"


def test_routing_summary_does_not_expose_credentials() -> None:
    default = ProviderConfig("openai", "gpt-default", api_key_env="SECRET_ENV_NAME")
    roles = {"reviewer": ProviderConfig("anthropic", "claude-review", api_key_env="OTHER_SECRET")}
    output = "\n".join(routing_lines(default, roles))
    assert "openai/gpt-default" in output
    assert "reviewer: anthropic/claude-review" in output
    assert "SECRET_ENV_NAME" not in output
    assert "OTHER_SECRET" not in output


def test_cli_can_configure_role_and_show_model_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("DEVAGENT_CONFIG", str(path))

    assert main(["setup", "--provider", "openai", "--model", "gpt-default"]) == 0
    assert main(
        [
            "setup",
            "--role",
            "reviewer",
            "--provider",
            "anthropic",
            "--model",
            "claude-review",
        ]
    ) == 0
    assert main(["models"]) == 0

    output = capsys.readouterr().out
    assert "Configured reviewer role with anthropic" in output
    assert "DEVAGENT MODEL ROUTING" in output
    assert "default: openai/gpt-default" in output
    assert "reviewer: anthropic/claude-review" in output
    assert "investigator: openai/gpt-default (default)" in output
