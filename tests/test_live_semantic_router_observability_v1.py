from __future__ import annotations

import pytest

from devagent.live.semantic_assistant import _DiagnosticProvider, _bounded_error_text
from devagent.providers import ProviderError


class _FailingProvider:
    def request(self, *, role, payload, schema):
        raise ProviderError("Missing API key environment variable: OPENAI_API_KEY")


def test_diagnostic_provider_preserves_bounded_provider_failure_reason() -> None:
    provider = _DiagnosticProvider(_FailingProvider())

    with pytest.raises(ProviderError):
        provider.request(
            role="live_semantic_intent_router",
            payload={"question": "is system good?"},
            schema={"type": "object"},
        )

    assert provider.last_error == "Missing API key environment variable: OPENAI_API_KEY"


def test_bounded_error_text_removes_multiline_noise_and_caps_size() -> None:
    raw = "provider failed\n" + ("x" * 500)
    rendered = _bounded_error_text(raw, limit=80)

    assert "\n" not in rendered
    assert len(rendered) <= 80
    assert rendered.endswith("...")
