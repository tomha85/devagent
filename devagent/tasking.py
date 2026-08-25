from __future__ import annotations

import re
from typing import Any

from devagent.models import (
    AcceptanceCriterion,
    AcceptanceSource,
    RiskLevel,
    TaskSpec,
    TaskType,
)


_CLASSIFIERS: tuple[tuple[TaskType, tuple[str, ...]], ...] = (
    (TaskType.BUILD_FAILURE, ("build fail", "compile error", "won't build", "does not build")),
    (TaskType.TEST_FAILURE, ("test fail", "failing test", "pytest error")),
    (TaskType.RUNTIME_ERROR, ("traceback", "exception", "runtime error", "crash")),
    (TaskType.MIGRATION, ("migration", "migrate ", "schema change", "alembic", "database migration")),
    (TaskType.PERFORMANCE, ("performance", "optimize", "slow", "latency", "n+1", "faster", "speed up")),
    (TaskType.REFACTOR, ("refactor", "restructure", "cleanup", "rename ", "move ", "delete obsolete")),
    (TaskType.UNIT_TEST, ("add unit test", "write tests", "test coverage")),
    (TaskType.BUG_FIX, ("fix", "bug", "incorrect", "broken", "regression failure", "regression bug")),
    (TaskType.FEATURE, ("add ", "implement", "support ", "feature", "create ")),
)

_HIGH_RISK = {
    "auth",
    "authentication",
    "authorization",
    "permission",
    "payment",
    "migration",
    "migrate",
    "schema",
    "crypto",
    "credential",
    "public api",
    "concurrency",
}

_REQUIREMENT_SECTIONS = {
    "requirements",
    "required changes",
    "acceptance criteria",
    "acceptance",
    "tests",
    "verification",
    "constraints",
}
_KNOWN_SECTIONS = _REQUIREMENT_SECTIONS | {
    "goal",
    "context",
    "current behavior",
    "current behaviour",
    "non-goals",
    "non goals",
    "notes",
    "engineering design",
    "engineering context",
}
_DIRECTIVE = re.compile(
    r"^(?:add|create|implement|support|preserve|keep|ensure|require|must|should|when|do not|don't|"
    r"verify|run|return|raise|allow|prevent|maintain|migrate|refactor|rename|move|delete|update|fix|handle)\b",
    re.IGNORECASE,
)

# Bounded normalization for terse user intent. This intentionally fixes common
# engineering shorthand and spelling without attempting to invent product behavior.
_OPERATION_ALIASES: tuple[tuple[str, str], ...] = (
    ("substraction", "subtraction"),
    ("substract", "subtract"),
    ("multipy", "multiply"),
    ("mutiply", "multiply"),
    ("authentification", "authentication"),
    ("autorization", "authorization"),
    ("loging", "login"),
)
_ACRONYMS = {
    "api": "API",
    "csv": "CSV",
    "db": "DB",
    "http": "HTTP",
    "https": "HTTPS",
    "json": "JSON",
    "jwt": "JWT",
    "oauth": "OAuth",
    "sql": "SQL",
    "ui": "UI",
    "url": "URL",
}


def _classify(text: str) -> TaskType:
    lowered = text.lower()
    for task_type, needles in _CLASSIFIERS:
        if any(needle in lowered for needle in needles):
            return task_type
    return TaskType.GENERAL_ENGINEERING_TASK


def _risk(text: str, task_type: TaskType) -> RiskLevel:
    lowered = text.lower()
    if task_type is TaskType.MIGRATION or any(term in lowered for term in _HIGH_RISK):
        return RiskLevel.HIGH
    if task_type in {TaskType.FEATURE, TaskType.PERFORMANCE, TaskType.REFACTOR}:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _clean_requirement_item(value: str) -> str:
    value = re.sub(r"^[-*+]\s+", "", value.strip())
    value = re.sub(r"^\d+[.)]\s+", "", value)
    return re.sub(r"\s+", " ", value).strip().rstrip(".;")


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = re.sub(r"\W+", " ", item.lower()).strip()
        if item and normalized not in seen:
            result.append(item)
            seen.add(normalized)
    return result


