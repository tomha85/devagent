from __future__ import annotations

import fnmatch
import os
import shlex
from pathlib import Path
from typing import Sequence


class SafetyError(RuntimeError):
    """Raised when a requested local operation crosses a safety boundary."""


SKIP_DIRECTORIES = frozenset(
    {
        ".git", ".devagent", ".venv", "venv", "node_modules", "dist", "build",
        "target", "__pycache__", "vendor", "coverage", "htmlcov", ".tox", ".nox",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", "site-packages",
    }
)

_SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "credentials*",
    "secrets*",
    "*private_key*",
    "*apikey*",
)


def is_secret_path(path: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in path.parts)
    if ".ssh" in lowered_parts or ".aws" in lowered_parts:
        return True
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_PATTERNS)


class PathPolicy:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise SafetyError(f"Workspace is not a directory: {self.root}")

    def resolve(self, relative: str, *, allow_missing: bool = True) -> Path:
        if not relative or "\x00" in relative:
            raise SafetyError("Path must be a non-empty text path")
        candidate = (self.root / relative).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SafetyError(f"Path escapes workspace: {relative}") from exc
        if is_secret_path(candidate.relative_to(self.root)):
            raise SafetyError(f"Access to sensitive file is blocked: {relative}")
        if not allow_missing and not candidate.exists():
            raise SafetyError(f"Path does not exist: {relative}")
        return candidate


