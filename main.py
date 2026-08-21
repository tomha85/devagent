from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from agent.loop import run_bugfix_loop

app = typer.Typer(no_args_is_help=True, help="Local AI development agent")
console = Console()


@app.command()
def fix(
    repo: Path = typer.Option(
        ...,
        "--repo",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Path to the application repository",
    ),
    task: str = typer.Option(..., "--task", help="Bug or feature request"),
    max_steps: int = typer.Option(8, "--max-steps", min=1, max=50),
    provider: Optional[str] = typer.Option(
        None, "--provider", help="openai / claude / grok"
    ),
) -> None:
    """Fix a bug or implement a feature in a local repository."""
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


if __name__ == "__main__":
    app()