def _section_header(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    markdown = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
    if markdown:
        return markdown.group(1).strip().rstrip(":").lower(), ""
    colon = re.match(r"^([A-Za-z][A-Za-z0-9 _/-]{0,80})\s*:\s*(.*)$", stripped)
    if colon and colon.group(1).strip().lower() in _KNOWN_SECTIONS:
        return colon.group(1).strip().lower(), colon.group(2).strip()
    return None


def _extract_goal(requirement: str) -> str:
    """Prefer an explicit Goal section while preserving ordinary free-form input."""

    lines = requirement.splitlines()
    for index, raw in enumerate(lines):
        header = _section_header(raw)
        if header is None or header[0] != "goal":
            continue
        _, inline = header
        if inline:
            return _clean_requirement_item(inline)
        collected: list[str] = []
        for candidate in lines[index + 1 :]:
            if _section_header(candidate) is not None:
                break
            if candidate.strip():
                collected.append(_clean_requirement_item(candidate))
        if collected:
            return " ".join(collected)
    return re.sub(r"\s+", " ", requirement).strip()


def _polish_plain_goal(value: str) -> str:
    """Improve readability without changing the requested product semantics."""

    result = re.sub(r"\s+", " ", value).strip()
    for source, destination in _OPERATION_ALIASES:
        result = re.sub(rf"\b{re.escape(source)}\b", destination, result, flags=re.IGNORECASE)
    result = re.sub(r"^customer\s+(?:need|needs|want|wants)\s+", "Implement ", result, flags=re.IGNORECASE)
    result = re.sub(r"^user\s+(?:need|needs|want|wants)\s+", "Implement ", result, flags=re.IGNORECASE)
    for source, destination in _ACRONYMS.items():
        result = re.sub(rf"\b{source}\b", destination, result, flags=re.IGNORECASE)
    if result:
        result = result[0].upper() + result[1:]
    return result.rstrip(".;")


def _normalize_terse_requirement(requirement: str) -> str:
    """Compile common rough prompts into a clearer bounded engineering request."""

    raw_value = _extract_goal(requirement)
    value = _polish_plain_goal(raw_value)
    if not value:
        return value
    # An explicit callable name is already a precise user contract; never rename it.
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(", value):
        return value

    # Common shorthand from natural prompts such as "addition 2 matrix 2x2".
    matrix_match = re.search(
        r"\b(add(?:ition)?|sum|subtract(?:ion)?|multiply|multiplication|divide|division)\b"
        r"(?:\s+(?:of|for))?\s+(?:2|two)\s+matrix(?:es)?\s+(\d+x\d+)\b",
        value,
        flags=re.IGNORECASE,
    )
    if matrix_match:
        operation = matrix_match.group(1).lower()
        dimension = matrix_match.group(2).lower()
        canonical_operation = {
            "add": "addition",
            "addition": "addition",
            "sum": "addition",
            "subtract": "subtraction",
            "subtraction": "subtraction",
            "multiply": "multiplication",
            "multiplication": "multiplication",
            "divide": "division",
            "division": "division",
        }[operation]
        prefix = "Add" if re.search(r"\b(add|new|function|implement)\b", value, re.IGNORECASE) else "Implement"
        return (
            f"{prefix} a matrix {canonical_operation} function for two {dimension} matrices "
            f"(matrix inputs)"
        )

    value = re.sub(r"\b2\s+matrix\b", "two matrices", value, flags=re.IGNORECASE)
    value = re.sub(r"\b2\s+file\b", "two files", value, flags=re.IGNORECASE)
    value = re.sub(r"\b2\s+test\b", "two tests", value, flags=re.IGNORECASE)
    return value


def _user_acceptance_items(requirement: str) -> list[str]:
    lines = requirement.splitlines()
    explicit: list[str] = []
    active_section: str | None = None
    recognized_section = False
    nonempty_lines = [line.strip() for line in lines if line.strip()]

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue

        bullet = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped)
        if bullet:
            if active_section in _REQUIREMENT_SECTIONS:
                explicit.append(_clean_requirement_item(stripped))
            continue

        header = _section_header(stripped)
        if header is not None:
            recognized_section = True
            name, inline = header
            active_section = name if name in _REQUIREMENT_SECTIONS else None
            if active_section is not None and inline:
                explicit.append(_clean_requirement_item(inline))
            continue

        if active_section in _REQUIREMENT_SECTIONS:
            item = _clean_requirement_item(stripped)
            if (
                active_section in {"tests", "verification", "constraints"}
                or _DIRECTIVE.match(item)
                or re.search(r"\b(?:must|should|shall)\b", item, re.IGNORECASE)
            ):
                explicit.append(item)

    explicit = _dedupe(explicit)
    if explicit:
        return explicit

    candidates = re.split(r"(?<=[.!?;])\s+|\n+", requirement)
    directives: list[str] = []
    for candidate in candidates:
        item = _clean_requirement_item(candidate)
        item = re.sub(r"^(?:goal|requirement|task)\s*:\s*", "", item, flags=re.IGNORECASE)
        if not item:
            continue
        if _DIRECTIVE.match(item) or re.search(r"\b(?:must|should|shall)\b", item, re.IGNORECASE):
            directives.append(item)
    directives = _dedupe(directives)

    # For a loose multi-line customer note, do not silently discard fragments merely
    # because one line happens to begin with a directive. Preserve the whole intent as
    # one user criterion unless the text is clearly a structured directive list.
    if len(nonempty_lines) > 1 and not recognized_section:
        all_directive_like = all(
            _DIRECTIVE.match(_clean_requirement_item(item))
            or re.search(r"\b(?:must|should|shall)\b", item, re.IGNORECASE)
            for item in nonempty_lines
        )
        if not all_directive_like:
            return [re.sub(r"\s+", " ", requirement).strip()]

    if directives:
        return directives
    return [re.sub(r"\s+", " ", requirement).strip()]


def _append_criterion(
    criteria: list[AcceptanceCriterion],
    description: str,
    *,
    source: AcceptanceSource,
    required: bool = True,
    verification_command: tuple[str, ...] | None = None,
) -> None:
    normalized = re.sub(r"\W+", " ", description.lower()).strip()
    if any(re.sub(r"\W+", " ", item.description.lower()).strip() == normalized for item in criteria):
        return
    criteria.append(
        AcceptanceCriterion(
            description=description,
            required=required,
            source=source,
            verification_command=verification_command,
        )
    )


def compile_task(requirement: str) -> TaskSpec:
    raw_goal = re.sub(r"\s+", " ", requirement).strip()
    if not raw_goal:
        raise ValueError("Engineering requirement cannot be empty")
    goal = _normalize_terse_requirement(requirement)
    task_type = _classify(goal)
    code_change = task_type is not TaskType.UNIT_TEST or "only" not in goal.lower()
    requires_tests = task_type is not TaskType.BUILD_FAILURE

    criteria: list[AcceptanceCriterion] = []
    user_items = _user_acceptance_items(requirement)
    if len(user_items) == 1 and user_items[0] == raw_goal and goal != raw_goal:
        user_items = [goal]
    for item in user_items:
        _append_criterion(criteria, item, source=AcceptanceSource.USER)

    if task_type in {TaskType.BUG_FIX, TaskType.RUNTIME_ERROR, TaskType.TEST_FAILURE}:
        _append_criterion(criteria, "The root cause is addressed, not merely masked", source=AcceptanceSource.TASK_POLICY)
        _append_criterion(criteria, "Regression coverage exercises the failing case", source=AcceptanceSource.TASK_POLICY)
    elif task_type is TaskType.FEATURE:
        _append_criterion(criteria, "The feature follows existing repository conventions", source=AcceptanceSource.TASK_POLICY)
        _append_criterion(criteria, "Normal and relevant edge-case behavior are covered", source=AcceptanceSource.TASK_POLICY)
    elif task_type is TaskType.REFACTOR:
        _append_criterion(
            criteria,
            "Externally observable behavior remains unchanged unless explicitly requested",
            source=AcceptanceSource.TASK_POLICY,
        )
        _append_criterion(criteria, "Regression coverage protects the refactored behavior", source=AcceptanceSource.TASK_POLICY)
    elif task_type is TaskType.MIGRATION:
        _append_criterion(
            criteria,
            "Migration preserves compatibility with the current supported application contract",
            source=AcceptanceSource.TASK_POLICY,
        )
        _append_criterion(
            criteria,
            "Migration has an explicit forward and rollback or safe non-reversible strategy",
            source=AcceptanceSource.TASK_POLICY,
        )
        _append_criterion(
            criteria,
            "Migration behavior is covered against representative existing state",
            source=AcceptanceSource.TASK_POLICY,
        )
    elif task_type is TaskType.UNIT_TEST:
        _append_criterion(criteria, "Tests assert externally meaningful behavior", source=AcceptanceSource.TASK_POLICY)

    _append_criterion(criteria, "No unrelated behavior changes", source=AcceptanceSource.QUALITY_GATE)
    if requires_tests:
        _append_criterion(criteria, "Relevant automated tests pass", source=AcceptanceSource.QUALITY_GATE)
    _append_criterion(criteria, "Final diff and independent review pass", source=AcceptanceSource.QUALITY_GATE)

    return TaskSpec(
        task_type=task_type,
        goal=goal,
        requires_code_change=code_change,
        requires_tests=requires_tests,
        acceptance_criteria=criteria,
        risk=_risk(goal, task_type),
    )


def _repository_language(repository: Any) -> str | None:
    languages = [
        language.lower()
        for component in repository.components
        for language in component.languages
    ]
    for preferred in ("python", "java", "csharp", "c#", "javascript", "typescript", "go", "rust"):
        if preferred in languages:
            return preferred
    return languages[0] if languages else None


def _matrix_operation_contract(task: TaskSpec, repository: Any) -> None:
    """Turn the bounded matrix shorthand compiler output into a repo-style callable contract."""

    match = re.fullmatch(
        r"(?:Add|Implement) a matrix (addition|subtraction|multiplication|division) "
        r"function for two (\d+x\d+) matrices \(matrix inputs\)",
        task.goal,
    )
    if match is None:
        return

    operation, dimension = match.groups()
    verb = {
        "addition": "add",
        "subtraction": "subtract",
        "multiplication": "multiply",
        "division": "divide",
    }[operation]
    language = _repository_language(repository)
    if language in {"java", "javascript", "typescript"}:
        symbol = f"{verb}Matrices{dimension}"
    elif language in {"csharp", "c#"}:
        symbol = f"{verb.capitalize()}Matrices{dimension}"
    else:
        symbol = f"{verb}_matrices_{dimension}"

    compiled = (
        f"Add {symbol}(a, b) to perform element-wise matrix {operation} "
        f"for two {dimension} matrices"
    )
    task.goal = compiled
    user_criteria = [
        criterion for criterion in task.acceptance_criteria if criterion.source is AcceptanceSource.USER
    ]
    if len(user_criteria) == 1:
        user_criteria[0].description = compiled


def _unique_repository_values(repository: Any, field: str) -> list[str]:
    values: list[str] = []
    for component in repository.components:
        for value in getattr(component, field, []):
            if value and value not in values:
                values.append(value)
    return values


def _task_design_defaults(task: TaskSpec) -> list[str]:
    common = [
        "Integrate with the repository's existing architecture and naming conventions instead of creating a parallel pattern.",
        "Keep the implementation bounded to the requested behavior and avoid unrelated refactors.",
        "Preserve behavior outside the explicitly requested scope unless the user states otherwise.",
    ]
    if task.requires_tests:
        common.append("Add or update focused regression coverage using the repository's existing test conventions.")

    if task.task_type in {TaskType.BUG_FIX, TaskType.RUNTIME_ERROR, TaskType.TEST_FAILURE}:
        common.extend(
            [
                "Identify and fix the underlying cause rather than masking the visible symptom.",
                "Prove the failing scenario with regression coverage when the repository supports it.",
            ]
        )
    elif task.task_type is TaskType.REFACTOR:
        common.append("Keep externally observable behavior stable while updating references and tests affected by the refactor.")
    elif task.task_type is TaskType.MIGRATION:
        common.extend(
            [
                "Use the repository's existing migration mechanism and preserve compatibility with supported application state.",
                "Provide a forward path plus rollback or an explicitly safe non-reversible strategy; do not invent destructive data policy.",
            ]
        )
    elif task.task_type is TaskType.PERFORMANCE:
        common.append("Preserve functional behavior while improving the requested performance concern; do not invent an unrequested numeric target.")

    lowered = " ".join(
        criterion.description for criterion in task.acceptance_criteria if criterion.source is AcceptanceSource.USER
    ).lower()
    if any(term in lowered for term in ("auth", "login", "oauth", "token", "credential", "api key", "secret")):
        common.extend(
            [
                "Use the repository's existing configuration and secret-handling mechanisms; never hardcode credentials.",
                "Do not invent authorization roles, OAuth scopes, account-linking policy, or other security/product decisions absent from the user request.",
            ]
        )
    return _dedupe(common)


def _compile_repository_aware_brief(task: TaskSpec, repository: Any) -> None:
    """Turn user intent into a richer engineering brief without changing user-owned criteria.

    This brief is supplied to every later DevAgent role through TaskSpec.goal. It may
    add safe engineering defaults and repository facts, but it explicitly does not
    create new user/business requirements. AcceptanceSource.USER criteria remain the
    authoritative statement of what the user asked for.
    """

    if "DEVAGENT REQUIREMENT INTELLIGENCE" in task.goal:
        return

    core_goal = task.goal.strip()
    user_requirements = [
        criterion.description
        for criterion in task.acceptance_criteria
        if criterion.source is AcceptanceSource.USER
    ]
    languages = _unique_repository_values(repository, "languages")
    frameworks = _unique_repository_values(repository, "frameworks")
    manifests = _unique_repository_values(repository, "manifests")
    test_locations = _unique_repository_values(repository, "test_locations")

    trusted_commands: list[str] = []
    for capability in repository.capabilities:
        if capability.trusted:
            command = " ".join(capability.command)
            if command and command not in trusted_commands:
                trusted_commands.append(command)

    lines = [
        core_goal,
        "",
        "DEVAGENT REQUIREMENT INTELLIGENCE",
        "User intent remains authoritative; the sections below are engineering design guidance, not invented business requirements.",
        "",
        "USER REQUIREMENTS",
    ]
    lines.extend(f"- {item}" for item in user_requirements or [core_goal])

    lines.extend(["", "SAFE ENGINEERING DEFAULTS"])
    lines.extend(f"- {item}" for item in _task_design_defaults(task))

    repository_lines: list[str] = []
    if languages:
        repository_lines.append("Languages: " + ", ".join(languages[:8]))
    if frameworks:
        repository_lines.append("Frameworks: " + ", ".join(frameworks[:8]))
    if manifests:
        repository_lines.append("Manifests: " + ", ".join(manifests[:10]))
    if test_locations:
        repository_lines.append("Existing test locations: " + ", ".join(test_locations[:10]))
    if trusted_commands:
        repository_lines.append("Evidence-backed verification: " + "; ".join(trusted_commands[:8]))
    if len(repository.components) > 1:
        repository_lines.append(f"Repository structure: {repository.kind} with {len(repository.components)} discovered components")

    lines.extend(["", "REPOSITORY-DERIVED CONTEXT"])
    lines.extend(f"- {item}" for item in repository_lines or ["Use discovered repository structure and conventions as implementation evidence."])

    lines.extend(
        [
            "",
            "DESIGN GUARDRAIL",
            "- Do not invent pricing, retry counts, authorization policy, destructive data behavior, external API semantics, or other product/business rules that the user did not request.",
            "- If source evidence shows a material ambiguity, prefer a bounded implementation or BLOCKED/PARTIALLY_VERIFIED outcome over silently choosing product policy.",
        ]
    )
    task.goal = "\n".join(lines)


def enrich_acceptance_contract(task: TaskSpec, repository: Any) -> TaskSpec:
    """Compile repository-aware requirement intelligence and trusted final checks."""

    _matrix_operation_contract(task, repository)
    seen_commands: set[tuple[str, ...]] = set()
    for capability in repository.capabilities:
        if not capability.trusted:
            continue
        if not (capability.broad or capability.kind in {"build", "lint", "typecheck", "integration"}):
            continue
        if capability.command in seen_commands:
            continue
        seen_commands.add(capability.command)
        _append_criterion(
            task.acceptance_criteria,
            f"Repository-supported {capability.kind} check passes on the final revision",
            source=AcceptanceSource.REPOSITORY,
            verification_command=capability.command,
        )

    _compile_repository_aware_brief(task, repository)
    return task
