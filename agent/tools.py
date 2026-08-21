import fnmatch
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class ToolError(RuntimeError):
    """Raised when a workspace tool request is unsafe or invalid."""


class WorkspaceTools:
    """Bounded tools for one local application repository."""

    SKIP_DIRS = {".git", ".devagent", ".venv", "venv", "node_modules", "__pycache__"}
    SENSITIVE_NAMES = {
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "secrets.json",
    }

    BLOCKED_COMMAND_SNIPPETS = (
        "sudo ",
        "rm ",
        "rm\t",
        "git commit",
        "git push",
        "git reset",
        "git clean",
        "git checkout",
        "git switch",
        "git restore",
        "git rebase",
        "git merge",
        "git cherry-pick",
        "git tag",
        "mkfs",
        "shutdown",
        "reboot",
        "poweroff",
        "curl ",
        "wget ",
        "scp ",
        "ssh ",
        "rsync ",
        "nc ",
        "netcat ",
        "pip install",
        "pip3 install",
        "npm install",
        "npm i ",
        "yarn add",
        "pnpm add",
        "sed -i",
        "perl -i",
        " > ",
        ">>",
    )

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ToolError(f"Workspace is not a directory: {self.root}")

        self.state_dir = self.root / ".devagent"
        self.backup_dir = self.state_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ToolError(f"Path escapes workspace: {relative_path}") from exc
        return path

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _is_skipped(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True
        return any(part in self.SKIP_DIRS for part in relative.parts)

    def _is_sensitive(self, path: Path) -> bool:
        name = path.name.lower()
        if name in self.SENSITIVE_NAMES:
            return True
        if name.startswith(".env."):
            return True
        return any(token in name for token in ("secret", "private_key", "apikey"))

    def _require_safe_file(self, path: Path) -> None:
        if self._is_sensitive(path):
            raise ToolError(f"Access to sensitive file is blocked: {self._relative(path)}")

    def list_files(self, path: str = ".", pattern: str = "*", limit: int = 250) -> str:
        base = self._resolve(path)
        if not base.exists():
            raise ToolError(f"Path does not exist: {path}")

        candidates = [base] if base.is_file() else base.rglob("*")
        results = []
        for candidate in candidates:
            if len(results) >= limit:
                break
            if not candidate.is_file() or self._is_skipped(candidate):
                continue
            if self._is_sensitive(candidate):
                continue
            relative = self._relative(candidate)
            if fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(relative, pattern):
                results.append(relative)

        suffix = f"\n... limited to {limit} files" if len(results) >= limit else ""
        return "\n".join(results) + suffix if results else "(no matching files)"

    def read_file(self, path: str, max_chars: int = 60000) -> str:
        target = self._resolve(path)
        self._require_safe_file(target)
        if not target.is_file():
            raise ToolError(f"File does not exist: {path}")
        if target.stat().st_size > 2_000_000:
            raise ToolError(f"File is too large to read safely: {path}")

        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not UTF-8 text: {path}") from exc

        if len(content) > max_chars:
            return content[:max_chars] + f"\n... truncated at {max_chars} characters"
        return content

    def search_text(
        self,
        query: str,
        path: str = ".",
        limit: int = 100,
    ) -> str:
        if not query:
            raise ToolError("search_text requires a non-empty query")
        base = self._resolve(path)
        candidates = [base] if base.is_file() else base.rglob("*")
        matches = []

        for candidate in candidates:
            if len(matches) >= limit:
                break
            if not candidate.is_file() or self._is_skipped(candidate):
                continue
            if self._is_sensitive(candidate) or candidate.stat().st_size > 2_000_000:
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if query.lower() in line.lower():
                    matches.append(f"{self._relative(candidate)}:{line_no}: {line[:500]}")
                    if len(matches) >= limit:
                        break

        return "\n".join(matches) if matches else "(no matches)"

    def _backup(self, target: Path) -> Optional[str]:
        if not target.exists():
            return None
        if not target.is_file():
            raise ToolError(f"Cannot back up non-file path: {self._relative(target)}")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        relative = target.relative_to(self.root)
        destination = self.backup_dir / timestamp / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
        return destination.relative_to(self.root).as_posix()

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        self._require_safe_file(target)
        if target.exists() and not target.is_file():
            raise ToolError(f"Target is not a file: {path}")

        backup = self._backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if backup:
            return f"wrote {path}; backup={backup}"
        return f"created {path}; no previous file to back up"

    def replace_text(
        self,
        path: str,
        old: str,
        new: str,
        count: int = 1,
    ) -> str:
        if not old:
            raise ToolError("replace_text requires non-empty old text")
        target = self._resolve(path)
        self._require_safe_file(target)
        if not target.is_file():
            raise ToolError(f"File does not exist: {path}")

        source = target.read_text(encoding="utf-8")
        occurrences = source.count(old)
        if occurrences == 0:
            raise ToolError(f"Exact old text was not found in {path}")
        if count < 1:
            raise ToolError("replace_text count must be >= 1")
        if occurrences < count:
            raise ToolError(
                f"Requested {count} replacements but only {occurrences} occurrence(s) exist"
            )

        backup = self._backup(target)
        target.write_text(source.replace(old, new, count), encoding="utf-8")
        return f"updated {path}; replacements={count}; backup={backup}"

    def run_command(self, command: str, timeout: int = 120) -> str:
        normalized = " ".join(command.strip().lower().split())
        if not normalized:
            raise ToolError("run_command requires a command")
        if "../" in command:
            raise ToolError("Commands containing parent-directory traversal are blocked")
        if any(snippet in normalized for snippet in self.BLOCKED_COMMAND_SNIPPETS):
            raise ToolError(f"Blocked unsafe or mutating command: {command}")
        if any(secret in normalized for secret in (".env", "id_rsa", "id_ed25519")):
            raise ToolError("Commands referencing sensitive files are blocked")

        timeout = max(1, min(int(timeout), 600))
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"Command timed out after {timeout}s: {command}") from exc

        stdout = completed.stdout[-12000:]
        stderr = completed.stderr[-12000:]
        return (
            f"exit_code={completed.returncode}\n"
            f"stdout:\n{stdout or '(empty)'}\n"
            f"stderr:\n{stderr or '(empty)'}"
        )
