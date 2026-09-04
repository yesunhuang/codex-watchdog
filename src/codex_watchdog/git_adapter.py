from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .models import sha256_text, utc_now


_OID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_NONNEGATIVE_COUNT = re.compile(r"^[0-9]+$")
READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "cat-file",
        "check-ref-format",
        "config",
        "for-each-ref",
        "ls-files",
        "ls-remote",
        "remote",
        "rev-list",
        "rev-parse",
        "status",
        "symbolic-ref",
    }
)

# These variables can make ``git -C <registered-root>`` inspect a different
# repository, index, object database, or configuration than the registry says.
# Treat their presence as ambiguous instead of silently overriding user state.
_UNSAFE_GIT_ENVIRONMENT = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
_SCRUBBED_GIT_ENVIRONMENT = frozenset(
    {
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
    }
)
_SCRUBBED_GIT_ENVIRONMENT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


@dataclass(frozen=True)
class GitObservation:
    repo_root: str
    status: str
    topology: Optional[str]
    branch: Optional[str]
    upstream: Optional[str]
    head_oid: Optional[str]
    upstream_oid: Optional[str]
    dirty_tracked: bool
    untracked_present: bool
    blockers: Tuple[str, ...]
    error_sha256: Optional[str]
    error_chars: int
    observed_at: str
    schema_version: int = 1

    def to_dict(self) -> Dict:
        value = asdict(self)
        value["blockers"] = list(self.blockers)
        return value

    def transition_fingerprint(self) -> str:
        stable = self.to_dict()
        stable.pop("observed_at", None)
        return sha256_text(json.dumps(stable, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class _RepositoryState:
    blockers: Tuple[str, ...] = ()
    branch: Optional[str] = None
    upstream: Optional[str] = None
    upstream_ref: Optional[str] = None
    remote: Optional[str] = None
    merge_ref: Optional[str] = None
    head_oid: Optional[str] = None
    upstream_oid: Optional[str] = None
    dirty_tracked: bool = False
    untracked_present: bool = False
    status_fingerprint: Optional[str] = None
    completed: Optional[subprocess.CompletedProcess[str]] = field(
        default=None, compare=False, repr=False
    )

    def stability_key(self, include_upstream_oid: bool) -> Tuple:
        values = (
            self.branch,
            self.upstream,
            self.upstream_ref,
            self.remote,
            self.merge_ref,
            self.head_oid,
            self.dirty_tracked,
            self.untracked_present,
            self.status_fingerprint,
        )
        if include_upstream_oid:
            return (*values, self.upstream_oid)
        return values


class LocalGitAdapter:
    """Inspect local state and the exact remote branch without mutating Git state."""

    def __init__(
        self,
        git_executable: str = "git",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.git_executable = git_executable
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def observe(self, repo_root: Path) -> GitObservation:
        root = Path(repo_root).expanduser().resolve()
        if self._unsafe_git_environment():
            return self._blocked(root, "unsafe_git_environment")
        if not root.is_dir():
            return self._blocked(root, "repo_missing")
        try:
            return self._observe(root)
        except FileNotFoundError:
            return self._blocked(root, "git_unavailable")
        except subprocess.TimeoutExpired:
            return self._blocked(root, "git_timeout")
        except ValueError:
            return self._blocked(root, "malformed_git_output")
        except OSError as exc:
            return self._blocked(root, "inspection_failed", error=str(exc))

    def _observe(self, root: Path) -> GitObservation:
        top = self._run(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return self._blocked(root, "not_worktree", completed=top)
        try:
            actual_root = Path(top.stdout.strip()).resolve()
        except (OSError, ValueError):
            return self._blocked(root, "malformed_git_output", completed=top)
        if actual_root != root:
            return self._blocked(root, "repo_root_mismatch")

        git_dir_result = self._run(root, "rev-parse", "--git-dir")
        common_dir_result = self._run(root, "rev-parse", "--git-common-dir")
        if git_dir_result.returncode != 0 or common_dir_result.returncode != 0:
            return self._blocked(root, "inspection_failed", completed=git_dir_result)
        try:
            git_dir = self._resolve_git_path(root, git_dir_result.stdout)
            common_dir = self._resolve_git_path(root, common_dir_result.stdout)
        except (OSError, ValueError):
            return self._blocked(root, "malformed_git_output")

        before = self._capture_state(root, git_dir, common_dir)
        if before.blockers:
            return self._observation_from_state(root, before)

        remote_check = self._run(
            root,
            "ls-remote",
            "--exit-code",
            "--refs",
            "--",
            before.remote or "",
            before.merge_ref or "",
        )
        if remote_check.returncode != 0:
            return self._observation(
                root,
                ["ls_remote_failed"],
                branch=before.branch,
                upstream=before.upstream,
                head_oid=before.head_oid,
                dirty_tracked=before.dirty_tracked,
                untracked_present=before.untracked_present,
                completed=remote_check,
            )
        try:
            remote_oid = self._parse_ls_remote(
                remote_check.stdout, before.merge_ref or ""
            )
        except ValueError:
            return self._observation_from_state(
                root,
                before,
                blockers=("malformed_git_output",),
                completed=remote_check,
            )

        if not self._identity_matches(root, git_dir, common_dir):
            return self._state_changed(root, before)
        after = self._capture_state(root, git_dir, common_dir)
        if after.blockers:
            return self._observation_from_state(root, after)
        if before.stability_key(True) != after.stability_key(True):
            return self._state_changed(root, after)

        assert after.head_oid is not None
        if after.head_oid == remote_oid:
            topology = "equal"
        else:
            remote_object = self._run(
                root, "cat-file", "-e", f"{remote_oid}^{{commit}}"
            )
            if remote_object.returncode != 0:
                # A newly advertised remote commit normally is not in the local
                # object database. Its OID is still sufficient to detect and
                # deduplicate the inbound update without fetching anything.
                topology = "remote_changed"
            else:
                counts = self._run(
                    root,
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"{after.head_oid}...{remote_oid}",
                )
                if counts.returncode != 0:
                    return self._observation(
                        root,
                        ["topology_unknown"],
                        branch=after.branch,
                        upstream=after.upstream,
                        head_oid=after.head_oid,
                        upstream_oid=remote_oid,
                        dirty_tracked=after.dirty_tracked,
                        untracked_present=after.untracked_present,
                        completed=counts,
                    )
                values = counts.stdout.split()
                if len(values) != 2 or any(
                    not _NONNEGATIVE_COUNT.fullmatch(value) for value in values
                ):
                    return self._observation_from_state(
                        root,
                        replace(after, upstream_oid=remote_oid),
                        blockers=("malformed_git_output",),
                        completed=counts,
                    )
                local_count, remote_count = (int(value) for value in values)
                topology = {
                    (False, False): "equal",
                    (False, True): "remote_ahead",
                    (True, False): "local_ahead",
                    (True, True): "diverged",
                }[(local_count > 0, remote_count > 0)]

        if not self._identity_matches(root, git_dir, common_dir):
            return self._state_changed(root, after)
        final = self._capture_state(root, git_dir, common_dir)
        if final.blockers:
            return self._observation_from_state(root, final)
        if after.stability_key(True) != final.stability_key(True):
            return self._state_changed(root, final)

        return self._observation(
            root,
            ["diverged"] if topology == "diverged" else [],
            topology=topology,
            branch=final.branch,
            upstream=final.upstream,
            head_oid=final.head_oid,
            upstream_oid=remote_oid,
            dirty_tracked=final.dirty_tracked,
            untracked_present=final.untracked_present,
        )

    @staticmethod
    def _parse_ls_remote(output: str, expected_ref: str) -> str:
        lines = output.splitlines()
        if len(lines) != 1:
            raise ValueError("remote branch response must contain exactly one ref")
        fields = lines[0].split()
        if (
            len(fields) != 2
            or fields[1] != expected_ref
            or not _OID.fullmatch(fields[0])
        ):
            raise ValueError("remote branch response is malformed")
        return fields[0].lower()

    @staticmethod
    def _unsafe_git_environment() -> bool:
        for name in os.environ:
            if name in _UNSAFE_GIT_ENVIRONMENT:
                return True
        return False

    def _capture_state(
        self, root: Path, git_dir: Path, common_dir: Path
    ) -> _RepositoryState:
        blockers = self._repository_blockers(root, git_dir, common_dir)
        if blockers:
            return _RepositoryState(blockers=tuple(blockers))

        layout_blockers, layout_completed = self._layout_blockers(root)
        if layout_blockers:
            return _RepositoryState(
                blockers=tuple(layout_blockers), completed=layout_completed
            )

        status_result = self._run(
            root, "status", "--porcelain=v2", "-z", "--untracked-files=normal",
        )
        unmerged_result = self._run(root, "ls-files", "-u", "-z")
        if status_result.returncode != 0 or unmerged_result.returncode != 0:
            failed = status_result if status_result.returncode != 0 else unmerged_result
            return _RepositoryState(blockers=("inspection_failed",), completed=failed)
        try:
            dirty_tracked, untracked_present, status_conflicts = self._parse_status(
                status_result.stdout
            )
        except ValueError:
            return _RepositoryState(
                blockers=("malformed_git_output",), completed=status_result
            )
        status_fingerprint = sha256_text(
            status_result.stdout + "\0" + unmerged_result.stdout
        )
        if unmerged_result.stdout or status_conflicts:
            return _RepositoryState(
                blockers=("conflicts",),
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
            )

        branch_result = self._run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = self._single_line(branch_result.stdout)
        if branch_result.returncode != 0 or branch is None:
            return _RepositoryState(
                blockers=("detached_head",),
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=branch_result if branch_result.returncode != 0 else None,
            )

        head_result = self._run(root, "rev-parse", "--verify", "HEAD^{commit}")
        if head_result.returncode != 0:
            return _RepositoryState(
                blockers=("unborn_head",),
                branch=branch,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=head_result,
            )
        head_oid = self._single_line(head_result.stdout)
        if head_oid is None or not _OID.fullmatch(head_oid):
            return _RepositoryState(
                blockers=("malformed_git_output",),
                branch=branch,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=head_result,
            )

        remote, blocker, completed = self._single_config_value(
            root, f"branch.{branch}.remote"
        )
        if blocker:
            return _RepositoryState(
                blockers=("missing_upstream" if blocker == "missing" else blocker,),
                branch=branch,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=completed,
            )
        merge_ref, blocker, completed = self._single_config_value(
            root, f"branch.{branch}.merge"
        )
        if blocker:
            return _RepositoryState(
                blockers=("missing_upstream" if blocker == "missing" else blocker,),
                branch=branch,
                remote=remote,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=completed,
            )

        assert remote is not None
        assert merge_ref is not None
        if not self._valid_remote_name(remote):
            return _RepositoryState(
                blockers=("unsupported_upstream",),
                branch=branch,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
            )
        remote_result = self._run(root, "remote")
        if remote_result.returncode != 0:
            return _RepositoryState(
                blockers=("inspection_failed",),
                branch=branch,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=remote_result,
            )
        if remote_result.stdout.splitlines().count(remote) != 1:
            return _RepositoryState(
                blockers=("unsupported_upstream",),
                branch=branch,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
            )

        heads_prefix = "refs/heads/"
        if not merge_ref.startswith(heads_prefix) or merge_ref == heads_prefix:
            return _RepositoryState(
                blockers=("unsupported_upstream",),
                branch=branch,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
            )
        remote_branch = merge_ref[len(heads_prefix) :]
        upstream_ref = f"refs/remotes/{remote}/{remote_branch}"
        check_ref = self._run(root, "check-ref-format", upstream_ref)
        if check_ref.returncode != 0:
            return _RepositoryState(
                blockers=("unsupported_upstream",),
                branch=branch,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=check_ref,
            )

        upstream_result = self._run(
            root, "rev-parse", "--symbolic-full-name", "@{upstream}"
        )
        upstream_actual = self._single_line(upstream_result.stdout)
        if upstream_result.returncode != 0 or upstream_actual is None:
            return _RepositoryState(
                blockers=("upstream_unresolved",),
                branch=branch,
                upstream=f"{remote}/{remote_branch}",
                upstream_ref=upstream_ref,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=upstream_result,
            )
        if upstream_actual != upstream_ref:
            return _RepositoryState(
                blockers=("unsupported_upstream",),
                branch=branch,
                upstream=upstream_actual,
                upstream_ref=upstream_ref,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
            )

        upstream_oid_result = self._run(
            root, "rev-parse", "--verify", "@{upstream}^{commit}"
        )
        upstream_oid = self._single_line(upstream_oid_result.stdout)
        if upstream_oid_result.returncode != 0:
            return _RepositoryState(
                blockers=("upstream_unresolved",),
                branch=branch,
                upstream=f"{remote}/{remote_branch}",
                upstream_ref=upstream_ref,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=upstream_oid_result,
            )
        if upstream_oid is None or not _OID.fullmatch(upstream_oid):
            return _RepositoryState(
                blockers=("malformed_git_output",),
                branch=branch,
                upstream=f"{remote}/{remote_branch}",
                upstream_ref=upstream_ref,
                remote=remote,
                merge_ref=merge_ref,
                head_oid=head_oid,
                dirty_tracked=dirty_tracked,
                untracked_present=untracked_present,
                status_fingerprint=status_fingerprint,
                completed=upstream_oid_result,
            )
        return _RepositoryState(
            branch=branch,
            upstream=f"{remote}/{remote_branch}",
            upstream_ref=upstream_ref,
            remote=remote,
            merge_ref=merge_ref,
            head_oid=head_oid,
            upstream_oid=upstream_oid,
            dirty_tracked=dirty_tracked,
            untracked_present=untracked_present,
            status_fingerprint=status_fingerprint,
        )

    def _layout_blockers(
        self, root: Path
    ) -> Tuple[List[str], Optional[subprocess.CompletedProcess[str]]]:
        blockers: List[str] = []
        index_result = self._run(root, "ls-files", "--stage", "-z")
        if index_result.returncode != 0:
            return ["inspection_failed"], index_result
        for record in index_result.stdout.split("\0"):
            if not record:
                continue
            metadata, separator, path = record.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 3 or not path:
                return ["malformed_git_output"], index_result
            if fields[0] == "160000" or path == ".gitmodules":
                blockers.append("submodules_present")

        replace_result = self._run(
            root, "for-each-ref", "--format=%(refname)", "refs/replace/"
        )
        if replace_result.returncode != 0:
            return ["inspection_failed"], replace_result
        if replace_result.stdout:
            blockers.append("replace_refs")

        for key in ("core.sparseCheckout", "index.sparse"):
            config_result = self._run(root, "config", "--bool", "--get-all", key)
            if config_result.returncode == 1 and not config_result.stdout:
                continue
            if config_result.returncode != 0:
                return ["inspection_failed"], config_result
            values = config_result.stdout.splitlines()
            if len(values) != 1 or values[0] not in ("true", "false"):
                return ["ambiguous_git_config"], config_result
            if values[0] == "true":
                blockers.append("sparse_checkout")
        return list(dict.fromkeys(blockers)), None

    def _single_config_value(
        self, root: Path, key: str
    ) -> Tuple[
        Optional[str], Optional[str], Optional[subprocess.CompletedProcess[str]],
    ]:
        result = self._run(root, "config", "--get-all", key)
        if result.returncode == 1 and not result.stdout:
            return None, "missing", result
        if result.returncode != 0:
            return None, "inspection_failed", result
        values = result.stdout.splitlines()
        if len(values) != 1 or not values[0]:
            return None, "ambiguous_upstream", result
        return values[0], None, None

    def _identity_matches(self, root: Path, git_dir: Path, common_dir: Path) -> bool:
        top = self._run(root, "rev-parse", "--show-toplevel")
        git_dir_result = self._run(root, "rev-parse", "--git-dir")
        common_dir_result = self._run(root, "rev-parse", "--git-common-dir")
        if any(
            result.returncode != 0
            for result in (top, git_dir_result, common_dir_result)
        ):
            return False
        try:
            actual_root = Path(top.stdout.strip()).resolve()
            actual_git_dir = self._resolve_git_path(root, git_dir_result.stdout)
            actual_common_dir = self._resolve_git_path(root, common_dir_result.stdout)
        except (OSError, ValueError):
            return False
        return (
            actual_root == root
            and actual_git_dir == git_dir
            and actual_common_dir == common_dir
        )

    @staticmethod
    def _single_line(output: str) -> Optional[str]:
        values = output.splitlines()
        if len(values) != 1 or not values[0]:
            return None
        return values[0]

    @staticmethod
    def _valid_remote_name(remote: str) -> bool:
        return (
            remote != "."
            and not remote.startswith("-")
            and remote == remote.strip()
            and all(
                ord(character) >= 33 and not character.isspace() for character in remote
            )
        )

    def _observation_from_state(
        self,
        root: Path,
        state: _RepositoryState,
        blockers: Optional[Sequence[str]] = None,
        completed: Optional[subprocess.CompletedProcess[str]] = None,
    ) -> GitObservation:
        return self._observation(
            root,
            blockers if blockers is not None else state.blockers,
            branch=state.branch,
            upstream=state.upstream,
            head_oid=state.head_oid,
            upstream_oid=state.upstream_oid,
            dirty_tracked=state.dirty_tracked,
            untracked_present=state.untracked_present,
            completed=completed if completed is not None else state.completed,
        )

    def _state_changed(self, root: Path, state: _RepositoryState) -> GitObservation:
        return self._observation_from_state(
            root, state, blockers=("state_changed_during_observation",)
        )

    def _run(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        if not arguments or arguments[0] not in READ_ONLY_GIT_SUBCOMMANDS:
            raise ValueError("Git subcommand is not allowed in read-only WatchDog")
        environment = os.environ.copy()
        for name in tuple(environment):
            if name in _SCRUBBED_GIT_ENVIRONMENT or name.startswith(
                _SCRUBBED_GIT_ENVIRONMENT_PREFIXES
            ):
                environment.pop(name, None)
        environment.pop("GIT_ASKPASS", None)
        environment.pop("SSH_ASKPASS", None)
        environment.update(
            GIT_TERMINAL_PROMPT="0",
            GCM_INTERACTIVE="Never",
            SSH_ASKPASS_REQUIRE="never",
        )
        return self.runner(
            [self.git_executable, "-C", str(root), *arguments],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            env=environment,
        )

    @staticmethod
    def _resolve_git_path(root: Path, output: str) -> Path:
        raw = output.strip()
        if not raw:
            raise ValueError("empty Git path")
        path = Path(raw)
        return (path if path.is_absolute() else root / path).resolve()

    def _repository_blockers(
        self, root: Path, git_dir: Path, common_dir: Path
    ) -> List[str]:
        blockers: List[str] = []
        directories = tuple(dict.fromkeys((git_dir, common_dir)))
        if any((directory / "index.lock").exists() for directory in directories):
            blockers.append("index_locked")
        if any(self._has_ref_lock(directory) for directory in directories):
            blockers.append("ref_locked")
        markers = {
            "MERGE_HEAD": "merge_in_progress",
            "rebase-apply": "rebase_in_progress",
            "rebase-merge": "rebase_in_progress",
            "CHERRY_PICK_HEAD": "cherry_pick_in_progress",
            "REVERT_HEAD": "revert_in_progress",
            "BISECT_LOG": "bisect_in_progress",
            "sequencer": "sequencer_in_progress",
        }
        for marker, blocker in markers.items():
            if any((directory / marker).exists() for directory in directories):
                blockers.append(blocker)
        if any((directory / "shallow").exists() for directory in directories):
            blockers.append("shallow_repository")
        if any(
            any(path.is_file() for path in (directory / "refs" / "replace").rglob("*"))
            for directory in directories
            if (directory / "refs" / "replace").is_dir()
        ):
            blockers.append("replace_refs")
        if any(
            (directory / "info" / "grafts").is_file()
            and (directory / "info" / "grafts").stat().st_size > 0
            for directory in directories
        ):
            blockers.append("grafts_present")
        if (root / ".gitmodules").exists():
            blockers.append("submodules_present")
        if any(
            (directory / "info" / "sparse-checkout").exists()
            for directory in directories
        ):
            blockers.append("sparse_checkout")
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _has_ref_lock(directory: Path) -> bool:
        fixed = ("HEAD.lock", "packed-refs.lock", "config.lock", "shallow.lock")
        if any((directory / name).exists() for name in fixed):
            return True
        refs = directory / "refs"
        return refs.is_dir() and any(refs.rglob("*.lock"))

    @staticmethod
    def _parse_status(output: str) -> Tuple[bool, bool, bool]:
        dirty_tracked = False
        untracked = False
        conflicts = False
        if output and not output.endswith("\0"):
            raise ValueError("unterminated porcelain v2 output")
        records = output.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            if not record:
                index += 1
                continue
            prefix = record[:2]
            if prefix == "1 ":
                dirty_tracked = True
            elif prefix == "2 ":
                dirty_tracked = True
                index += 1
                if index >= len(records) or not records[index]:
                    raise ValueError("rename record is missing its source path")
            elif prefix == "u ":
                dirty_tracked = True
                conflicts = True
            elif prefix == "? ":
                untracked = True
            elif prefix != "! ":
                raise ValueError("malformed porcelain v2 output")
            index += 1
        return dirty_tracked, untracked, conflicts

    def _blocked(
        self,
        root: Path,
        blocker: str,
        completed: Optional[subprocess.CompletedProcess[str]] = None,
        error: Optional[str] = None,
    ) -> GitObservation:
        return self._observation(root, [blocker], completed=completed, error=error)

    @staticmethod
    def _observation(
        root: Path,
        blockers: Sequence[str],
        topology: Optional[str] = None,
        branch: Optional[str] = None,
        upstream: Optional[str] = None,
        head_oid: Optional[str] = None,
        upstream_oid: Optional[str] = None,
        dirty_tracked: bool = False,
        untracked_present: bool = False,
        completed: Optional[subprocess.CompletedProcess[str]] = None,
        error: Optional[str] = None,
    ) -> GitObservation:
        error_text = error or ""
        if completed is not None:
            error_text = completed.stdout + "\0" + completed.stderr
        unique_blockers = tuple(dict.fromkeys(blockers))
        return GitObservation(
            repo_root=str(root),
            status="blocked" if unique_blockers else "observed",
            topology=topology,
            branch=branch,
            upstream=upstream,
            head_oid=head_oid,
            upstream_oid=upstream_oid,
            dirty_tracked=dirty_tracked,
            untracked_present=untracked_present,
            blockers=unique_blockers,
            error_sha256=sha256_text(error_text) if error_text else None,
            error_chars=len(error_text),
            observed_at=utc_now(),
        )
