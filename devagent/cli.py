from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Sequence

from devagent import __version__
from devagent.config import (
    ROLE_NAMES,
    ProviderConfig,
    config_path,
    load_config,
    load_role_configs,
    provider_defaults,
    save_config,
    save_role_config,
)
from devagent.models import Outcome, SourceControlResult, jsonable
from devagent.providers import ProviderError, create_provider
from devagent.routing import create_routed_provider, routing_lines
from devagent.safety import is_secret_path
from devagent.source_control import PublicationPlan, prepare_publication, publish_verified_branch
from devagent.technical_review import analyze_developer_review


_MAX_INPUT_BYTES = 2_000_000
_PROVIDER_CHOICES = ("openai", "anthropic", "claude", "xai", "grok", "gemini", "google", "compatible")
_LIVE_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "const": True}},
    "required": ["ok"],
    "additionalProperties": False,
}

_PROGRESS_STAGES: dict[str, tuple[int, str]] = {
    "DISCOVER": (1, "DISCOVER / UNDERSTAND"),
    "UNDERSTAND": (1, "DISCOVER / UNDERSTAND"),
    "TASK_SPEC": (2, "REQUIREMENTS / PLAN"),
    "BASELINE": (2, "REQUIREMENTS / PLAN"),
    "PLAN": (2, "REQUIREMENTS / PLAN"),
    "GATHER_CONTEXT": (2, "REQUIREMENTS / PLAN"),
    "REPRODUCE": (2, "REQUIREMENTS / PLAN"),
    "IMPLEMENT": (3, "IMPLEMENT"),
    "VERIFY_TARGETED": (4, "VERIFY / REPAIR IF NEEDED"),
    "VERIFY_BROAD": (4, "VERIFY / REPAIR IF NEEDED"),
    "REVIEW": (5, "INDEPENDENT REVIEW"),
    "QUALITY_CHECK": (6, "FINAL VERIFICATION"),
    "FINAL_VERIFY": (6, "FINAL VERIFICATION"),
}
_STATE_LINE = re.compile(r"^\[([A-Z_]+)\]$")


class _ProgressStatus:
    """Render stable user-facing milestones while keeping full diagnostics opt-in."""

    def __init__(
        self,
        sink: Callable[[str], None] = print,
        *,
        verbose: bool = False,
    ) -> None:
        self.sink = sink
        self.verbose = verbose
        self._last_stage = 0
        self._plan_seen = False
        self._implement_seen = False
        self._review_seen = False

    def __call__(self, message: str) -> None:
        if self.verbose:
            self.sink(message)
            return

        match = _STATE_LINE.fullmatch(message)
        if match is None:
            return
        state = match.group(1)

        if state == "DIAGNOSE":
            self.sink("      ↳ DIAGNOSE")
            return
        if state == "PLAN":
            if self._plan_seen:
                self.sink("      ↳ REPLAN")
                return
            self._plan_seen = True
        if state == "IMPLEMENT":
            if self._implement_seen:
                label = "APPLY REVIEW FIXES" if self._review_seen else "APPLY CORRECTION"
                self.sink(f"      ↳ {label}")
                return
            self._implement_seen = True
        if state == "REVIEW":
            self._review_seen = True

        stage = _PROGRESS_STAGES.get(state)
        if stage is None:
            return
        number, label = stage
        if number <= self._last_stage:
            return
        self._last_stage = number
        self.sink(f"[{number}/7] {label}")

    def report(self) -> None:
        if self.verbose:
            self.sink("[ENGINEERING_REPORT]")
            return
        if self._last_stage < 7:
            self._last_stage = 7
            self.sink("[7/7] ENGINEERING REPORT")


def _top_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devagent", description="Evidence-driven local software engineering agent")
    parser.add_argument("--version", action="version", version=f"DevAgent {__version__}")
    parser.add_argument("task", nargs="?", help="Engineering requirement; reads stdin or prompts when omitted")
    parser.add_argument("--repo", "-r", type=Path, default=Path.cwd(), help="Target repository (default: current directory)")
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        metavar="PATH",
        help="Read the requirement from any local UTF-8 text file path; extension is unrestricted",
    )
    parser.add_argument("--provider", choices=_PROVIDER_CHOICES)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show internal state transitions and diagnostics instead of concise progress stages",
    )
    parser.add_argument("--no-isolation", action="store_true", help="Work in place instead of creating a local detached worktree")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Explicitly request the default automatic VERIFIED-branch publication behavior",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Do not commit or push after the engineering report",
    )
    parser.add_argument(
        "--publish-branch",
        help="Explicitly start a new branch for the VERIFIED result",
    )
    parser.add_argument(
        "--publish-remote",
        default="origin",
        help="Git remote used for automatic branch publication (default: origin)",
    )
    return parser


def _setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent setup",
        description="Configure the default model provider or one engineering role",
    )
    parser.add_argument("--role", choices=ROLE_NAMES, help="Optional engineering role to configure")
    parser.add_argument("--provider", choices=_PROVIDER_CHOICES, default="openai")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    return parser


