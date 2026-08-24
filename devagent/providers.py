from __future__ import annotations

import json
import math
import os
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from devagent.config import ProviderConfig


class ProviderError(RuntimeError):
    pass


class StructuredResponseError(ProviderError):
    """A bounded, safe description of malformed provider output."""


class ModelProvider(Protocol):
    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]: ...


_SYSTEM = """You are one role inside DevAgent, a local evidence-driven engineering system.
Output exactly one JSON object matching the supplied JSON schema; the schema is authoritative.
Every confidence value must be a number from 0.0 through 1.0, never low/medium/high.
Return no markdown and no extra fields. Use only supplied repository evidence, and never claim
commands ran; the deterministic harness runs commands and evaluates exit codes."""


@dataclass(frozen=True)
class _Violation:
    path: str
    expected: str
    actual: str


def _actual(value: Any, *, path: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean {value!r}"
    if isinstance(value, (int, float)):
        return f"{type(value).__name__} {value!r}"
    if isinstance(value, str):
        lowered = value.lower()
        if path.endswith("confidence") and lowered in {"low", "medium", "high"}:
            return f"string {value!r}"
        return f"string(length={len(value)})"
    if isinstance(value, list):
        return f"array(length={len(value)})"
    if isinstance(value, dict):
        return f"object(field_count={len(value)})"
    return type(value).__name__


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _expected_type(schema: dict[str, Any]) -> str:
    expected = schema.get("type", "value")
    if isinstance(expected, list):
        description = " or ".join(str(item) for item in expected)
    else:
        description = str(expected)
    if "minimum" in schema or "maximum" in schema:
        description += f" between {schema.get('minimum', '-infinity')} and {schema.get('maximum', 'infinity')}"
    if "minLength" in schema:
        description += f" with at least {schema['minLength']} character(s)"
    if "minItems" in schema:
        description += f" with at least {schema['minItems']} item(s)"
    if "enum" in schema:
        description += f" in {schema['enum']!r}"
    if "const" in schema:
        description += f" equal to {schema['const']!r}"
    return description


def _validate(value: Any, schema: dict[str, Any], path: str = "$") -> _Violation | None:
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        if any(_validate(value, alternative, path) is None for alternative in alternatives):
            return None
        return _Violation(path, "one of the allowed schema shapes", _actual(value, path=path))

    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected] if expected else []
    if expected_types and not any(_type_matches(value, item) for item in expected_types):
        return _Violation(path, _expected_type(schema), _actual(value, path=path))

    if "const" in schema and value != schema["const"]:
        return _Violation(path, _expected_type(schema), _actual(value, path=path))
    if "enum" in schema and value not in schema["enum"]:
        return _Violation(path, _expected_type(schema), _actual(value, path=path))

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for field in schema.get("required", []):
            if field not in value:
                return _Violation(f"{path}.{field}", "required field", "missing")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                field = extras[0]
                return _Violation(f"{path}.{field}", "no extra fields", "unexpected field")
        for field, item in value.items():
            if field in properties:
                violation = _validate(item, properties[field], f"{path}.{field}")
                if violation:
                    return violation

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return _Violation(path, _expected_type(schema), _actual(value, path=path))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return _Violation(path, f"array with at most {schema['maxItems']} item(s)", _actual(value, path=path))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                violation = _validate(item, item_schema, f"{path}[{index}]")
                if violation:
                    return violation

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return _Violation(path, _expected_type(schema), _actual(value, path=path))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return _Violation(path, "non-empty string containing non-whitespace text", _actual(value, path=path))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return _Violation(path, "finite " + _expected_type(schema), _actual(value, path=path))
        if "minimum" in schema and value < schema["minimum"]:
            return _Violation(path, _expected_type(schema), _actual(value, path=path))
        if "maximum" in schema and value > schema["maximum"]:
            return _Violation(path, _expected_type(schema), _actual(value, path=path))

    return None


