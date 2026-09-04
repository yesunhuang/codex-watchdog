from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from codex_watchdog.git_adapter import LocalGitAdapter


def git(cwd: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    completed = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        env=environment,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(
    "subcommand",
    (
        "add",
        "checkout",
        "commit",
        "fetch",
        "merge",
        "pull",
        "push",
        "rebase",
        "reset",
        "switch",
        "update-ref",
    ),
)
def test_production_git_adapter_rejects_mutating_subcommands(
    tmp_path: Path, subcommand: str
) -> None:
    called = False

    def runner(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("mutating Git command reached the process runner")

    with pytest.raises(ValueError, match="not allowed"):
        LocalGitAdapter(runner=runner)._run(tmp_path, subcommand)

    assert called is False


def configure(repo: Path) -> None:
    git(repo, "config", "user.name", "Codex Watchdog Test")
    git(repo, "config", "user.email", "watchdog@example.invalid")


def commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-m", message)


def repositories(tmp_path: Path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "workspace"
    peer = tmp_path / "peer"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(seed)],
        check=True,
        capture_output=True,
        text=True,
    )
    configure(seed)
    commit_file(seed, "tracked.txt", "base\n", "base")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "clone", str(remote), str(workspace)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "clone", str(remote), str(peer)],
        check=True,
        capture_output=True,
        text=True,
    )
    configure(workspace)
    configure(peer)
    return remote, workspace, peer


def test_remote_oid_check_never_moves_head_index_worktree_or_tracking_ref(
    tmp_path: Path,
) -> None:
    _remote, workspace, peer = repositories(tmp_path)
    adapter = LocalGitAdapter()

    equal = adapter.observe(workspace)
    original_head = git(workspace, "rev-parse", "HEAD")
    original_tracking = git(workspace, "rev-parse", "refs/remotes/origin/main")
    original_index = (workspace / ".git" / "index").read_bytes()
    original_status = git(workspace, "status", "--porcelain=v2")
    commit_file(peer, "peer.txt", "remote\n", "remote")
    git(peer, "push", "origin", "main")
    remote_changed = adapter.observe(workspace)

    assert equal.status == "observed"
    assert equal.topology == "equal"
    assert remote_changed.topology == "remote_changed"
    assert git(workspace, "rev-parse", "HEAD") == original_head
    assert git(workspace, "rev-parse", "refs/remotes/origin/main") == original_tracking
    assert (workspace / ".git" / "index").read_bytes() == original_index
    assert git(workspace, "status", "--porcelain=v2") == original_status
    assert remote_changed.upstream_oid == git(peer, "rev-parse", "HEAD")


def test_local_ahead_and_diverged_are_classified_without_mutation(
    tmp_path: Path,
) -> None:
    _remote, workspace, peer = repositories(tmp_path)
    adapter = LocalGitAdapter()
    commit_file(workspace, "local.txt", "local\n", "local")

    local_ahead = adapter.observe(workspace)
    commit_file(peer, "peer.txt", "remote\n", "remote")
    git(peer, "push", "origin", "main")
    remote_changed = adapter.observe(workspace)
    git(workspace, "fetch", "origin", "main")
    diverged = adapter.observe(workspace)

    assert local_ahead.topology == "local_ahead"
    assert remote_changed.topology == "remote_changed"
    assert diverged.topology == "diverged"
    assert diverged.status == "blocked"
    assert diverged.blockers == ("diverged",)


def test_dirty_tracked_and_untracked_are_reported_separately(tmp_path: Path) -> None:
    _remote, workspace, _peer = repositories(tmp_path)
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (workspace / "untracked.secret").write_text("not staged\n", encoding="utf-8")

    observation = LocalGitAdapter().observe(workspace)

    assert observation.status == "observed"
    assert observation.dirty_tracked is True
    assert observation.untracked_present is True
    assert "untracked.secret" not in str(observation.to_dict())


def test_detached_missing_upstream_and_root_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    _remote, workspace, _peer = repositories(tmp_path)
    adapter = LocalGitAdapter()
    child = workspace / "child"
    child.mkdir()
    mismatch = adapter.observe(child)

    git(workspace, "checkout", "--detach")
    detached = adapter.observe(workspace)
    git(workspace, "switch", "main")
    git(workspace, "branch", "--unset-upstream")
    missing = adapter.observe(workspace)

    assert mismatch.blockers == ("repo_root_mismatch",)
    assert "detached_head" in detached.blockers
    assert "missing_upstream" in missing.blockers


