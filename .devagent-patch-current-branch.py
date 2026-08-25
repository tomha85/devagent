from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(path: str, marker: str, addition: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if marker in text:
        return
    target.write_text(text.rstrip() + "\n\n" + addition.strip() + "\n", encoding="utf-8")


replace_once(
    "devagent/source_control.py",
    "import re\nimport subprocess\nfrom pathlib import Path\n",
    "import re\nimport subprocess\nfrom dataclasses import dataclass\nfrom pathlib import Path\n",
)
replace_once(
    "devagent/source_control.py",
    '_SAFE_REMOTE = re.compile(r"^[A-Za-z0-9._-]+$")\n',
    '''_SAFE_REMOTE = re.compile(r"^[A-Za-z0-9._-]+$")\n\n\n@dataclass(frozen=True)\nclass PublicationPlan:\n    """Deterministic source-control context captured before the engineering run."""\n\n    mode: str\n    remote: str\n    branch: str | None\n    base_commit: str\n    expected_remote_head: str | None\n''',
)
replace_once(
    "devagent/source_control.py",
    '''def _validate_path(path: str) -> bool:\n    candidate = Path(path)\n    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts and not is_secret_path(candidate)\n\n\n''',
    '''def _validate_path(path: str) -> bool:\n    candidate = Path(path)\n    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts and not is_secret_path(candidate)\n\n\ndef _remote_branch_head(root: Path, remote: str, branch: str) -> str | None:\n    completed = _git(\n        root,\n        "ls-remote",\n        "--exit-code",\n        "--heads",\n        remote,\n        f"refs/heads/{branch}",\n        timeout=30,\n    )\n    if completed.returncode == 2:\n        return None\n    if completed.returncode != 0:\n        raise ValueError(_failure("Could not safely inspect remote branch", completed))\n    line = next((line for line in completed.stdout.splitlines() if line.strip()), "")\n    if not line:\n        raise ValueError("Remote branch lookup succeeded without returning a commit")\n    return line.split()[0]\n\n\ndef _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:\n    completed = _git(root, "merge-base", "--is-ancestor", ancestor, descendant, timeout=10)\n    if completed.returncode == 0:\n        return True\n    if completed.returncode == 1:\n        return False\n    raise ValueError(_failure("Could not compare local and remote branch history", completed))\n\n\ndef prepare_publication(\n    root: Path | str,\n    *,\n    explicit_branch: str | None = None,\n    remote: str = "origin",\n) -> PublicationPlan:\n    """Resolve the safe publication target and exact worktree base before model execution."""\n\n    repository_root = Path(root).expanduser().resolve()\n    if not repository_root.is_dir():\n        raise ValueError(f"Repository does not exist: {repository_root}")\n    if not _SAFE_REMOTE.fullmatch(remote):\n        raise ValueError("Remote name contains unsupported characters")\n\n    local_head_result = _git(repository_root, "rev-parse", "HEAD", timeout=10)\n    if local_head_result.returncode != 0:\n        raise ValueError(_failure("Could not resolve local Git HEAD", local_head_result))\n    local_head = local_head_result.stdout.strip()\n\n    remote_check = _git(repository_root, "remote", "get-url", remote, timeout=10)\n    if remote_check.returncode != 0:\n        raise ValueError(_failure(f"Git remote {remote!r} is unavailable", remote_check))\n\n    current_result = _git(repository_root, "branch", "--show-current", timeout=10)\n    current_branch = current_result.stdout.strip() if current_result.returncode == 0 else ""\n\n    if explicit_branch:\n        branch_check = _git(repository_root, "check-ref-format", "--branch", explicit_branch, timeout=10)\n        if branch_check.returncode != 0:\n            raise ValueError(_failure("Invalid branch name", branch_check))\n        if explicit_branch.lower() in _PROTECTED_BRANCHES:\n            raise ValueError(f"Refusing to publish directly to protected branch: {explicit_branch}")\n        return PublicationPlan(\n            mode="new",\n            remote=remote,\n            branch=explicit_branch,\n            base_commit=local_head,\n            expected_remote_head=None,\n        )\n\n    if not current_branch or current_branch.lower() in _PROTECTED_BRANCHES:\n        return PublicationPlan(\n            mode="new",\n            remote=remote,\n            branch=None,\n            base_commit=local_head,\n            expected_remote_head=None,\n        )\n\n    branch_check = _git(repository_root, "check-ref-format", "--branch", current_branch, timeout=10)\n    if branch_check.returncode != 0:\n        raise ValueError(_failure("Invalid current branch name", branch_check))\n\n    remote_head = _remote_branch_head(repository_root, remote, current_branch)\n    base_commit = local_head\n    if remote_head is not None:\n        fetch = _git(\n            repository_root,\n            "fetch",\n            "--no-tags",\n            remote,\n            f"refs/heads/{current_branch}",\n            timeout=60,\n        )\n        if fetch.returncode != 0:\n            raise ValueError(_failure("Could not fetch current development branch", fetch))\n        fetched = _git(repository_root, "rev-parse", "FETCH_HEAD", timeout=10)\n        if fetched.returncode != 0 or fetched.stdout.strip() != remote_head:\n            raise ValueError("Fetched development branch does not match the inspected remote HEAD")\n\n        if local_head == remote_head:\n            base_commit = local_head\n        elif _is_ancestor(repository_root, local_head, remote_head):\n            base_commit = remote_head\n        elif _is_ancestor(repository_root, remote_head, local_head):\n            base_commit = local_head\n        else:\n            raise ValueError(\n                f"Local and remote branch histories diverged for {current_branch}; "\n                "resolve the branch manually before running DevAgent"\n            )\n\n    return PublicationPlan(\n        mode="continue",\n        remote=remote,\n        branch=current_branch,\n        base_commit=base_commit,\n        expected_remote_head=remote_head,\n    )\n\n\n''',
)

source_control = Path("devagent/source_control.py")
source_text = source_control.read_text(encoding="utf-8")
prefix, separator, _old_function = source_text.partition("def publish_verified_branch(")
if not separator:
    raise RuntimeError("devagent/source_control.py: publish_verified_branch not found")
new_function = '''def publish_verified_branch(\n    result: RunResult,\n    *,\n    branch: str | None = None,\n    remote: str = "origin",\n    mode: str = "new",\n    expected_remote_head: str | None = None,\n) -> SourceControlResult:\n    """Commit and push a VERIFIED result without PR, merge, rebase, or force push."""\n\n    target_branch = branch or f"devagent/{result.run_id}"\n    publication = SourceControlResult(requested=True, remote=remote, branch=target_branch)\n\n    if result.outcome is not Outcome.VERIFIED:\n        publication.error = "Branch publishing is allowed only for VERIFIED runs"\n        return publication\n    if mode not in {"new", "continue"}:\n        publication.error = f"Unsupported publication mode: {mode}"\n        return publication\n\n    source_root = Path(result.repository.root).expanduser().resolve()\n    working_root = Path(result.working_root).expanduser().resolve()\n    if working_root == source_root:\n        publication.error = "Branch publishing requires the default isolated worktree; remove --no-isolation"\n        return publication\n    if not working_root.is_dir():\n        publication.error = f"Working root does not exist: {working_root}"\n        return publication\n\n    if not _SAFE_REMOTE.fullmatch(remote):\n        publication.error = "Remote name contains unsupported characters"\n        return publication\n    if target_branch.lower() in _PROTECTED_BRANCHES:\n        publication.error = f"Refusing to publish directly to protected branch: {target_branch}"\n        return publication\n    if not result.changes.paths:\n        publication.error = "No reviewed file changes are available to commit"\n        return publication\n    invalid_paths = [path for path in result.changes.paths if not _validate_path(path)]\n    if invalid_paths:\n        publication.error = f"Refusing to stage unsafe path: {invalid_paths[0]}"\n        return publication\n\n    branch_check = _git(working_root, "check-ref-format", "--branch", target_branch, timeout=10)\n    if branch_check.returncode != 0:\n        publication.error = _failure("Invalid branch name", branch_check)\n        return publication\n\n    remote_check = _git(working_root, "remote", "get-url", remote, timeout=10)\n    if remote_check.returncode != 0:\n        publication.error = _failure(f"Git remote {remote!r} is unavailable", remote_check)\n        return publication\n\n    if mode == "new":\n        local_check = _git(working_root, "show-ref", "--verify", "--quiet", f"refs/heads/{target_branch}", timeout=10)\n        if local_check.returncode == 0:\n            publication.error = f"Local branch already exists: {target_branch}"\n            return publication\n        try:\n            remote_head = _remote_branch_head(working_root, remote, target_branch)\n        except ValueError as exc:\n            publication.error = str(exc)\n            return publication\n        if remote_head is not None:\n            publication.error = f"Remote branch already exists: {remote}/{target_branch}"\n            return publication\n        switch = _git(working_root, "switch", "-c", target_branch, timeout=20)\n        if switch.returncode != 0:\n            publication.error = _failure("Could not create publication branch", switch)\n            return publication\n    else:\n        if result.repository.git_branch != target_branch:\n            publication.error = (\n                "Continuation target must match the developer's current local branch: "\n                f"{result.repository.git_branch or '(detached)'} != {target_branch}"\n            )\n            return publication\n        try:\n            remote_head = _remote_branch_head(working_root, remote, target_branch)\n        except ValueError as exc:\n            publication.error = str(exc)\n            return publication\n        if remote_head != expected_remote_head:\n            publication.error = (\n                f"Remote branch changed during run: expected {expected_remote_head or '(absent)'}, "\n                f"found {remote_head or '(absent)'}"\n            )\n            return publication\n\n    stage = _git(working_root, "add", "--", *result.changes.paths, timeout=20)\n    if stage.returncode != 0:\n        publication.error = _failure("Could not stage reviewed changes", stage)\n        return publication\n\n    staged = _git(working_root, "diff", "--cached", "--quiet", "--", timeout=20)\n    if staged.returncode == 0:\n        publication.error = "No staged changes remained after verification"\n        return publication\n    if staged.returncode != 1:\n        publication.error = _failure("Could not inspect staged changes", staged)\n        return publication\n\n    message = f"DevAgent: {result.task.goal.strip()}"\n    if len(message) > 120:\n        message = message[:117].rstrip() + "..."\n    commit = _git(working_root, "commit", "-m", message, timeout=30)\n    if commit.returncode != 0:\n        publication.error = _failure("Git commit failed", commit)\n        return publication\n\n    head = _git(working_root, "rev-parse", "HEAD", timeout=10)\n    if head.returncode != 0:\n        publication.error = _failure("Could not resolve created commit", head)\n        return publication\n    publication.committed = True\n    publication.commit = head.stdout.strip()\n\n    if mode == "continue":\n        try:\n            remote_head = _remote_branch_head(working_root, remote, target_branch)\n        except ValueError as exc:\n            publication.error = str(exc)\n            return publication\n        if remote_head != expected_remote_head:\n            publication.error = (\n                f"Remote branch changed before push: expected {expected_remote_head or '(absent)'}, "\n                f"found {remote_head or '(absent)'}"\n            )\n            return publication\n        push = _git(working_root, "push", remote, f"HEAD:refs/heads/{target_branch}")\n    else:\n        push = _git(working_root, "push", "--set-upstream", remote, target_branch)\n    if push.returncode != 0:\n        publication.error = _failure("Git push failed", push)\n        return publication\n\n    publication.pushed = True\n    return publication\n'''
source_control.write_text(prefix + new_function, encoding="utf-8")

replace_once(
    "devagent/orchestrator.py",
    '''    def __init__(self, provider: ModelProvider, *, max_corrections: int = 2, isolate: bool = True, verbose: bool = False, status: Callable[[str], None] | None = None) -> None:\n        self.provider = provider\n        self.max_corrections = max(0, min(max_corrections, 5))\n        self.isolate = isolate\n        self.verbose = verbose\n        self.status = status or (lambda message: None)\n''',
    '''    def __init__(\n        self,\n        provider: ModelProvider,\n        *,\n        max_corrections: int = 2,\n        isolate: bool = True,\n        verbose: bool = False,\n        status: Callable[[str], None] | None = None,\n        base_commit: str | None = None,\n    ) -> None:\n        self.provider = provider\n        self.max_corrections = max(0, min(max_corrections, 5))\n        self.isolate = isolate\n        self.verbose = verbose\n        self.status = status or (lambda message: None)\n        self.base_commit = base_commit\n''',
)
replace_once(
    "devagent/orchestrator.py",
    '''        source_repository = discover_repository(root, probe_capabilities=False)\n        selection = select_worktree(root, artifacts.run_id, enabled=self.isolate, git_head=source_repository.git_head, dirty_files=source_repository.dirty_files)\n        working_root = selection.root\n''',
    '''        source_repository = discover_repository(root, probe_capabilities=False)\n        effective_head = self.base_commit or source_repository.git_head\n        selection = select_worktree(\n            root,\n            artifacts.run_id,\n            enabled=self.isolate,\n            git_head=effective_head,\n            dirty_files=source_repository.dirty_files,\n        )\n        working_root = selection.root\n''',
)
replace_once(
    "devagent/orchestrator.py",
    "        repository.git_head = source_repository.git_head\n",
    "        repository.git_head = effective_head\n",
)

replace_once(
    "devagent/cli.py",
    "from devagent.source_control import publish_verified_branch\n",
    "from devagent.source_control import PublicationPlan, prepare_publication, publish_verified_branch\n",
)
replace_once(
    "devagent/cli.py",
    '''    parser.add_argument(\n        "--publish-branch",\n        help="New branch name to commit and push after a VERIFIED report",\n    )\n''',
    '''    parser.add_argument(\n        "--publish-branch",\n        help="Explicitly start a new branch for the VERIFIED result",\n    )\n''',
)
replace_once(
    "devagent/cli.py",
    '''        publish_requested = not args.no_publish\n        if publish_requested and args.no_isolation:\n            raise ValueError("Automatic branch publishing requires isolation; use isolation or add --no-publish")\n\n        configured = load_config()\n''',
    '''        publish_requested = not args.no_publish\n        if publish_requested and args.no_isolation:\n            raise ValueError("Automatic branch publishing requires isolation; use isolation or add --no-publish")\n\n        publication_plan: PublicationPlan | None = None\n        if publish_requested:\n            publication_plan = prepare_publication(\n                args.repo,\n                explicit_branch=args.publish_branch,\n                remote=args.publish_remote,\n            )\n\n        configured = load_config()\n''',
)
replace_once(
    "devagent/cli.py",
    '''        result = DevAgent(\n            create_provider(config),\n            isolate=not args.no_isolation,\n            verbose=args.verbose,\n            status=print,\n        ).run(args.repo, requirement)\n''',
    '''        result = DevAgent(\n            create_provider(config),\n            isolate=not args.no_isolation,\n            verbose=args.verbose,\n            status=print,\n            base_commit=publication_plan.base_commit if publication_plan else None,\n        ).run(args.repo, requirement)\n''',
)
replace_once(
    "devagent/cli.py",
    '''        target_branch = args.publish_branch or f"devagent/{result.run_id}"\n        if publish_requested:\n            result.source_control = SourceControlResult(\n                requested=True,\n                remote=args.publish_remote,\n                branch=target_branch,\n            )\n''',
    '''        target_branch = (\n            args.publish_branch\n            or (publication_plan.branch if publication_plan else None)\n            or f"devagent/{result.run_id}"\n        )\n        if publish_requested:\n            result.source_control = SourceControlResult(\n                requested=True,\n                remote=args.publish_remote,\n                branch=target_branch,\n            )\n''',
)
replace_once(
    "devagent/cli.py",
    '''            result.source_control = publish_verified_branch(\n                result,\n                branch=target_branch,\n                remote=args.publish_remote,\n            )\n''',
    '''            result.source_control = publish_verified_branch(\n                result,\n                branch=target_branch,\n                remote=args.publish_remote,\n                mode=publication_plan.mode if publication_plan else "new",\n                expected_remote_head=(\n                    publication_plan.expected_remote_head if publication_plan else None\n                ),\n            )\n''',
)

replace_once(
    "tests/test_source_control_publish.py",
    "from devagent.source_control import publish_verified_branch\n",
    "from devagent.source_control import prepare_publication, publish_verified_branch\n",
)
append_once(
    "tests/test_source_control_publish.py",
    "def test_prepare_publication_continues_current_development_branch",
    r'''
def test_prepare_publication_continues_current_development_branch(tmp_path: Path) -> None:
    source, _working, _remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0

    plan = prepare_publication(source)

    assert plan.mode == "continue"
    assert plan.branch == "feature/calculator"
    assert plan.base_commit == baseline
    assert plan.expected_remote_head == baseline


def test_prepare_publication_uses_remote_head_when_devagent_branch_is_ahead(tmp_path: Path) -> None:
    source, _working, remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0

    other = tmp_path / "other"
    cloned = subprocess.run(
        ["git", "clone", "--branch", "feature/calculator", str(remote), str(other)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0
    assert _git(other, "config", "user.name", "Other Developer").returncode == 0
    assert _git(other, "config", "user.email", "other@example.com").returncode == 0
    (other / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    assert _git(other, "add", "calculator.py").returncode == 0
    assert _git(other, "commit", "-m", "previous DevAgent result").returncode == 0
    assert _git(other, "push", "origin", "feature/calculator").returncode == 0
    remote_head = _git(other, "rev-parse", "HEAD").stdout.strip()
    assert remote_head != baseline

    plan = prepare_publication(source)

    assert plan.mode == "continue"
    assert plan.branch == "feature/calculator"
    assert plan.base_commit == remote_head
    assert plan.expected_remote_head == remote_head
    assert _git(source, "rev-parse", "HEAD").stdout.strip() == baseline


def test_continue_mode_fast_forward_pushes_same_branch(tmp_path: Path) -> None:
    source, working, remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0
    (working / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    result = _result(source, working)
    result.repository.git_branch = "feature/calculator"
    result.repository.git_head = baseline

    publication = publish_verified_branch(
        result,
        branch="feature/calculator",
        mode="continue",
        expected_remote_head=baseline,
    )

    assert publication.pushed is True
    assert publication.committed is True
    assert publication.error is None
    remote_head = _git(remote, "rev-parse", "refs/heads/feature/calculator").stdout.strip()
    assert remote_head == publication.commit
    parent = _git(remote, "rev-parse", f"{remote_head}^").stdout.strip()
    assert parent == baseline


def test_continue_mode_blocks_when_remote_moves_during_run(tmp_path: Path) -> None:
    source, working, remote = _repo_with_bare_remote(tmp_path)
    assert _git(source, "switch", "-c", "feature/calculator").returncode == 0
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    assert _git(source, "push", "-u", "origin", "feature/calculator").returncode == 0
    (working / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\ndef multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    result = _result(source, working)
    result.repository.git_branch = "feature/calculator"
    result.repository.git_head = baseline

    (source / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n\n# concurrent change\n",
        encoding="utf-8",
    )
    assert _git(source, "add", "calculator.py").returncode == 0
    assert _git(source, "commit", "-m", "concurrent update").returncode == 0
    assert _git(source, "push", "origin", "feature/calculator").returncode == 0
    moved = _git(remote, "rev-parse", "refs/heads/feature/calculator").stdout.strip()
    assert moved != baseline

    publication = publish_verified_branch(
        result,
        branch="feature/calculator",
        mode="continue",
        expected_remote_head=baseline,
    )

    assert publication.pushed is False
    assert publication.committed is False
    assert publication.error is not None
    assert "Remote branch changed during run" in publication.error
''',
)

replace_once(
    "tests/test_cli.py",
    ")\n\n\ndef test_version",
    ")\nfrom devagent.source_control import PublicationPlan\n\n\ndef test_version",
)
replace_once(
    "tests/test_cli.py",
    '''    monkeypatch.setattr("devagent.cli.create_provider", lambda _config: object())\n    monkeypatch.setattr(\n        "devagent.cli.analyze_developer_review",\n''',
    '''    monkeypatch.setattr("devagent.cli.create_provider", lambda _config: object())\n    monkeypatch.setattr(\n        "devagent.cli.prepare_publication",\n        lambda _repo, *, explicit_branch=None, remote="origin": PublicationPlan(\n            mode="new",\n            remote=remote,\n            branch=explicit_branch,\n            base_commit="baseline",\n            expected_remote_head=None,\n        ),\n    )\n    monkeypatch.setattr(\n        "devagent.cli.analyze_developer_review",\n''',
)
replace_once(
    "tests/test_cli.py",
    '''    def fake_publish(\n        _result: RunResult,\n        *,\n        branch: str | None = None,\n        remote: str = "origin",\n    ) -> SourceControlResult:\n''',
    '''    def fake_publish(\n        _result: RunResult,\n        *,\n        branch: str | None = None,\n        remote: str = "origin",\n        mode: str = "new",\n        expected_remote_head: str | None = None,\n    ) -> SourceControlResult:\n''',
)
append_once(
    "tests/test_cli.py",
    "def test_current_local_development_branch_is_continued_by_default",
    r'''
def test_current_local_development_branch_is_continued_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    repo, result = _patch_engineering_run(tmp_path, monkeypatch, events)
    result.repository.git_branch = "feature/calculator"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "devagent.cli.prepare_publication",
        lambda _repo, *, explicit_branch=None, remote="origin": PublicationPlan(
            mode="continue",
            remote=remote,
            branch="feature/calculator",
            base_commit="remote123",
            expected_remote_head="remote123",
        ),
    )

    def fake_publish(
        _result: RunResult,
        *,
        branch: str | None = None,
        remote: str = "origin",
        mode: str = "new",
        expected_remote_head: str | None = None,
    ) -> SourceControlResult:
        events.append("publish")
        captured.update(
            branch=branch,
            remote=remote,
            mode=mode,
            expected_remote_head=expected_remote_head,
        )
        return SourceControlResult(
            requested=True,
            remote=remote,
            branch=branch,
            commit="next456",
            committed=True,
            pushed=True,
        )

    monkeypatch.setattr("devagent.cli.publish_verified_branch", fake_publish)

    assert main(["--repo", str(repo), "Add addition support"]) == 0

    output = capsys.readouterr().out
    assert events == ["run", "report", "publish"]
    assert captured == {
        "branch": "feature/calculator",
        "remote": "origin",
        "mode": "continue",
        "expected_remote_head": "remote123",
    }
    assert "Branch: feature/calculator" in output
    assert "Status: PUSHED" in output
''',
)

replace_once(
    "README.md",
    "**A local, evidence-driven software engineering agent that turns a requirement into a tested, independently reviewed patch, prints a developer-grade engineering report, then automatically commits and pushes a `VERIFIED` result to a new branch — without creating a PR or merging code.**",
    "**A local, evidence-driven software engineering agent that turns a requirement into a tested, independently reviewed patch, prints a developer-grade engineering report, then automatically commits and fast-forward pushes a `VERIFIED` result to the developer's current local branch — creating a new safe branch only when starting from `main`, `master`, or `trunk` — without creating a PR or merging code.**",
)
replace_once(
    "README.md",
    "- **Bounded automatic publishing** — after the report, only a `VERIFIED` result may be committed and pushed, only to a new non-protected branch, and only from the isolated worktree.",
    "- **Bounded automatic publishing** — after the report, only a `VERIFIED` result may be committed and fast-forward pushed from the isolated worktree; normal development branches continue in place, while protected branches cause DevAgent to create a new safe branch.",
)
replace_once(
    "README.md",
    '''create a new DevAgent branch\n        ↓\ncommit only reviewed changed paths\n        ↓\npush the new branch to origin\n''',
    '''continue the current local development branch\n(or create a new DevAgent branch from main/master/trunk)\n        ↓\ncommit only reviewed changed paths\n        ↓\nfast-forward push that branch to origin\n''',
)
replace_once(
    "README.md",
    '''The default branch is unique per run, for example:\n\n```text\ndevagent/20260825T020000Z-ab12cd\n```\n\nTo choose the new branch name explicitly:\n''',
    '''By default, DevAgent treats the developer's current local Git branch as the working branch. Repeated prompts continue that same non-protected branch. If the developer is on `main`, `master`, or `trunk`, DevAgent creates a unique safe branch such as:\n\n```text\ndevagent/20260825T020000Z-ab12cd\n```\n\nTo explicitly start a new branch instead of continuing the current development branch:\n''',
)
replace_once(
    "README.md",
    '''- the target branch must be new and cannot be `main`, `master`, or `trunk`,\n- only reviewed changed paths are staged,\n- one commit is created and pushed to the selected remote branch,\n''',
    '''- `main`, `master`, and `trunk` are never publication targets; DevAgent creates a new safe branch when started there,\n- an existing branch may be continued only when it is the developer's current local non-protected branch and its remote history is compatible,\n- remote branch state is captured before model execution and checked again before publication,\n- only reviewed changed paths are staged,\n- one commit is created and pushed with normal fast-forward Git semantics; no force push is used,\n''',
)
replace_once(
    "README.md",
    "IF VERIFIED: COMMIT + PUSH NEW BRANCH",
    "IF VERIFIED: COMMIT + FAST-FORWARD PUSH WORKING BRANCH",
)

replace_once(
    "CHANGELOG.md",
    "- Added automatic post-report branch publication for `VERIFIED` runs: DevAgent prints the full engineering review report first, then deterministic harness code commits and pushes the verified change to a new branch.",
    "- Added automatic post-report branch publication for `VERIFIED` runs: DevAgent prints the full engineering review report first, then deterministic harness code commits and pushes the verified change. The current local non-protected development branch is continued by default; `main`, `master`, and `trunk` still cause creation of a new safe branch.",
)
replace_once(
    "CHANGELOG.md",
    "- Added `--no-publish` for developers who want a local review-only run; `--publish-branch` can select the new target branch explicitly.",
    "- Added `--no-publish` for developers who want a local review-only run; `--publish-branch` explicitly starts a new target branch instead of continuing the current development branch.",
)
replace_once(
    "CHANGELOG.md",
    "- Publishing requires the default isolated worktree, refuses protected branches such as `main`, `master`, and `trunk`, refuses existing remote branches, stages only reviewed changed paths, creates one commit, and pushes to the selected remote branch.",
    "- Publishing requires the default isolated worktree, refuses direct publication to `main`, `master`, and `trunk`, supports safe continuation of the current local development branch, stages only reviewed changed paths, creates one commit, re-checks the expected remote HEAD, and uses normal fast-forward push semantics.",
)
replace_once(
    "SECURITY.md",
    "- publication to protected or pre-existing remote branches,",
    "- publication to protected, unexpected, or diverged remote branches,",
)
replace_once(
    "SECURITY.md",
    "Engineering/model-facing command execution continues to block Git write operations. For a normal isolated run, DevAgent prints the complete engineering review report first. Only after that report is emitted may the separate deterministic publication path commit and push a `VERIFIED` result to a new non-protected branch. The publication path stages only reviewed changed paths and refuses `main`, `master`, `trunk`, or an already-existing target branch. Developers can disable publication with `--no-publish`.",
    "Engineering/model-facing command execution continues to block Git write operations. For a normal isolated run, DevAgent prints the complete engineering review report first. Only after that report is emitted may the separate deterministic publication path commit and push a `VERIFIED` result. A current local non-protected development branch may be continued, but its remote HEAD is captured before model execution and checked again before publication; diverged or unexpectedly moved branches are blocked. When the developer is on `main`, `master`, or `trunk`, DevAgent creates a new safe branch instead of publishing to the protected branch. The publication path stages only reviewed changed paths and uses normal fast-forward push semantics without force push. Developers can disable publication with `--no-publish`.",
)
replace_once(
    "CONTRIBUTING.md",
    "6. Keep commit/push publication post-report, post-`VERIFIED`, deterministic, isolated-worktree-only, limited to a new non-protected branch, and outside model-facing command execution. Preserve `--no-publish` as an explicit review-only escape hatch. Never add automatic PR, merge, rebase, force-push, or deploy behavior.",
    "6. Keep commit/push publication post-report, post-`VERIFIED`, deterministic, isolated-worktree-only, and outside model-facing command execution. Current local non-protected development branches may continue only with remote-head/concurrency checks and normal fast-forward push semantics; protected branches must create a new safe branch. Preserve `--no-publish` as an explicit review-only escape hatch. Never add automatic PR, merge, rebase, force-push, or deploy behavior.",
)
replace_once(
    "CONTRIBUTING.md",
    "Branch-publishing tests should use local bare Git repositories where possible and must prove that protected/non-`VERIFIED` publication attempts are rejected. CLI tests should also prove that the engineering report is emitted before any commit/push action.",
    "Branch-publishing tests should use local bare Git repositories where possible and must prove that protected/non-`VERIFIED` publication attempts are rejected, current development branches continue by fast-forward only, and remote movement/divergence is blocked. CLI tests should also prove that the engineering report is emitted before any commit/push action.",
)

print("Current-local-branch continuation patch applied.")