class CommandPolicy:
    """Token-aware policy for non-shell subprocess execution."""

    _BLOCKED_PROGRAMS = {
        "sudo",
        "su",
        "rm",
        "rmdir",
        "mkfs",
        "shutdown",
        "reboot",
        "poweroff",
        "curl",
        "wget",
        "scp",
        "ssh",
        "sftp",
        "rsync",
        "nc",
        "netcat",
        "busybox",
        "env",
    }
    _READ_ONLY_GIT = {"diff", "status", "rev-parse", "log", "show", "ls-files", "grep"}
    _SHELL_PROGRAMS = {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"}

    @classmethod
    def parse(cls, command: str | Sequence[str]) -> tuple[str, ...]:
        if isinstance(command, str):
            try:
                tokens = tuple(shlex.split(command, posix=os.name != "nt"))
            except ValueError as exc:
                raise SafetyError(f"Invalid command quoting: {exc}") from exc
        else:
            tokens = tuple(str(item) for item in command)
        if not tokens or any(not token or "\x00" in token for token in tokens):
            raise SafetyError("Command must contain non-empty arguments")
        return tokens

    @classmethod
    def is_dependency_install(cls, command: str | Sequence[str]) -> bool:
        tokens = cls.parse(command)
        executable = Path(tokens[0]).name.lower()
        lowered = tuple(token.lower() for token in tokens)
        if executable in {"pip", "pip3"}:
            return len(lowered) > 1 and lowered[1] == "install"
        if executable in {"python", "python3"}:
            return len(lowered) > 3 and lowered[1:4] == ("-m", "pip", "install")
        if executable == "npm":
            return len(lowered) > 1 and lowered[1] in {"ci", "install", "i"}
        if executable == "pnpm":
            return len(lowered) > 1 and lowered[1] in {"install", "i", "add"}
        if executable == "yarn":
            return len(lowered) > 1 and lowered[1] in {"install", "add"}
        return False

    @classmethod
    def _safe_dependency_install(cls, tokens: tuple[str, ...]) -> tuple[str, ...]:
        executable = Path(tokens[0]).name.lower()
        lowered = tuple(token.lower() for token in tokens)
        if executable in {"python", "python3"}:
            prefix = tokens[:4]
            args = list(tokens[4:])
            kind = "pip"
        elif executable in {"pip", "pip3"}:
            prefix = tokens[:2]
            args = list(tokens[2:])
            kind = "pip"
        else:
            prefix = tokens[:2]
            args = list(tokens[2:])
            kind = executable

        forbidden_flags = {
            "-e", "--editable", "--index-url", "--extra-index-url", "--trusted-host",
            "--find-links", "--registry", "--global", "-g", "--no-binary",
        }
        for token in args:
            normalized = token.lower()
            if normalized in forbidden_flags or normalized.startswith(
                (
                    "--index-url=", "--extra-index-url=", "--trusted-host=", "--find-links=",
                    "--registry=", "--no-binary=",
                )
            ):
                raise SafetyError(f"Dependency install option is not allowed: {token}")
            if normalized.startswith(("http://", "https://", "ftp://", "git+", "ssh://")):
                raise SafetyError("Direct dependency URLs are blocked")

        if kind == "pip":
            # Keep the caller-controlled surface deliberately tiny: exactly one repository
            # requirements file. DevAgent appends all safety flags itself, so arbitrary
            # requirement specifiers or user-supplied pip options cannot bypass the file.
            if len(args) != 2 or args[0].lower() not in {"-r", "--requirement"}:
                raise SafetyError(
                    "Safe pip installation accepts only -r/--requirement with one repository file"
                )
            requirement = args[1]
            if requirement.startswith("-") or is_secret_path(Path(requirement)):
                raise SafetyError("Safe pip installation requires a non-sensitive requirement file")
            return (
                *prefix,
                args[0],
                requirement,
                "--no-input",
                "--disable-pip-version-check",
                "--only-binary=:all:",
            )

        if kind == "npm":
            if lowered[1] != "ci":
                raise SafetyError("Safe npm installation requires `npm ci`, not a lockfile-mutating install")
            normalized = list(prefix) + args
            if "--ignore-scripts" not in lowered:
                normalized.append("--ignore-scripts")
            return tuple(normalized)

        if kind == "pnpm":
            if lowered[1] not in {"install", "i"}:
                raise SafetyError("Safe pnpm installation does not allow adding arbitrary packages")
            normalized = list(prefix) + args
            if "--frozen-lockfile" not in lowered:
                normalized.append("--frozen-lockfile")
            if "--ignore-scripts" not in lowered:
                normalized.append("--ignore-scripts")
            return tuple(normalized)

        if kind == "yarn":
            if lowered[1] != "install":
                raise SafetyError("Safe yarn installation does not allow adding arbitrary packages")
            normalized = list(prefix) + args
            if "--frozen-lockfile" not in lowered and "--immutable" not in lowered:
                normalized.append("--frozen-lockfile")
            # Script suppression is injected through the environment in Workspace.run.
            # Yarn Classic supports YARN_IGNORE_SCRIPTS; Yarn Modern maps
            # YARN_ENABLE_SCRIPTS to enableScripts and rejects --ignore-scripts.
            return tuple(normalized)

        raise SafetyError("Unsupported dependency installer")

    @classmethod
    def validate(
        cls,
        command: str | Sequence[str],
        *,
        allow_dependency_install: bool = False,
    ) -> tuple[str, ...]:
        tokens = cls.parse(command)
        executable = Path(tokens[0]).name.lower()
        lowered = tuple(token.lower() for token in tokens)
        if executable in cls._BLOCKED_PROGRAMS or executable in cls._SHELL_PROGRAMS:
            raise SafetyError(f"Blocked command program: {executable}")
        if executable == "git" and (len(lowered) < 2 or lowered[1] not in cls._READ_ONLY_GIT):
            operation = lowered[1] if len(lowered) > 1 else "(missing)"
            raise SafetyError(f"Only explicit read-only Git operations are allowed; blocked: git {operation}")
        if executable in {"python", "python3", "node", "ruby", "perl"} and any(
            token in {"-c", "-e", "--eval"} for token in lowered[1:]
        ):
            raise SafetyError("Inline interpreter execution is blocked")

        # pip is never a general-purpose verification tool. Only the explicitly gated
        # `install -r <repo file>` form may pass through the dependency-install path.
        if executable in {"pip", "pip3"} and (len(lowered) < 2 or lowered[1] != "install"):
            raise SafetyError("Non-install pip commands are blocked during an engineering run")
        if executable in {"python", "python3"} and len(lowered) >= 3 and lowered[1:3] == ("-m", "pip"):
            if len(lowered) < 4 or lowered[3] != "install":
                raise SafetyError("Non-install pip commands are blocked during an engineering run")

        dependency_install = cls.is_dependency_install(tokens)
        if dependency_install:
            if not allow_dependency_install:
                raise SafetyError("Package installation is blocked during an engineering run")
            tokens = cls._safe_dependency_install(tokens)

        for token in tokens:
            lowered_token = token.lower()
            if is_secret_path(Path(lowered_token)):
                raise SafetyError("Command references a sensitive path")
            if lowered_token.startswith(("http://", "https://", "ftp://")):
                raise SafetyError("Network URLs are blocked in engineering commands")
            if any(operator in token for operator in ("\n", "\r", "`", "$(", ">${", "<${")):
                raise SafetyError("Shell syntax is not allowed in structured commands")
        return tokens