def test_operation_and_lock_markers_block_before_remote_check(tmp_path: Path) -> None:
    _remote, workspace, _peer = repositories(tmp_path)
    git_dir = Path(git(workspace, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = workspace / git_dir
    (git_dir / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="ascii")
    (git_dir / "index.lock").write_text("", encoding="ascii")

    observation = LocalGitAdapter().observe(workspace)

    assert "merge_in_progress" in observation.blockers
    assert "index_locked" in observation.blockers
    assert observation.topology is None


def test_ls_remote_failure_is_hashed_without_retaining_remote_output(
    tmp_path: Path,
) -> None:
    _remote, workspace, _peer = repositories(tmp_path)
    missing = tmp_path / "missing-remote.git"
    git(workspace, "remote", "set-url", "origin", str(missing))

    observation = LocalGitAdapter().observe(workspace)

    assert observation.blockers == ("ls_remote_failed",)
    assert observation.error_sha256
    assert observation.error_chars > 0
    assert str(missing) not in str(observation.to_dict())


def test_late_subprocess_timeout_fails_closed(tmp_path: Path) -> None:
    _remote, workspace, _peer = repositories(tmp_path)

    def runner(command, **kwargs):
        if "--git-dir" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.run(command, **kwargs)

    observation = LocalGitAdapter(runner=runner).observe(workspace)

    assert observation.blockers == ("git_timeout",)
    assert observation.topology is None


def test_malformed_porcelain_fails_closed(tmp_path: Path) -> None:
    _remote, workspace, _peer = repositories(tmp_path)

    def runner(command, **kwargs):
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, "unexpected\0", "")
        return subprocess.run(command, **kwargs)

    observation = LocalGitAdapter(runner=runner).observe(workspace)

    assert observation.blockers == ("malformed_git_output",)
    assert observation.topology is None


def test_porcelain_v2_rename_consumes_the_second_path_record() -> None:
    dirty, untracked, conflicts = LocalGitAdapter._parse_status(
        "2 renamed record\0source-name\0"
    )

    assert dirty is True
    assert untracked is False
    assert conflicts is False
    with pytest.raises(ValueError):
        LocalGitAdapter._parse_status("2 renamed record\0")


def test_unsafe_git_environment_blocks_before_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def runner(command, **kwargs):
        nonlocal called
        called = True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "different.git"))
    observation = LocalGitAdapter(runner=runner).observe(tmp_path)

    assert observation.blockers == ("unsafe_git_environment",)
    assert called is False


def test_git_subprocess_is_noninteractive_and_scrubs_config_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    def runner(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.example")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "unsafe")
    monkeypatch.setenv("GIT_ASKPASS", "prompt-helper")
    monkeypatch.setenv("SSH_ASKPASS", "prompt-helper")

    LocalGitAdapter(runner=runner)._run(tmp_path, "status", "--porcelain")

    environment = captured["env"]
    assert captured["stdin"] == subprocess.DEVNULL
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_CONFIG_KEY_0" not in environment
    assert "GIT_CONFIG_VALUE_0" not in environment
    assert "GIT_ASKPASS" not in environment
    assert "SSH_ASKPASS" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["SSH_ASKPASS_REQUIRE"] == "never"


def test_strict_oid_and_nonnegative_topology_counts_fail_closed(
    tmp_path: Path,
) -> None:
    _remote, workspace, _peer = repositories(tmp_path)

    def malformed_oid_runner(command, **kwargs):
        if "HEAD^{commit}" in command:
            return subprocess.CompletedProcess(command, 0, "a" * 41 + "\n", "")
        return subprocess.run(command, **kwargs)

    invalid_oid = LocalGitAdapter(runner=malformed_oid_runner).observe(workspace)
    commit_file(workspace, "local.txt", "local\n", "local")

    def negative_count_runner(command, **kwargs):
        if "rev-list" in command:
            return subprocess.CompletedProcess(command, 0, "0 -1\n", "")
        return subprocess.run(command, **kwargs)

    invalid_counts = LocalGitAdapter(runner=negative_count_runner).observe(workspace)

    assert invalid_oid.blockers == ("malformed_git_output",)
    assert invalid_counts.blockers == ("malformed_git_output",)


def test_post_remote_check_lock_is_observed_before_topology(tmp_path: Path) -> None:
    _remote, workspace, _peer = repositories(tmp_path)
    git_dir = Path(git(workspace, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = workspace / git_dir

    def runner(command, **kwargs):
        completed = subprocess.run(command, **kwargs)
        if "ls-remote" in command:
            (git_dir / "index.lock").write_text("", encoding="ascii")
        return completed

    observation = LocalGitAdapter(runner=runner).observe(workspace)

    assert observation.blockers == ("index_locked",)
    assert observation.topology is None


def test_topology_uses_exact_oids_and_final_state_is_revalidated(
    tmp_path: Path,
) -> None:
    _remote, workspace, _peer = repositories(tmp_path)
    commit_file(workspace, "local.txt", "local\n", "local")
    topology_argument = None

    def runner(command, **kwargs):
        nonlocal topology_argument
        completed = subprocess.run(command, **kwargs)
        if "rev-list" in command:
            topology_argument = command[-1]
            (workspace / "tracked.txt").write_text("changed late\n", encoding="utf-8")
        return completed

    observation = LocalGitAdapter(runner=runner).observe(workspace)

    assert topology_argument is not None
    left, right = topology_argument.split("...")
    assert len(left) in (40, 64)
    assert len(right) in (40, 64)
    assert observation.blockers == ("state_changed_during_observation",)


def test_transition_fingerprint_ignores_observation_time(tmp_path: Path) -> None:
    _remote, workspace, _peer = repositories(tmp_path)
    adapter = LocalGitAdapter()

    first = adapter.observe(workspace)
    second = adapter.observe(workspace)

    assert first.transition_fingerprint() == second.transition_fingerprint()
