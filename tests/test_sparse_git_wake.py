"""Real Git sparse worktrees retain their files/index while remote wakes progress."""
from pathlib import Path

import pytest

from codex_watchdog.git_adapter import LocalGitAdapter
from test_git_adapter import git, repositories
from test_mvp_service import (
    FakeMutator, FakeNotifier, FakeQueue, build_service, receipt, workspace,
)


def sparse_repository(tmp_path, mode):
    _remote, checkout, peer = repositories(tmp_path)
    for name in ("src/code.txt", "results/weights.bin"):
        path = checkout / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    git(checkout, "add", "src", "results")
    git(checkout, "commit", "-m", "sparse fixture")
    git(checkout, "push", "origin", "main")
    git(peer, "pull", "--ff-only")
    if mode == "linked":
        linked = tmp_path / "linked"
        git(checkout, "worktree", "add", "-b", "review", str(linked))
        checkout = linked
        git(checkout, "branch", "--set-upstream-to=origin/main")
    if mode == "noncone":
        git(checkout, "sparse-checkout", "set", "--no-cone", "/*", "!/results/**")
    else:
        git(checkout, "sparse-checkout", "set", "--cone",
            "--sparse-index" if mode == "sparse-index" else "--no-sparse-index", "src")
    assert not (checkout / "results/weights.bin").exists()
    return checkout, peer


def preserved(checkout):
    """Read exact index/config/patterns, refs, and fixture worktree bytes."""
    values = {}
    for name in ("index", "config", "config.worktree", "info/sparse-checkout"):
        path = Path(git(checkout, "rev-parse", "--git-path", name))
        if not path.is_absolute():
            path = checkout / path
        values[name] = path.read_bytes() if path.exists() else None
    for name in ("tracked.txt", "src/code.txt", "results/weights.bin", "new.txt"):
        path = checkout / name
        values[name] = path.read_bytes() if path.exists() else None
    for ref in ("HEAD", "refs/remotes/origin/main"):
        values[ref] = git(checkout, "rev-parse", ref)
    return values


@pytest.mark.parametrize("mode", ["noncone", "cone", "sparse-index", "linked"])
@pytest.mark.parametrize("dirty", [False, True])
def test_sparse_remote_observation_preserves_index_patterns_and_excluded_files(
    tmp_path, mode, dirty,
):
    checkout, peer = sparse_repository(tmp_path, mode)
    if dirty:
        (checkout / "src/code.txt").write_text("changed\n", encoding="utf-8")
        (checkout / "new.txt").write_text("untracked\n", encoding="utf-8")
    adapter = LocalGitAdapter()
    before = preserved(checkout)
    equal = adapter.observe(checkout)
    (peer / "src/code.txt").write_text("remote\n", encoding="utf-8")
    git(peer, "commit", "-am", "remote sparse update")
    git(peer, "push", "origin", "main")
    changed = adapter.observe(checkout)
    assert equal.status == "observed" and equal.topology == "equal"
    assert changed.status == "observed" and changed.topology == "remote_changed"
    assert changed.upstream_oid == git(peer, "rev-parse", "HEAD")
    assert changed.dirty_tracked is dirty and changed.untracked_present is dirty
    assert preserved(checkout) == before


@pytest.mark.parametrize("mode", ["noncone", "sparse-index", "linked"])
def test_sparse_workspace_remote_wake_uses_original_session_once(tmp_path, mode):
    checkout, peer = sparse_repository(tmp_path, mode)
    runtime = tmp_path / "runtime"
    tracked = workspace(checkout)
    queue = FakeQueue(remote_receipt=receipt(tracked.session_id, "git:fresh", status="started"))
    service = build_service(runtime, [tracked], LocalGitAdapter(), FakeMutator(),
                            FakeNotifier(runtime), queue)
    assert service.run_once().workspaces[0].wake is None
    (peer / "src/code.txt").write_text("remote\n", encoding="utf-8")
    git(peer, "commit", "-am", "remote wake")
    git(peer, "push", "origin", "main")
    before = preserved(checkout)
    result = service.run_once().workspaces[0]
    assert result.wake["kind"] == "remote_update"
    assert queue.remote_calls == [(tracked.session_id, git(peer, "rev-parse", "HEAD"), tracked.workspace_id)]
    assert service.run_once().workspaces[0].wake is None
    assert len(queue.remote_calls) == 1
    assert preserved(checkout) == before


@pytest.mark.parametrize("mode", ["noncone", "sparse-index", "linked"])
def test_sparse_worktree_still_refuses_live_index_lock(tmp_path, mode):
    checkout, _peer = sparse_repository(tmp_path, mode)
    path = Path(git(checkout, "rev-parse", "--git-path", "index.lock"))
    if not path.is_absolute():
        path = checkout / path
    path.write_text("live lock", encoding="utf-8")
    before = preserved(checkout)
    observation = LocalGitAdapter().observe(checkout)
    assert "index_locked" in observation.blockers
    assert observation.upstream_oid is None
    assert path.read_text(encoding="utf-8") == "live lock"
    assert preserved(checkout) == before
