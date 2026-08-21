from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from agent.loop import run_bugfix_loop

app = typer.Typer(no_args_is_help=True, help="Local AI development agent")
console = Console()


def _execute_agent(
    *,
    repo: Path,
    task: str,
    max_steps: int,
    provider: Optional[str],
) -> None:
    console.print(f"[bold green]Repository:[/bold green] {repo}")
    console.print(f"[bold]Task:[/bold] {task}\n")

    try:
        result = run_bugfix_loop(
            repo_path=str(repo),
            task=task,
            max_steps=max_steps,
            provider=provider,
        )
    except Exception as exc:  # CLI boundary: show a clean error to the user.
        console.print(f"[bold red]DevAgent failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print("\n[bold]Result:[/bold]")
    console.print(result)


def _repo_option() -> Path:
    return typer.Option(  # type: ignore[return-value]
        ...,
        "--repo",
        "-r",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Path to the application repository",
    )


def _task_option() -> str:
    return typer.Option(  # type: ignore[return-value]
        ...,
        "--task",
        "-t",
        help="Bug or feature request",
    )


def _provider_option() -> Optional[str]:
    return typer.Option(  # type: ignore[return-value]
        None,
        "--provider",
        "-p",
        help="openai / claude / grok",
    )


@app.command()
def fix(
    repo: Path = _repo_option(),
    task: str = _task_option(),
    max_steps: int = typer.Option(8, "--max-steps", min=1, max=50),
    provider: Optional[str] = _provider_option(),
) -> None:
    """Fix a bug or implement a feature in a local repository."""
    _execute_agent(repo=repo, task=task, max_steps=max_steps, provider=provider)


@app.command()
def run(
    repo: Path = _repo_option(),
    task: str = _task_option(),
    max_steps: int = typer.Option(8, "--max-steps", min=1, max=50),
    provider: Optional[str] = _provider_option(),
) -> None:
    """Compatibility alias for `fix`."""
    _execute_agent(repo=repo, task=task, max_steps=max_steps, provider=provider)


if __name__ == "__main__":
    app()
