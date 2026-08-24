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
    _INSTALLERS = {("pip", "install"), ("pip3", "install"), ("npm", "install"), ("npm", "i"), ("pnpm", "add"), ("yarn", "add")}

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
    def validate(cls, command: str | Sequence[str]) -> tuple[str, ...]:
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
        if executable in {"python", "python3"} and len(lowered) > 2 and lowered[1:3] == ("-m", "pip"):
            raise SafetyError("Package installation and pip execution are blocked during a run")
        if len(lowered) > 1 and (executable, lowered[1]) in cls._INSTALLERS:
            raise SafetyError(f"Package installation is blocked during an engineering run: {' '.join(tokens[:2])}")
        for token in tokens:
            lowered_token = token.lower()
            if is_secret_path(Path(lowered_token)):
                raise SafetyError("Command references a sensitive path")
            if lowered_token.startswith(("http://", "https://", "ftp://")):
                raise SafetyError("Network URLs are blocked in engineering commands")
            if any(operator in token for operator in ("\n", "\r", "`", "$(", ">${", "<${")):
                raise SafetyError("Shell syntax is not allowed in structured commands")
        return tokens
