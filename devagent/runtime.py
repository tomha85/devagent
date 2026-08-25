from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence


class RuntimePolicyError(RuntimeError):
    """Raised when the requested runtime isolation cannot be satisfied safely."""


class SandboxMode(str, Enum):
    AUTO = "auto"
    REQUIRED = "required"
    OFF = "off"


class NetworkMode(str, Enum):
    DENY = "deny"
    INHERIT = "inherit"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RuntimePolicy:
    """Host/runtime boundary for commands executed during an engineering run.

    AUTO uses bubblewrap on Linux when available and otherwise falls back to the
    existing token/environment policy. REQUIRED fails closed when bubblewrap is
    unavailable. OFF is intended only for trusted development environments.
    """

    sandbox: SandboxMode = SandboxMode.AUTO
    network: NetworkMode = NetworkMode.DENY
    allow_dependency_install: bool = False

    @classmethod
    def from_environment(cls) -> "RuntimePolicy":
        sandbox_text = os.getenv("DEVAGENT_SANDBOX", SandboxMode.AUTO.value).strip().lower()
        network_text = os.getenv("DEVAGENT_NETWORK", NetworkMode.DENY.value).strip().lower()
        try:
            sandbox = SandboxMode(sandbox_text)
        except ValueError as exc:
            raise RuntimePolicyError(
                "DEVAGENT_SANDBOX must be one of: auto, required, off"
            ) from exc
        try:
            network = NetworkMode(network_text)
        except ValueError as exc:
            raise RuntimePolicyError(
                "DEVAGENT_NETWORK must be one of: deny, inherit"
            ) from exc
        return cls(
            sandbox=sandbox,
            network=network,
            allow_dependency_install=_env_flag("DEVAGENT_ALLOW_DEPENDENCY_INSTALL"),
        )


