from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import commit_all
from devagent.config import ProviderConfig
from devagent.models import AgentState, Outcome
from devagent.orchestrator import (
    DIAGNOSE_SCHEMA,
    IMPLEMENT_SCHEMA,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    UNDERSTANDING_SCHEMA,
    DevAgent,
    OrchestrationError,
    _plan as parse_plan,
    _review as parse_review,
    _understanding,
)
from devagent.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderError,
    ScriptedFakeProvider,
    StructuredResponseError,
    validate_response,
)


def _understand() -> dict[str, Any]:
    return {
        "problem": "The current function returns the wrong value.",
        "expected_behavior": "The function returns the required value.",
        "affected_paths": ["app.py"],
        "root_cause": "app.py contains the incorrect return value.",
        "evidence": [
            {
                "statement": "The return statement contains the incorrect literal.",
                "paths": ["app.py"],
                "confidence": 0.95,
            }
        ],
        "proposed_solution": ["Replace the incorrect literal and verify the result."],
        "confidence": 0.9,
    }


def _plan() -> dict[str, Any]:
    return {
        "files_to_inspect": ["app.py"],
        "implementation": ["Replace the incorrect literal."],
        "verification": [["python", "-m", "compileall", "-q", "."]],
        "rationale": "This is the smallest evidence-backed change.",
    }


def _implement() -> dict[str, Any]:
    return {
        "actions": [
            {
                "tool": "replace_text",
                "arguments": {"path": "app.py", "old": "return 1", "new": "return 2"},
            }
        ],
        "summary": ["Corrected the return value."],
    }


def _diagnose() -> dict[str, Any]:
    return {
        "decision": "replan",
        "updated_hypothesis": "The original plan did not cover the failing path.",
        "actions": [],
    }


def _review() -> dict[str, Any]:
    return {"approved": True, "issues": [], "summary": "The focused change is verified."}


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ("low", "$.confidence"),
        (-0.1, "$.confidence"),
        (1.1, "$.confidence"),
    ],
)
def test_understanding_confidence_contract_rejects_qualitative_and_out_of_range_values(value: Any, field: str) -> None:
    response = _understand()
    response["confidence"] = value

    with pytest.raises(StructuredResponseError, match=rf"understand response at \{field}"):
        validate_response("understand", response, UNDERSTANDING_SCHEMA)


def test_understanding_rejects_qualitative_evidence_confidence() -> None:
    response = _understand()
    response["evidence"][0]["confidence"] = "high"

    with pytest.raises(StructuredResponseError, match=r"\$\.evidence\[0\]\.confidence"):
        validate_response("understand", response, UNDERSTANDING_SCHEMA)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confidence", "low"),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("evidence.confidence", "high"),
        ("evidence.confidence", 1.1),
    ],
)
def test_understanding_parser_defensively_rejects_invalid_confidence(field: str, value: Any) -> None:
    response = _understand()
    if field == "confidence":
        response["confidence"] = value
    else:
        response["evidence"][0]["confidence"] = value

    with pytest.raises(OrchestrationError, match="expected number from 0.0 through 1.0"):
        _understanding(response)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_understanding_rejects_missing_and_unexpected_fields(mutation: str) -> None:
    response = _understand()
    if mutation == "missing":
        del response["root_cause"]
    else:
        response["unexpected"] = "not allowed"

    with pytest.raises(StructuredResponseError):
        validate_response("understand", response, UNDERSTANDING_SCHEMA)


@pytest.mark.parametrize(
    ("role", "schema", "response"),
    [
        ("understand", UNDERSTANDING_SCHEMA, {**_understand(), "affected_paths": "app.py"}),
        ("plan", PLAN_SCHEMA, {**_plan(), "verification": ["pytest"]}),
        (
            "implement",
            IMPLEMENT_SCHEMA,
            {
                **_implement(),
                "actions": [
                    {
                        "tool": "write_file",
                        "arguments": {"path": "app.py", "content": "pass\n", "extra": True},
                    }
                ],
            },
        ),
        ("diagnose", DIAGNOSE_SCHEMA, {**_diagnose(), "decision": "guess"}),
        ("review", REVIEW_SCHEMA, {**_review(), "approved": "yes"}),
    ],
)
def test_every_role_schema_rejects_malformed_output(role: str, schema: dict[str, Any], response: dict[str, Any]) -> None:
    with pytest.raises(StructuredResponseError, match=f"Invalid {role} response"):
        validate_response(role, response, schema)


