from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from devagent import __version__
from devagent.config import ProviderConfig, config_path, load_config, save_config
from devagent.models import Outcome, jsonable
from devagent.providers import ProviderError, create_provider
from devagent.safety import is_secret_path
from devagent.source_control import publish_verified_branch


def _top_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devagent", description="Evidence-driven local software engineering agent")
    parser.add_argument("--version", action="version", version=f"DevAgent {__version__}")
    parser.add_argument("task", nargs="?", help="Engineering requirement; reads stdin or prompts when omitted")
    parser.add_argument("--repo", "-r", type=Path, default=Path.cwd(), help="Target repository (default: current directory)")
    parser.add_argument("--input", "-i", type=Path, help="Read the requirement/error context from a file")
    parser.add_argument("--provider", choices=("openai", "anthropic", "claude", "xai", "grok", "compatible"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--verbose", action="store_true", help="Show state transitions")
    parser.add_argument("--no-isolation", action="store_true", help="Work in place instead of creating a local detached worktree")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="After VERIFIED, commit and push to a new remote branch; never creates a PR or merges",
    )
    parser.add_argument(
        "--publish-branch",
        help="New branch name to commit and push after VERIFIED; implies --publish",
    )
    parser.add_argument(
        "--publish-remote",
        default="origin",
        help="Git remote used by --publish (default: origin)",
    )
    return parser


def _setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devagent setup", description="Configure the default model provider")
    parser.add_argument("--provider", choices=("openai", "anthropic", "xai", "compatible"), default="openai")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    return parser


def _defaults(provider: str) -> tuple[str, str]:
    provider = {"claude": "anthropic", "grok": "xai"}.get(provider, provider)
    return {
        "openai": ("gpt-5", "OPENAI_API_KEY"),
        "anthropic": ("claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
        "xai": ("grok-4", "XAI_API_KEY"),
        "compatible": ("local-model", "DEVAGENT_API_KEY"),
    }[provider]


def _setup(argv: Sequence[str]) -> int:
    args = _setup_parser().parse_args(argv)
    model, key_env = _defaults(args.provider)
    target = save_config(ProviderConfig(args.provider, args.model or model, args.base_url, args.api_key_env or key_env))
    print(f"Configured {args.provider} in {target}")
    print(f"API keys are not stored; set {args.api_key_env or key_env} in your environment.")
    return 0


def _doctor() -> int:
    config = load_config()
    checks = {
        "git": shutil.which("git") is not None,
        "configuration": config_path().is_file(),
        "provider_sdk": importlib.util.find_spec("anthropic" if config.provider in {"anthropic", "claude"} else "openai") is not None,
        "api_key": config.provider == "compatible" or not config.api_key_env or bool(os.getenv(config.api_key_env)),
    }
    print("DEVAGENT DOCTOR")
    for name, okay in checks.items():
        print(f"{'OK' if okay else 'WARN'}  {name}")
    if not checks["configuration"]:
        print("Run `devagent setup` before a cloud-provider engineering run.")
    return 0


def _status(repo: Path) -> int:
    runs = repo.resolve() / ".devagent" / "runs"
    reports = sorted(runs.glob("*/report.json")) if runs.is_dir() else []
    if not reports:
        print("No DevAgent runs found for this repository.")
        return 0
    latest = reports[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"Latest run report is unreadable: {latest}")
        return 1
    print(f"Run: {data.get('run_id', latest.parent.name)}")
    print(f"Status: {data.get('outcome', 'UNKNOWN')}")
    source_control = data.get("source_control") or {}
    if source_control.get("requested"):
        print(f"Branch: {source_control.get('branch') or '(none)'}")
        print(f"Pushed: {'YES' if source_control.get('pushed') else 'NO'}")
    print(f"Report: {latest}")
    return 0


def _requirement(args: argparse.Namespace) -> str:
    if args.input:
        if is_secret_path(args.input):
            raise ValueError("Refusing to read a sensitive input file")
        if not args.input.is_file() or args.input.stat().st_size > 2_000_000:
            raise ValueError("Input must be a bounded local text file")
        return args.input.read_text(encoding="utf-8").strip()
    if args.task:
        return args.task.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read(2_000_001).strip()
    return input("Engineering requirement: ").strip()


def _run(argv: Sequence[str]) -> int:
    args = _top_parser().parse_args(argv)
    try:
        requirement = _requirement(args)
        if not requirement:
            raise ValueError("Engineering requirement cannot be empty")
        if not args.repo.resolve().is_dir():
            raise ValueError(f"Repository does not exist: {args.repo}")
        if (args.publish or args.publish_branch) and args.no_isolation:
            raise ValueError("Branch publishing requires isolation; remove --no-isolation")
        configured = load_config()
        selected_provider = args.provider or configured.provider
        if args.provider and args.provider != configured.provider:
            default_model, default_key_env = _defaults(args.provider)
        else:
            default_model, default_key_env = configured.model, configured.api_key_env
        config = ProviderConfig(
            provider=selected_provider,
            model=args.model or default_model,
            base_url=args.base_url or configured.base_url,
            api_key_env=default_key_env,
            timeout_seconds=configured.timeout_seconds,
        )
        print("DevAgent is working...")
        from devagent.orchestrator import DevAgent
        from devagent.report import render_report

        result = DevAgent(
            create_provider(config),
            isolate=not args.no_isolation,
            verbose=args.verbose,
            status=print,
        ).run(args.repo, requirement)

        publish_requested = bool(args.publish or args.publish_branch)
        if publish_requested:
            result.source_control = publish_verified_branch(
                result,
                branch=args.publish_branch,
                remote=args.publish_remote,
            )
            report_path = Path(result.run_dir) / "report.json"
            report_path.write_text(json.dumps(jsonable(result), indent=2) + "\n", encoding="utf-8")

        report = render_report(result)
        print(report)
        if result.outcome is Outcome.VERIFIED and publish_requested and not result.source_control.pushed:
            return 3
        return {Outcome.VERIFIED: 0, Outcome.PARTIALLY_VERIFIED: 2, Outcome.BLOCKED: 1}[result.outcome]
    except (ValueError, OSError, ProviderError) as exc:
        print(f"DevAgent failed: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "setup":
        return _setup(arguments[1:])
    if arguments and arguments[0] == "doctor":
        argparse.ArgumentParser(prog="devagent doctor", description="Check local DevAgent readiness").parse_args(arguments[1:])
        return _doctor()
    if arguments and arguments[0] == "status":
        parser = argparse.ArgumentParser(prog="devagent status", description="Show the latest local run")
        parser.add_argument("--repo", "-r", type=Path, default=Path.cwd())
        return _status(parser.parse_args(arguments[1:]).repo)
    return _run(arguments)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
