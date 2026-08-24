from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from devagent.config import ProviderConfig


class ProviderError(RuntimeError):
    pass


class ModelProvider(Protocol):
    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]: ...


_SYSTEM = """You are one role inside DevAgent, a local evidence-driven engineering system.
Return one JSON object matching the supplied schema. Do not return markdown. Base every
claim on supplied repository evidence. Prefer the smallest correct change. Never claim
that a command ran; the deterministic harness runs commands and evaluates exit codes."""


def _decode_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(stripped.splitlines()[1:-1]).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Provider returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProviderError("Provider response must be a JSON object")
    return value


def _user_prompt(role: str, payload: dict[str, Any], schema: dict[str, Any]) -> str:
    return json.dumps({"role": role, "input": payload, "required_output_schema": schema}, ensure_ascii=False)


@dataclass
class OpenAICompatibleProvider:
    config: ProviderConfig

    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("OpenAI SDK is not installed") from exc
        api_key = os.getenv(self.config.api_key_env, "") if self.config.api_key_env else "not-required"
        if not api_key and self.config.provider != "compatible":
            raise ProviderError(f"Missing API key environment variable: {self.config.api_key_env}")
        client = OpenAI(api_key=api_key or "local", base_url=self.config.base_url, timeout=self.config.timeout_seconds)
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_prompt(role, payload, schema)},
        ]
        try:
            response = client.chat.completions.create(model=self.config.model, messages=messages, response_format={"type": "json_object"})
        except Exception as exc:  # pragma: no cover - network/provider dependent
            if self.config.provider != "compatible":
                raise ProviderError(f"Provider request failed: {exc}") from exc
            try:
                response = client.chat.completions.create(model=self.config.model, messages=messages)
            except Exception as fallback_exc:
                raise ProviderError(f"Compatible provider request failed: {fallback_exc}") from fallback_exc
        content = response.choices[0].message.content
        if not content:
            raise ProviderError("Provider returned an empty response")
        return _decode_json(content)


@dataclass
class AnthropicProvider:
    config: ProviderConfig

    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("Anthropic SDK is not installed") from exc
        api_key = os.getenv(self.config.api_key_env, "")
        if not api_key:
            raise ProviderError(f"Missing API key environment variable: {self.config.api_key_env}")
        try:
            response = Anthropic(api_key=api_key, timeout=self.config.timeout_seconds).messages.create(
                model=self.config.model,
                max_tokens=4096,
                system=_SYSTEM,
                messages=[{"role": "user", "content": _user_prompt(role, payload, schema)}],
            )
        except Exception as exc:  # pragma: no cover
            raise ProviderError(f"Anthropic request failed: {exc}") from exc
        text = "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return _decode_json(text)


class ScriptedFakeProvider:
    """Deterministic, credit-free provider used by tests and evaluation fixtures."""

    def __init__(self, responses: Iterable[dict[str, Any]]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"role": role, "payload": payload, "schema": schema})
        if not self.responses:
            raise ProviderError(f"Fake provider has no response remaining for role={role}")
        response = self.responses.popleft()
        if response.get("_role") not in (None, role):
            raise ProviderError(f"Fake response expected role={response['_role']}, received role={role}")
        return {key: value for key, value in response.items() if key != "_role"}


def create_provider(config: ProviderConfig) -> ModelProvider:
    provider = config.provider.lower()
    if provider in {"anthropic", "claude"}:
        return AnthropicProvider(config)
    if provider in {"openai", "xai", "grok", "compatible"}:
        if provider in {"xai", "grok"} and not config.base_url:
            config = ProviderConfig(provider, config.model, "https://api.x.ai/v1", config.api_key_env, config.timeout_seconds)
        return OpenAICompatibleProvider(config)
    raise ProviderError(f"Unsupported provider: {provider}")