@pytest.mark.parametrize(
    ("role", "schema", "response"),
    [
        ("understand", UNDERSTANDING_SCHEMA, _understand()),
        ("plan", PLAN_SCHEMA, _plan()),
        ("implement", IMPLEMENT_SCHEMA, _implement()),
        ("diagnose", DIAGNOSE_SCHEMA, _diagnose()),
        ("review", REVIEW_SCHEMA, _review()),
    ],
)
def test_every_role_schema_accepts_its_complete_contract(role: str, schema: dict[str, Any], response: dict[str, Any]) -> None:
    assert validate_response(role, response, schema) == response
    assert schema["type"] == "object"
    assert schema["properties"]
    assert schema["additionalProperties"] is False


def test_plan_and_review_parsers_reject_invalid_shapes_cleanly() -> None:
    invalid_plan = _plan()
    invalid_plan["rationale"] = 7
    invalid_review = {
        "approved": False,
        "issues": [{"severity": "high", "reason": "Missing path"}],
        "summary": "Rejected.",
    }

    with pytest.raises(OrchestrationError, match="plan response field 'rationale'"):
        parse_plan(invalid_plan, [])
    with pytest.raises(OrchestrationError, match=r"issues\[0\]"):
        parse_review(invalid_review)


def test_qualitative_confidence_gets_one_bounded_repair_and_parses() -> None:
    malformed = _understand()
    malformed["confidence"] = "low"
    corrected = _understand()
    corrected["confidence"] = 0.82
    provider = ScriptedFakeProvider([malformed, corrected])

    response = provider.request(role="understand", payload={"task": "fixture"}, schema=UNDERSTANDING_SCHEMA)
    understanding = _understanding(response)

    assert understanding.confidence == 0.82
    assert len(provider.calls) == 2
    assert provider.calls[0]["repair"] is False
    assert provider.calls[1]["repair"] is True
    assert "$.confidence" in provider.calls[1]["payload"]["schema_violation"]


def test_structured_repair_is_exhausted_after_one_attempt() -> None:
    malformed = _understand()
    malformed["confidence"] = "low"
    provider = ScriptedFakeProvider([malformed, malformed, _understand()])

    with pytest.raises(ProviderError, match="one structured-response repair attempt was exhausted"):
        provider.request(role="understand", payload={}, schema=UNDERSTANDING_SCHEMA)

    assert len(provider.calls) == 2
    assert len(provider.responses) == 1


def test_no_modification_occurs_before_valid_understanding_and_plan(git_repo: Path) -> None:
    (git_repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    commit_all(git_repo)
    malformed = _understand()
    malformed["confidence"] = "low"
    invalid_plan = _plan()
    del invalid_plan["rationale"]
    provider = ScriptedFakeProvider([malformed, _understand(), invalid_plan, invalid_plan])

    result = DevAgent(provider).run(git_repo, "Fix the incorrect app return value")

    assert result.outcome is Outcome.BLOCKED
    assert result.changes.files_changed == 0
    assert AgentState.IMPLEMENT not in result.state_history
    assert (git_repo / "app.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"


def _openai_response(value: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])


def test_openai_sends_supplied_schema_as_strict_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Completions:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return _openai_response(_plan())

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setenv("OPENAI_TEST_KEY", "test-only")
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(ProviderConfig("openai", "gpt-5.6-sol", None, "OPENAI_TEST_KEY", 30))

    assert provider.request(role="plan", payload={"task": "fixture"}, schema=PLAN_SCHEMA) == _plan()
    response_format = calls[0]["response_format"]
    assert response_format == {
        "type": "json_schema",
        "json_schema": {"name": "devagent_plan_response", "strict": True, "schema": PLAN_SCHEMA},
    }
    assert "low/medium/high" in calls[0]["messages"][0]["content"]
    assert "no extra fields" in calls[0]["messages"][0]["content"]


def test_compatible_provider_falls_back_when_json_object_format_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Completions:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise RuntimeError("response_format unsupported")
            return _openai_response(_review())

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(ProviderConfig("compatible", "local", "http://localhost/v1", "", 30))

    assert provider.request(role="review", payload={}, schema=REVIEW_SCHEMA) == _review()
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]


def test_anthropic_uses_local_validation_and_bounded_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    responses = [{**_review(), "approved": "yes"}, _review()]

    class Messages:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            value = responses.pop(0)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(value))])

    client = SimpleNamespace(messages=Messages())
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "test-only")
    monkeypatch.setattr("anthropic.Anthropic", lambda **kwargs: client)
    provider = AnthropicProvider(ProviderConfig("anthropic", "claude-test", None, "ANTHROPIC_TEST_KEY", 30))

    assert provider.request(role="review", payload={}, schema=REVIEW_SCHEMA) == _review()
    assert len(calls) == 2
    assert calls[1]["messages"][-1]["role"] == "user"
    assert "Schema violation only" in calls[1]["messages"][-1]["content"]
