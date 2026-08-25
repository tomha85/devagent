from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devagent import __version__
from devagent.config import ProviderConfig, provider_defaults
from devagent.orchestrator import REVIEW_SCHEMA
from devagent.providers import OpenAICompatibleProvider, create_provider


def _review() -> dict[str, Any]:
    return {"approved": True, "issues": [], "summary": "Qualified."}


def test_gemini_provider_defaults_are_first_class() -> None:
    assert provider_defaults("gemini") == ("gemini-3.7-flash", "GEMINI_API_KEY")
    assert provider_defaults("google") == ("gemini-3.7-flash", "GEMINI_API_KEY")


def test_gemini_uses_official_openai_compatibility_endpoint_and_local_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    clients: list[dict[str, Any]] = []

    class Completions:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_review())))]
            )

    def client_factory(**kwargs: Any) -> SimpleNamespace:
        clients.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    monkeypatch.setenv("GEMINI_TEST_KEY", "test-only")
    monkeypatch.setattr("openai.OpenAI", client_factory)

    provider = create_provider(
        ProviderConfig("gemini", "gemini-3.7-flash", None, "GEMINI_TEST_KEY", 45)
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.request(role="review", payload={}, schema=REVIEW_SCHEMA) == _review()
    assert clients[0]["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert clients[0]["timeout"] == 45
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_release_version_is_semver_and_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert match
    version = match.group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert version == __version__
    assert version == "0.7.0"


def test_release_workflow_requires_green_exact_main_revision() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "release-on-ci.yml").read_text(encoding="utf-8")

    assert 'workflows: ["Production CI"]' in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$VERIFIED_SHA"' in workflow


def test_pypi_workflow_uses_trusted_publishing_and_exact_tag_build() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "publish-pypi.yml").read_text(encoding="utf-8")

    assert "ref: ${{ needs.resolve.outputs.tag }}" in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "python -m twine check dist/*" in workflow