def _defaults(provider: str) -> tuple[str, str]:
    return provider_defaults(provider)


def _setup(argv: Sequence[str]) -> int:
    args = _setup_parser().parse_args(argv)
    model, key_env = _defaults(args.provider)
    config = ProviderConfig(
        args.provider,
        args.model or model,
        args.base_url,
        args.api_key_env or key_env,
    )
    if args.role:
        target = save_role_config(args.role, config)
        print(f"Configured {args.role} role with {args.provider} in {target}")
    else:
        target = save_config(config)
        print(f"Configured {args.provider} in {target}")
    print(f"API keys are not stored; set {args.api_key_env or key_env} in your environment.")
    return 0


def _sdk_available(config: ProviderConfig) -> bool:
    package = "anthropic" if config.provider in {"anthropic", "claude"} else "openai"
    return importlib.util.find_spec(package) is not None


def _api_key_available(config: ProviderConfig) -> bool:
    return config.provider == "compatible" or not config.api_key_env or bool(os.getenv(config.api_key_env))


def _live_probe(label: str, config: ProviderConfig) -> bool:
    try:
        response = create_provider(config).request(
            role="doctor",
            payload={
                "instruction": "Return ok=true. This is a DevAgent structured-output readiness probe."
            },
            schema=_LIVE_PROBE_SCHEMA,
        )
    except ProviderError as exc:
        print(f"LIVE FAIL  {label} {config.provider}/{config.model}: {exc}")
        return False
    okay = response.get("ok") is True
    print(
        f"{'LIVE PASS' if okay else 'LIVE FAIL'}  {label} "
        f"{config.provider}/{config.model}"
    )
    return okay


def _doctor(*, live: bool = False) -> int:
    config = load_config()
    role_configs = load_role_configs()
    checks = {
        "git": shutil.which("git") is not None,
        "configuration": config_path().is_file(),
        "provider_sdk": _sdk_available(config),
        "api_key": _api_key_available(config),
    }
    print("DEVAGENT DOCTOR")
    all_ok = True
    for name, okay in checks.items():
        print(f"{'OK' if okay else 'WARN'}  {name}")
        all_ok = all_ok and okay
    role_readiness: dict[str, bool] = {}
    for role in ROLE_NAMES:
        role_config = role_configs.get(role)
        if role_config is None:
            continue
        okay = _sdk_available(role_config) and _api_key_available(role_config)
        role_readiness[role] = okay
        all_ok = all_ok and okay
        print(
            f"{'OK' if okay else 'WARN'}  role:{role} "
            f"{role_config.provider}/{role_config.model}"
        )
    if not checks["configuration"]:
        print("Run `devagent setup` before a cloud-provider engineering run.")
    if live:
        if checks["provider_sdk"] and checks["api_key"]:
            all_ok = _live_probe("default", config) and all_ok
        else:
            print("LIVE SKIP  default static readiness failed")
            all_ok = False
        for role in ROLE_NAMES:
            role_config = role_configs.get(role)
            if role_config is None:
                continue
            if role_readiness.get(role, False):
                all_ok = _live_probe(f"role:{role}", role_config) and all_ok
            else:
                print(f"LIVE SKIP  role:{role} static readiness failed")
                all_ok = False
    return 0 if all_ok else 1


def _models() -> int:
    default = load_config()
    roles = load_role_configs()
    print("DEVAGENT MODEL ROUTING")
    for line in routing_lines(default, roles):
        print(line)
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


