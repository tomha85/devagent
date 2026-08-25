from __future__ import annotations

from pathlib import Path

from devagent.cli import _read_requirement_file
from devagent.models import (
    AcceptanceSource,
    Capability,
    CapabilityProvenance,
    Component,
    RepositoryModel,
    TaskType,
)
from devagent.tasking import compile_task, enrich_acceptance_contract


def _repo(*, language: str = "python", framework: str = "FastAPI") -> RepositoryModel:
    return RepositoryModel(
        root="/repo",
        kind="single-component",
        components=[
            Component(
                path=".",
                languages=[language],
                frameworks=[framework] if framework else [],
                manifests=["pyproject.toml" if language == "python" else "pom.xml"],
                test_locations=["tests"],
                capabilities=[
                    Capability(
                        kind="test",
                        command=("python", "-m", "pytest", "-q"),
                        source="pyproject.toml",
                        provenance=CapabilityProvenance.EXPLICIT,
                    )
                ],
            )
        ],
        facts=[],
        git_head="abc",
    )


def _user_criteria(task) -> list[str]:
    return [
        item.description
        for item in task.acceptance_criteria
        if item.source is AcceptanceSource.USER
    ]


def test_rough_terminal_prompt_becomes_repository_aware_engineering_brief() -> None:
    task = compile_task("add login google")
    assert task.task_type is TaskType.FEATURE

    enrich_acceptance_contract(task, _repo())

    assert task.goal.startswith("Add login google\n\nDEVAGENT REQUIREMENT INTELLIGENCE")
    assert "USER REQUIREMENTS\n- Add login google" in task.goal
    assert "SAFE ENGINEERING DEFAULTS" in task.goal
    assert "existing architecture and naming conventions" in task.goal
    assert "never hardcode credentials" in task.goal
    assert "Frameworks: FastAPI" in task.goal
    assert "Languages: python" in task.goal
    assert "Evidence-backed verification: python -m pytest -q" in task.goal
    assert "Do not invent material product, business, security" in task.goal
    assert _user_criteria(task) == ["Add login google"]


def test_terminal_and_file_path_feed_identical_requirement_intelligence(tmp_path: Path) -> None:
    text = "add CSV export for filtered reports and preserve JSON export"
    requirement = tmp_path / "customer-request.anything"
    requirement.write_text(text + "\n", encoding="utf-8")

    direct = compile_task(text)
    from_file = compile_task(_read_requirement_file(requirement))
    enrich_acceptance_contract(direct, _repo())
    enrich_acceptance_contract(from_file, _repo())

    assert direct.goal == from_file.goal
    assert _user_criteria(direct) == _user_criteria(from_file)


def test_unstructured_multiline_customer_note_does_not_lose_fragments() -> None:
    requirement = """customer need export
csv maybe
filtered data
keep json old one
button ui
"""
    task = compile_task(requirement)
    user = " ".join(_user_criteria(task)).lower()

    assert "export" in user
    assert "csv" in user
    assert "filtered data" in user
    assert "json" in user
    assert "button ui" in user

    enrich_acceptance_contract(task, _repo())
    assert "CSV" in task.goal
    assert "JSON" in task.goal
    assert "UI" in task.goal
    assert "not invented business requirements" in task.goal


def test_structured_file_goal_and_explicit_requirements_remain_authoritative() -> None:
    requirement = """Goal: Add CSV export for filtered reports

Requirements:
- Preserve existing JSON export behavior
- Export the currently filtered result set

Constraints:
- Do not change the existing JSON API
"""
    task = compile_task(requirement)
    assert task.goal == "Add CSV export for filtered reports"
    assert _user_criteria(task) == [
        "Preserve existing JSON export behavior",
        "Export the currently filtered result set",
        "Do not change the existing JSON API",
    ]

    enrich_acceptance_contract(task, _repo())
    assert task.goal.startswith("Add CSV export for filtered reports\n\nDEVAGENT REQUIREMENT INTELLIGENCE")
    for item in _user_criteria(task):
        assert f"- {item}" in task.goal


def test_requirement_intelligence_does_not_invent_payment_policy() -> None:
    task = compile_task("handle payment failure")
    enrich_acceptance_contract(task, _repo())
    lowered = task.goal.lower()

    # Payment-specific safety guidance is added only because the request is about
    # payment. It guides implementation without becoming a fabricated USER criterion.
    assert "do not invent retry counts, fees, cancellation policy" in lowered
    assert _user_criteria(task) == ["Handle payment failure"]
    assert not any("three retries" in item.lower() for item in _user_criteria(task))
    assert not any("cancel" in item.lower() for item in _user_criteria(task))
    assert not any("fee" in item.lower() for item in _user_criteria(task))


def test_generic_prompt_does_not_gain_unrelated_domain_retrieval_terms() -> None:
    task = compile_task("add report search")
    enrich_acceptance_contract(task, _repo())
    lowered = task.goal.lower()

    assert "retry counts" not in lowered
    assert "oauth scopes" not in lowered
    assert "payment state transitions" not in lowered


def test_performance_shorthand_gets_safe_design_without_fake_target() -> None:
    task = compile_task("make checkout faster")
    assert task.task_type is TaskType.PERFORMANCE
    enrich_acceptance_contract(task, _repo())

    assert "Preserve functional behavior" in task.goal
    assert "do not invent an unrequested numeric target" in task.goal
    assert _user_criteria(task) == ["Make checkout faster"]


def test_detailed_callable_contract_is_preserved_inside_new_design_brief() -> None:
    requirement = "Add calculate_total(items) and preserve calculate_tax behavior"
    task = compile_task(requirement)
    enrich_acceptance_contract(task, _repo())

    assert task.goal.startswith(requirement + "\n\nDEVAGENT REQUIREMENT INTELLIGENCE")
    assert _user_criteria(task) == [requirement]
