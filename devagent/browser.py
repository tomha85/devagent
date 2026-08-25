from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlparse

from devagent.runtime import NetworkMode, RuntimeExecutor, RuntimePolicy, RuntimePolicyError
from devagent.safety import PathPolicy, SafetyError


class BrowserVerificationError(RuntimeError):
    """Raised when bounded browser verification cannot be performed safely."""


_BROWSER_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "microsoft-edge",
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def find_browser() -> str | None:
    configured = os.getenv("DEVAGENT_BROWSER")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        resolved = shutil.which(configured)
        if resolved:
            return resolved
    for name in _BROWSER_CANDIDATES:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def normalize_target(root: Path | str, target: str) -> tuple[str, bool]:
    workspace = Path(root).resolve()
    parsed = urlparse(target)
    if parsed.scheme in {"http", "https"}:
        if parsed.hostname not in _LOCAL_HOSTS:
            raise BrowserVerificationError("Browser verification only permits localhost HTTP(S) targets")
        return target, True
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path)).resolve()
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise BrowserVerificationError("Browser file target escapes the repository") from exc
        if not path.is_file():
            raise BrowserVerificationError(f"Browser file target does not exist: {path}")
        return path.as_uri(), False
    if parsed.scheme:
        raise BrowserVerificationError(f"Unsupported browser target scheme: {parsed.scheme}")

    try:
        path = PathPolicy(workspace).resolve(target, allow_missing=False)
    except SafetyError as exc:
        raise BrowserVerificationError(str(exc)) from exc
    if not path.is_file():
        raise BrowserVerificationError(f"Browser target is not a file: {target}")
    return path.as_uri(), False


def verify_browser(
    root: Path | str,
    target: str,
    *,
    timeout: int = 60,
) -> tuple[int, str, str, Path]:
    workspace = Path(root).resolve()
    browser = find_browser()
    if not browser:
        raise BrowserVerificationError(
            "No Chromium-family browser found; set DEVAGENT_BROWSER to an executable path"
        )
    normalized_target, uses_localhost = normalize_target(workspace, target)
    try:
        policy = RuntimePolicy.from_environment()
    except RuntimePolicyError as exc:
        raise BrowserVerificationError(str(exc)) from exc

    runtime = RuntimeExecutor(workspace, policy)
    if uses_localhost and policy.network is not NetworkMode.INHERIT:
        raise BrowserVerificationError(
            "Localhost browser verification requires DEVAGENT_NETWORK=inherit; browser egress remains constrained"
        )
    if not uses_localhost and policy.network is NetworkMode.DENY and not runtime.os_sandboxed:
        raise BrowserVerificationError(
            "Offline browser verification requires the Linux OS sandbox; refusing to render repository HTML "
            "with unenforced network denial"
        )

    output_dir = workspace / ".devagent" / "browser"
    profile_dir = output_dir / "profile"
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot = output_dir / "latest.png"
    # A prior successful capture must never satisfy the current verification attempt.
    screenshot.unlink(missing_ok=True)

    command: list[str] = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--metrics-recording-only",
        "--no-first-run",
        f"--user-data-dir={profile_dir}",
        "--window-size=1440,900",
        f"--screenshot={screenshot}",
    ]
    if runtime.os_sandboxed:
        # Chromium's own user-namespace sandbox can conflict with the outer bwrap
        # namespace. The outer read-only-root/write-scoped repository boundary remains.
        command.append("--no-sandbox")
    if uses_localhost:
        # Permit the local application while forcing non-local HTTP(S)/WebSocket traffic
        # to an unused loopback port. QUIC and non-proxied WebRTC UDP are disabled above.
        command.extend(
            (
                "--proxy-server=http://127.0.0.1:9",
                "--proxy-bypass-list=localhost;127.0.0.1;[::1]",
            )
        )
    command.extend(("--dump-dom", normalized_target))

    try:
        execution_argv = runtime.prepare(command)
    except RuntimePolicyError as exc:
        raise BrowserVerificationError(str(exc)) from exc

    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TERM", "SYSTEMROOT", "WINDIR"}
    }
    environment.update(
        {
            "HOME": str(profile_dir),
            "TMPDIR": str(output_dir),
            "CI": "true",
            "DEVAGENT_RUNTIME_BACKEND": runtime.backend,
            "DEVAGENT_NETWORK_MODE": policy.network.value,
        }
    )
    completed = subprocess.run(
        execution_argv,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=max(1, min(int(timeout), 300)),
        check=False,
    )
    runtime_failure = runtime.infrastructure_failure(completed.stderr) if completed.returncode != 0 else None
    if runtime_failure:
        raise BrowserVerificationError(runtime_failure)
    return completed.returncode, completed.stdout[-24_000:], completed.stderr[-24_000:], screenshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent-ui-check",
        description="Run bounded headless browser verification for a repository file or localhost URL",
    )
    parser.add_argument("target", help="Repository HTML file, file:// URL, or localhost HTTP(S) URL")
    parser.add_argument("--repo", "-r", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=int, default=60)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        exit_code, stdout, stderr, screenshot = verify_browser(args.repo, args.target, timeout=args.timeout)
    except (BrowserVerificationError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"BROWSER FAIL: {exc}")
        return 1
    if exit_code != 0 or not screenshot.is_file():
        detail = stderr.strip() or stdout.strip() or f"browser exited {exit_code}"
        print(f"BROWSER FAIL: {detail[-2000:]}")
        return 1
    print(f"BROWSER PASS: {screenshot}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