def _read_requirement_file(path: Path) -> str:
    """Read a bounded textual requirement from any local path, regardless of extension."""

    candidate = path.expanduser()
    resolved = candidate.resolve(strict=False)
    if is_secret_path(candidate) or is_secret_path(resolved):
        raise ValueError("Refusing to read a sensitive input file")
    if not resolved.is_file():
        raise ValueError(f"Input path is not a readable file: {candidate}")

    try:
        with resolved.open("rb") as handle:
            content = handle.read(_MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Could not read input file: {candidate}: {exc}") from exc

    if len(content) > _MAX_INPUT_BYTES:
        raise ValueError(f"Input text file exceeds {_MAX_INPUT_BYTES} bytes: {candidate}")
    if b"\x00" in content:
        raise ValueError(f"Input file appears to be binary, not text: {candidate}")
    try:
        return content.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(f"Input file must contain readable UTF-8 text: {candidate}") from exc


def _requirement(args: argparse.Namespace) -> str:
    if args.input:
        return _read_requirement_file(args.input)
    if args.task:
        return args.task.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read(2_000_001).strip()
    return input("Engineering requirement: ").strip()


def _publication_receipt(source: SourceControlResult) -> str:
    if source.pushed:
        status = "PUSHED"
    elif source.error:
        status = "NOT PUBLISHED"
    else:
        status = "SKIPPED"
    lines = [
        "SOURCE CONTROL PUBLICATION RECEIPT",
        f"Status: {status}",
        f"Remote: {source.remote or '(none)'}",
        f"Branch: {source.branch or '(none)'}",
        f"Commit: {source.commit or 'NOT CREATED'}",
        f"Committed: {'YES' if source.committed else 'NO'}",
        f"Pushed: {'YES' if source.pushed else 'NO'}",
        "Pull request: NOT CREATED",
        "Merge: NOT PERFORMED",
    ]
    if source.error:
        lines.append(f"Publication error: {source.error}")
    return "\n".join(lines)


def _run(argv: Sequence[str]) -> int:
    args = _top_parser().parse_args(argv)
    try:
        requirement = _requirement(args)
        if not requirement:
            raise ValueError("Engineering requirement cannot be empty")
        if not args.repo.resolve().is_dir():
            raise ValueError(f"Repository does not exist: {args.repo}")
        if args.no_publish and (args.publish or args.publish_branch):
            raise ValueError("--no-publish cannot be combined with --publish or --publish-branch")

        publish_requested = not args.no_publish
        if publish_requested and args.no_isolation:
            raise ValueError("Automatic branch publishing requires isolation; use isolation or add --no-publish")

        publication_plan: PublicationPlan | None = None
        if publish_requested:
            publication_plan = prepare_publication(
                args.repo,
                explicit_branch=args.publish_branch,
                remote=args.publish_remote,
            )

        configured = load_config()
        role_configs = load_role_configs()
        selected_provider = args.provider or configured.provider
        if args.provider and args.provider != configured.provider:
            default_model, default_key_env = _defaults(args.provider)
        else:
            default_model, default_key_env = configured.model, configured.api_key_env
        inherited_base_url = (
            configured.base_url
            if not args.provider or args.provider == configured.provider
            else None
        )
        config = ProviderConfig(
            provider=selected_provider,
            model=args.model or default_model,
            base_url=args.base_url if args.base_url is not None else inherited_base_url,
            api_key_env=default_key_env,
            timeout_seconds=configured.timeout_seconds,
        )
        explicit_run_model = bool(args.provider or args.model or args.base_url)
        if explicit_run_model:
            model_provider = create_provider(config)
        else:
            model_provider = create_routed_provider(
                config,
                role_configs,
                provider_factory=create_provider,
            )
            if role_configs:
                print("Model routing:")
                for line in routing_lines(config, role_configs):
                    print(f"  {line}")
        print("DevAgent is working...")
        from devagent.orchestrator import DevAgent
        from devagent.report import recommendations_for, render_report

        progress = _ProgressStatus(print, verbose=args.verbose)
        result = DevAgent(
            model_provider,
            isolate=not args.no_isolation,
            # Internal status events are always emitted. The progress reporter keeps normal
            # CLI output concise and passes the full state/diagnostic stream only in --verbose.
            verbose=True,
            status=progress,
            base_commit=publication_plan.base_commit if publication_plan else None,
        ).run(args.repo, requirement)

        result.developer_review = analyze_developer_review(result.working_root, result.changes.paths)

        target_branch = (
            args.publish_branch
            or (publication_plan.branch if publication_plan else None)
            or f"devagent/{result.run_id}"
        )
        if publish_requested:
            result.source_control = SourceControlResult(
                requested=True,
                remote=args.publish_remote,
                branch=target_branch,
            )

        # The full engineering report is intentionally emitted before any Git commit/push.
        result.recommendations = recommendations_for(result)
        progress.report()
        print(render_report(result))

        if publish_requested:
            print("\nEngineering report complete. Starting deterministic branch publication...")
            result.source_control = publish_verified_branch(
                result,
                branch=target_branch,
                remote=args.publish_remote,
                mode=publication_plan.mode if publication_plan else "new",
                expected_remote_head=(
                    publication_plan.expected_remote_head if publication_plan else None
                ),
            )
            result.recommendations = recommendations_for(result)
            print(_publication_receipt(result.source_control))

        # Persist the final machine-readable state after publication so the report records
        # the exact branch, commit SHA, push result, and any source-control failure.
        report_path = Path(result.run_dir) / "report.json"
        report_path.write_text(json.dumps(jsonable(result), indent=2) + "\n", encoding="utf-8")

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
        parser = argparse.ArgumentParser(
            prog="devagent doctor",
            description="Check local DevAgent readiness; --live performs real structured provider probes",
        )
        parser.add_argument(
            "--live",
            action="store_true",
            help="Send one minimal structured-output probe to the default and configured role models",
        )
        return _doctor(live=parser.parse_args(arguments[1:]).live)
    if arguments and arguments[0] == "models":
        argparse.ArgumentParser(prog="devagent models", description="Show configured model routing").parse_args(arguments[1:])
        return _models()
    if arguments and arguments[0] == "status":
        parser = argparse.ArgumentParser(prog="devagent status", description="Show the latest local run")
        parser.add_argument("--repo", "-r", type=Path, default=Path.cwd())
        return _status(parser.parse_args(arguments[1:]).repo)
    if arguments and arguments[0] == "benchmark":
        from devagent.realworld import main as benchmark_main

        return benchmark_main(arguments[1:])
    return _run(arguments)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
