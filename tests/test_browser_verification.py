from __future__ import annotations

from pathlib import Path

import pytest

from devagent.browser import BrowserVerificationError, normalize_target, verify_browser


def test_browser_file_target_must_stay_inside_repository(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text("<h1>Hello</h1>\n", encoding="utf-8")

    target, localhost = normalize_target(tmp_path, "index.html")

    assert target == page.resolve().as_uri()
    assert localhost is False


def test_browser_rejects_external_web_targets(tmp_path: Path) -> None:
    with pytest.raises(BrowserVerificationError, match="localhost"):
        normalize_target(tmp_path, "https://example.com")


def test_browser_accepts_localhost_targets(tmp_path: Path) -> None:
    target, localhost = normalize_target(tmp_path, "http://127.0.0.1:4173/app")

    assert target == "http://127.0.0.1:4173/app"
    assert localhost is True


def test_browser_rejects_file_url_outside_repository(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.html"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        with pytest.raises(BrowserVerificationError, match="escapes"):
            normalize_target(tmp_path, outside.as_uri())
    finally:
        outside.unlink(missing_ok=True)


def test_offline_browser_check_fails_closed_without_os_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "index.html").write_text("<h1>offline</h1>\n", encoding="utf-8")
    monkeypatch.setattr("devagent.browser.find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    monkeypatch.setenv("DEVAGENT_NETWORK", "deny")

    with pytest.raises(BrowserVerificationError, match="unenforced network denial"):
        verify_browser(tmp_path, "index.html")


def test_localhost_browser_check_requires_explicit_network_inherit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("devagent.browser.find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setenv("DEVAGENT_SANDBOX", "off")
    monkeypatch.setenv("DEVAGENT_NETWORK", "deny")

    with pytest.raises(BrowserVerificationError, match="DEVAGENT_NETWORK=inherit"):
        verify_browser(tmp_path, "http://localhost:4173")
