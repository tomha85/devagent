from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from devagent.config import ProviderConfig, ROLE_NAMES, provider_qualification
from devagent.providers import ModelProvider, create_provider


_REQUEST_ROLE_MAP: dict[str, str] = {
    "understand": "investigator",
    "plan": "planner",
    "replan": "planner",
    "implement": "implementer",
    "diagnose": "implementer",
    "implement_replan": "implementer",
    "implement_review_fixes": "implementer",
    "review": "reviewer",
}


def model_role_for_request(role: str) -> str | None:
    """Map an internal orchestration request to a stable user-facing model role."""

    return _REQUEST_ROLE_MAP.get(role)


@dataclass
class RoleRoutingProvider:
    """Route model calls by engineering role while preserving one ModelProvider contract."""

    default_provider: ModelProvider
    role_providers: dict[str, ModelProvider]

    def request(
        self,
        *,
        role: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        model_role = model_role_for_request(role)
        provider = self.role_providers.get(model_role, self.default_provider)
        return provider.request(role=role, payload=payload, schema=schema)


def create_routed_provider(
    default_config: ProviderConfig,
    role_configs: dict[str, ProviderConfig],
    *,
    provider_factory: Callable[[ProviderConfig], ModelProvider] = create_provider,
) -> RoleRoutingProvider:
    invalid = sorted(set(role_configs) - set(ROLE_NAMES))
    if invalid:
        raise ValueError(f"Unsupported model role(s): {', '.join(invalid)}")
    default_provider = provider_factory(default_config)
    role_providers = {
        role: provider_factory(config)
        for role, config in role_configs.items()
    }
    return RoleRoutingProvider(default_provider, role_providers)


def routing_lines(
    default_config: ProviderConfig,
    role_configs: dict[str, ProviderConfig],
) -> list[str]:
    """Return a safe provider/model routing summary with no credentials."""

    lines = [
        f"default: {default_config.provider}/{default_config.model} "
        f"[{provider_qualification(default_config.provider)}]"
    ]
    for role in ROLE_NAMES:
        config = role_configs.get(role, default_config)
        suffix = "" if role in role_configs else " (default)"
        lines.append(
            f"{role}: {config.provider}/{config.model} "
            f"[{provider_qualification(config.provider)}]{suffix}"
        )
    return lines
