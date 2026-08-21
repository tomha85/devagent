import json
from typing import Any, Dict, List, Optional, Tuple

from agent.llm import LLMClient
from agent.memory import AgentMemory
from agent.prompts import SYSTEM_PROMPT, build_step_prompt
from agent.tools import ToolError, WorkspaceTools


VERIFICATION_MARKERS = (
    "pytest",
    "unittest",
    "py_compile",
    "compileall",
    "ruff",
    "mypy",
    "npm test",
    "npm run test",
    "npm run build",
    "pnpm test",
    "pnpm run test",
    "pnpm run build",
    "yarn test",
    "yarn build",
    "go test",
    "cargo test",
    "cargo check",
    "ctest",
    "make test",
    "gradlew test",
    "mvn test",
    "dotnet test",
    "git diff --check",
)


def _parse_action(raw: str) -> Dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM response did not contain a JSON object")
        parsed = json.loads(text[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("LLM action must be a JSON object")
    if not isinstance(parsed.get("action"), str):
        raise ValueError("LLM action is missing string field 'action'")
    return parsed


def _is_verification_command(command: str) -> bool:
    normalized = " ".join(command.lower().split())
    return any(marker in normalized for marker in VERIFICATION_MARKERS)


def _command_succeeded(result: str) -> bool:
    return result.startswith("exit_code=0\n")


def _repository_context(tools: WorkspaceTools) -> str:
    files = tools.list_files(".", "*", limit=180)
    try:
        git_status = tools.run_command("git status --short", timeout=20)
    except ToolError as exc:
        git_status = f"git status unavailable: {exc}"
    return f"FILES\n{files}\n\nGIT STATUS\n{git_status}"


def _execute_action(
    tools: WorkspaceTools,
    action: Dict[str, Any],
) -> Tuple[str, bool, bool]:
    """Return (result, modified, successful_verification)."""
    name = action["action"].strip().lower()

    if name == "list_files":
        result = tools.list_files(
            path=str(action.get("path", ".")),
            pattern=str(action.get("pattern", "*")),
        )
        return result, False, False

    if name == "read_file":
        return tools.read_file(str(action["path"])), False, False

    if name == "search_text":
        result = tools.search_text(
            query=str(action["query"]),
            path=str(action.get("path", ".")),
        )
        return result, False, False

    if name == "write_file":
        result = tools.write_file(
            path=str(action["path"]),
            content=str(action["content"]),
        )
        return result, True, False

    if name == "replace_text":
        result = tools.replace_text(
            path=str(action["path"]),
            old=str(action["old"]),
            new=str(action["new"]),
            count=int(action.get("count", 1)),
        )
        return result, True, False

    if name == "run_command":
        command = str(action["command"])
        result = tools.run_command(
            command=command,
            timeout=int(action.get("timeout", 120)),
        )
        verified = _is_verification_command(command) and _command_succeeded(result)
        return result, False, verified

    raise ToolError(f"Unknown action: {name}")


def _trim_observations(observations: List[str], max_chars: int = 30000) -> str:
    text = "\n\n".join(observations)
    if len(text) <= max_chars:
        return text
    return "... earlier observations trimmed ...\n\n" + text[-max_chars:]


def run_bugfix_loop(
    repo_path: str,
    task: str,
    max_steps: int = 8,
    provider: Optional[str] = None,
) -> str:
    """Run a bounded Gather -> Act -> Verify loop against a local repository."""
    task = task.strip()
    if not task:
        raise ValueError("Task cannot be empty")
    if max_steps < 1:
        raise ValueError("max_steps must be at least 1")

    tools = WorkspaceTools(repo_path)
    memory = AgentMemory(repo_path)
    llm = LLMClient(provider=provider)

    repository_context = _repository_context(tools)
    memory_context = memory.context()
    observations: List[str] = []
    modified = False
    verified_after_modification = False
    last_verification = ""

    for step in range(1, max_steps + 1):
        prompt = build_step_prompt(
            task=task,
            step=step,
            max_steps=max_steps,
            repository_context=repository_context,
            recent_memory=memory_context,
            observations=_trim_observations(observations),
            modified=modified,
            verified_after_modification=verified_after_modification,
        )

        raw = llm.complete(system=SYSTEM_PROMPT, user=prompt)
        try:
            action = _parse_action(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            result = f"Invalid LLM action: {exc}. Return one supported JSON action only."
            observations.append(f"STEP {step}\n{result}")
            continue

        action_name = action["action"].strip().lower()
        if action_name == "finish":
            if modified and not verified_after_modification:
                result = (
                    "Finish rejected: code changed since the last successful verification. "
                    "Run a relevant test/build/lint/compile command first."
                )
                observations.append(f"STEP {step} ACTION={action_name}\n{result}")
                memory.record_attempt(task=task, step=step, action=action, result=result)
                continue

            summary = str(action.get("summary", "Task completed.")).strip()
            if modified and last_verification:
                summary = f"{summary}\n\nVerification:\n{last_verification[:4000]}"
            memory.record_attempt(task=task, step=step, action=action, result=summary)
            memory.record_lesson(task, summary)
            return summary

        try:
            result, changed_this_step, verified_this_step = _execute_action(tools, action)
        except (ToolError, KeyError, TypeError, ValueError) as exc:
            result = f"Tool error: {exc}"
            changed_this_step = False
            verified_this_step = False

        if changed_this_step:
            modified = True
            verified_after_modification = False
            repository_context = _repository_context(tools)

        if verified_this_step:
            verified_after_modification = True
            last_verification = result

        observations.append(
            f"STEP {step} ACTION={json.dumps(action, ensure_ascii=False)}\nRESULT\n{result}"
        )
        memory.record_attempt(task=task, step=step, action=action, result=result)

    state = "modified but not verified" if modified and not verified_after_modification else "incomplete"
    return (
        f"DevAgent reached max_steps={max_steps} ({state}). "
        "Review the latest tool output in .devagent/memory/attempts.jsonl and rerun with a larger step budget if appropriate."
    )
