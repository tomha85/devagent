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

    def prepare(
        self,
        argv: Sequence[str],
        *,
        network: NetworkMode | None = None,
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

        # The host filesystem is visible read-only while the selected repository and
        # explicit per-run state directories are rebound writable. This is important
        # when DevAgent executes in a retained external worktree while artifacts/HOME
        # remain under the source repository. Network is removed unless explicitly
        # inherited for a trusted verification/install phase.
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
        ]
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