def validate_response(role: str, response: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """Validate the supported JSON Schema subset without trusting provider enforcement."""
    violation = _validate(response, schema)
    if violation:
        raise StructuredResponseError(
            f"Invalid {role} response at {violation.path}: expected {violation.expected}; received {violation.actual}"
        )
    if not isinstance(response, dict):
        raise StructuredResponseError(f"Invalid {role} response at $: expected object; received {_actual(response, path='$')}")
    return response


def _decode_json(text: str, role: str) -> dict[str, Any]:
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise StructuredResponseError(
            f"Invalid {role} response at $: expected one JSON object; received invalid JSON at line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(value, dict):
        raise StructuredResponseError(
            f"Invalid {role} response at $: expected object; received {_actual(value, path='$')}"
        )
    return value


def _user_prompt(role: str, payload: dict[str, Any], schema: dict[str, Any]) -> str:
    return json.dumps({"role": role, "input": payload, "required_output_schema": schema}, ensure_ascii=False)


def _repair_prompt(violation: StructuredResponseError) -> str:
    return f"Schema violation only: {violation}. Return corrected JSON matching the supplied schema exactly."


def _request_with_repair(
    role: str,
    schema: dict[str, Any],
    send: Callable[[str | None, str | None], str | dict[str, Any]],
) -> dict[str, Any]:
    previous: str | None = None
    try:
        raw = send(None, None)
        previous = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        response = _decode_json(raw, role) if isinstance(raw, str) else raw
        return validate_response(role, response, schema)
    except StructuredResponseError as first_error:
        repair = _repair_prompt(first_error)

    try:
        raw = send(repair, previous)
        response = _decode_json(raw, role) if isinstance(raw, str) else raw
        return validate_response(role, response, schema)
    except StructuredResponseError as final_error:
        raise ProviderError(f"{final_error}; one structured-response repair attempt was exhausted") from final_error


def _schema_name(role: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", role)[:40]
    return f"devagent_{normalized}_response"


@dataclass
class OpenAICompatibleProvider:
    config: ProviderConfig

    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError("OpenAI SDK is not installed") from exc
        provider = self.config.provider.lower()
        api_key = os.getenv(self.config.api_key_env, "") if self.config.api_key_env else "not-required"
        if not api_key and provider != "compatible":
            raise ProviderError(f"Missing API key environment variable: {self.config.api_key_env}")
        client = OpenAI(api_key=api_key or "local", base_url=self.config.base_url, timeout=self.config.timeout_seconds)
        base_messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_prompt(role, payload, schema)},
        ]
        if provider == "openai":
            response_format: dict[str, Any] | None = {
                "type": "json_schema",
                "json_schema": {"name": _schema_name(role), "strict": True, "schema": schema},
            }
        else:
            response_format = {"type": "json_object"}
        compatible_format_supported = True

        def send(repair: str | None, previous: str | None) -> str:
            nonlocal compatible_format_supported
            messages = list(base_messages)
            if repair is not None:
                messages.extend(
                    [
                        {"role": "assistant", "content": previous or ""},
                        {"role": "user", "content": repair},
                    ]
                )
            kwargs: dict[str, Any] = {"model": self.config.model, "messages": messages}
            if response_format is not None and (provider == "openai" or compatible_format_supported):
                kwargs["response_format"] = response_format
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as exc:  # pragma: no cover - network/provider dependent
                if provider == "openai" or not compatible_format_supported:
                    label = "Provider" if provider == "openai" else "Compatible provider"
                    raise ProviderError(f"{label} request failed: {exc}") from exc
                compatible_format_supported = False
                kwargs.pop("response_format", None)
                try:
                    response = client.chat.completions.create(**kwargs)
                except Exception as fallback_exc:
                    raise ProviderError(f"Compatible provider request failed: {fallback_exc}") from fallback_exc
            content = response.choices[0].message.content
            return content or ""

        return _request_with_repair(role, schema, send)


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
        client = Anthropic(api_key=api_key, timeout=self.config.timeout_seconds)
        initial_prompt = _user_prompt(role, payload, schema)

        def send(repair: str | None, previous: str | None) -> str:
            messages = [{"role": "user", "content": initial_prompt}]
            if repair is not None:
                messages.extend(
                    [
                        {"role": "assistant", "content": previous or ""},
                        {"role": "user", "content": repair},
                    ]
                )
            try:
                response = client.messages.create(
                    model=self.config.model,
                    max_tokens=4096,
                    system=_SYSTEM,
                    messages=messages,
                )
            except Exception as exc:  # pragma: no cover
                raise ProviderError(f"Anthropic request failed: {exc}") from exc
            return "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")

        return _request_with_repair(role, schema, send)


class ScriptedFakeProvider:
    """Deterministic, credit-free provider used by tests and evaluation fixtures."""

    def __init__(self, responses: Iterable[dict[str, Any]]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, *, role: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        def send(repair: str | None, previous: str | None) -> dict[str, Any]:
            call_payload = payload if repair is None else {"schema_violation": repair}
            self.calls.append({"role": role, "payload": call_payload, "schema": schema, "repair": repair is not None})
            if not self.responses:
                raise ProviderError(f"Fake provider has no response remaining for role={role}")
            response = self.responses.popleft()
            if response.get("_role") not in (None, role):
                raise ProviderError(f"Fake response expected role={response['_role']}, received role={role}")
            return {key: value for key, value in response.items() if key != "_role"}

        return _request_with_repair(role, schema, send)


def create_provider(config: ProviderConfig) -> ModelProvider:
    provider = config.provider.lower()
    if provider in {"anthropic", "claude"}:
        return AnthropicProvider(config)
    if provider in {"openai", "xai", "grok", "compatible"}:
        if provider in {"xai", "grok"} and not config.base_url:
            config = ProviderConfig(provider, config.model, "https://api.x.ai/v1", config.api_key_env, config.timeout_seconds)
        return OpenAICompatibleProvider(config)
    raise ProviderError(f"Unsupported provider: {provider}")