class RuntimeExecutor:
    """Prepare deterministic subprocess argv for the configured runtime boundary."""

    def __init__(self, root: Path | str, policy: RuntimePolicy) -> None:
        self.root = Path(root).resolve()
        self.policy = policy
        self._bwrap = shutil.which("bwrap") if sys.platform.startswith("linux") else None

    @property
    def backend(self) -> str:
        if self.policy.sandbox is SandboxMode.OFF:
            return "host-policy"
        if self._bwrap:
            return "linux-bwrap"
        if self.policy.sandbox is SandboxMode.REQUIRED:
            return "unavailable"
        return "host-policy"

    @property
    def os_sandboxed(self) -> bool:
        return self.backend == "linux-bwrap"

    def status(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "os_sandboxed": self.os_sandboxed,
            "sandbox_mode": self.policy.sandbox.value,
            "network": self.policy.network.value,
            "dependency_install": self.policy.allow_dependency_install,
        }

    def infrastructure_failure(self, stderr: str) -> str | None:
        """Translate known bwrap host-policy failures into actionable fail-closed errors."""
        if not self.os_sandboxed:
            return None
        text = stderr.strip()
        lowered = text.casefold()
        if not lowered.startswith("bwrap:"):
            return None
        if "failed rtm_newaddr" in lowered or "loopback:" in lowered:
            return (
                "Linux sandbox network isolation is blocked by the host security policy. "
                "bubblewrap could not configure loopback in its private network namespace. "
                "On Ubuntu 24.04+ this commonly requires an AppArmor profile granting "
                "`userns` to /usr/bin/bwrap. DevAgent refuses to fall back to unrestricted "
                "network access while DEVAGENT_NETWORK=deny."
            )
        if any(
            marker in lowered
            for marker in (
                "setting up uid map",
                "creating new namespace",
                "no permissions to creating new namespace",
                "operation not permitted",
                "permission denied",
            )
        ):
            return (
                "Linux sandbox initialization is blocked by the host user-namespace/security "
                "policy. Configure the host to permit bubblewrap user namespaces or use a "
                "supported sandbox host; DevAgent will not silently bypass the OS sandbox."
            )
        return None

    def _linked_git_metadata(self) -> tuple[Path, ...]:
        """Return external Git metadata required by a linked worktree/submodule.

        A normal repository has a .git directory inside the writable workspace and needs
        no carve-out. Linked worktrees and submodules have a small .git text file pointing
        outside the workspace. When /tmp is masked, that external metadata must be reopened
        read-only or even `git diff`/`git status` cannot operate.
        """
        marker = self.root / ".git"
        if not marker.is_file():
            return ()
        try:
            first_line = marker.read_text(encoding="utf-8", errors="strict").splitlines()[0]
        except (OSError, UnicodeDecodeError, IndexError):
            return ()
        prefix = "gitdir:"
        if not first_line.casefold().startswith(prefix):
            return ()
        raw = first_line[len(prefix):].strip()
        if not raw:
            return ()
        git_dir = Path(raw).expanduser()
        if not git_dir.is_absolute():
            git_dir = marker.parent / git_dir
        try:
            git_dir = git_dir.resolve(strict=True)
        except OSError:
            return ()
        if not git_dir.is_dir():
            return ()

        common_file = git_dir / "commondir"
        if common_file.is_file():
            try:
                raw_common = common_file.read_text(encoding="utf-8", errors="strict").strip()
            except (OSError, UnicodeDecodeError):
                raw_common = ""
            if raw_common:
                common = Path(raw_common).expanduser()
                if not common.is_absolute():
                    common = git_dir / common
                try:
                    common = common.resolve(strict=True)
                except OSError:
                    common = git_dir
                if common.is_dir():
                    return (common,)
        return (git_dir,)

    def prepare(
        self,
        argv: Sequence[str],
        *,
        network: NetworkMode | None = None,
        readable_paths: Sequence[Path | str] = (),
        writable_paths: Sequence[Path | str] = (),
    ) -> tuple[str, ...]:
        command = tuple(str(item) for item in argv)
        effective_network = network or self.policy.network
        if self.policy.sandbox is SandboxMode.OFF:
            return command
        if not self._bwrap:
            if self.policy.sandbox is SandboxMode.REQUIRED:
                raise RuntimePolicyError(
                    "Linux OS sandbox is required but bubblewrap (`bwrap`) is unavailable"
                )
            return command

        readable: list[Path] = list(self._linked_git_metadata())
        for item in readable_paths:
            candidate = Path(item).expanduser().resolve()
            if not candidate.exists():
                raise RuntimePolicyError(f"Sandbox readable path does not exist: {candidate}")
            if candidate == self.root or candidate.is_relative_to(self.root):
                continue
            if candidate not in readable:
                readable.append(candidate)

        # The selected repository/worktree is writable. Explicit per-run state directories
        # may also be writable. Readable carve-outs (notably external Git common metadata)
        # are rebound read-only first so later writable child binds win. /tmp is private
        # tmpfs: normal toolchain temp files work while host temp state remains hidden.
        # bubblewrap resolves bind sources from the old root, so paths physically below
        # host /tmp can be reopened after the tmpfs mask.
        writable: list[Path] = [self.root]
        for item in writable_paths:
            candidate = Path(item).expanduser().resolve()
            if not candidate.exists():
                raise RuntimePolicyError(f"Sandbox writable path does not exist: {candidate}")
            if candidate == self.root or candidate.is_relative_to(self.root):
                continue
            if candidate not in writable:
                writable.append(candidate)

        wrapped: list[str] = [
            self._bwrap,
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
        ]
        for candidate in readable:
            wrapped.extend(("--ro-bind", str(candidate), str(candidate)))
        for candidate in writable:
            wrapped.extend(("--bind", str(candidate), str(candidate)))
        wrapped.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                str(self.root),
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
            )
        )
        if effective_network is NetworkMode.DENY:
            wrapped.append("--unshare-net")
        wrapped.extend(("--", *command))
        return tuple(wrapped)
