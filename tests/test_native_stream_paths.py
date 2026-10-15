"""Exact native cwd uses OS path semantics and actual directory identity."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog import native_stream_work as stream

THREAD = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def native(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir(); repo.mkdir()
    database = home / "state_5.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd,source,thread_source,archived)")
        db.execute("INSERT INTO threads VALUES(?,?,'vscode','user',0)", (THREAD, str(repo)))
    def set_cwd(value):
        with sqlite3.connect(database) as db:
            db.execute("UPDATE threads SET cwd=?", (value,))
    return SimpleNamespace(home=home, repo=repo, database=database, set_cwd=set_cwd,
        lookup=lambda: stream.stream_exact_thread(home, THREAD, repo))


def test_native_equivalent_trailing_separator_is_the_same_existing_directory(native):
    native.set_cwd(str(native.repo) + "/")
    assert native.lookup()


@pytest.mark.parametrize("fault", ["relative", "missing", "other", "file", "parent_traversal", "null", "nul", "number"])
def test_stored_cwd_must_remain_absolute_existing_exact_directory(native, fault):
    other = native.home.parent / "other"
    other.mkdir()
    file = native.home.parent / "file"
    file.write_text("not a directory")
    values = dict(relative="repo", missing=str(other / "missing"), other=str(other), file=str(file),
        parent_traversal=str(other / ".." / native.repo.name), null=None, nul=str(native.repo) + "\0bad", number=123)
    native.set_cwd(values[fault])
    assert not native.lookup()


def windows_model(native, monkeypatch, stored, *, resolved=None, exists=True, same_resource=True, error=None,
                  expected=r"C:\Work\Repo", normalized=None, legacy_unc_absolute=False):
    """Inject Windows path/filesystem semantics, never an OS flag or POSIX fold.

    PureWindowsPath supplies real Windows lexical equality. Existence, strict
    resolution and directory file IDs are explicitly simulated; this is not
    evidence from a Windows filesystem or a native process.
    """
    stored_key = str(PureWindowsPath(stored))
    expected_key = str(PureWindowsPath(expected))
    directory_ids = {expected_key: 11, stored_key: 11 if same_resource else 12}
    original_path, original_scope = stream.Path, stream._scope
    calls = []
    class Directory(PureWindowsPath):
        @property
        def value(self): return PureWindowsPath(self)
        def __eq__(self, other):
            return isinstance(other, Directory) and self.value == other.value
        def is_absolute(self):
            if legacy_unc_absolute and str(self.value).startswith("\\\\?\\unc\\"):
                return False  # Actual Python 3.9 lowercase extended-UNC result.
            return self.value.is_absolute()
        def is_dir(self): return exists
        def resolve(self, *, strict=False):
            assert strict is True
            calls.append(("resolve", str(self.value)))
            if error is not None: raise error("simulated invalid resolution")
            if not exists: raise FileNotFoundError()
            return Directory(resolved if str(self.value) == stored_key and resolved else str(self.value))
        def samefile(self, other):
            calls.append(("samefile", str(self.value), str(other.value)))
            return directory_ids[str(self.value)] == directory_ids[str(other.value)]
    def path(value):
        # Model only the supplied repository spellings. On a real Windows
        # test host the native database/sidecar paths also have drive letters;
        # those must keep their real filesystem methods and identity checks.
        modeled = {expected, stored, resolved, normalized}
        return Directory(value) if isinstance(value, str) and value in modeled else original_path(value)
    def scope(home, thread, repo):
        value = original_scope(home, thread, repo)
        value["repo_path"] = expected
        return value
    monkeypatch.setattr(stream, "Path", path)
    monkeypatch.setattr(stream, "_scope", scope)
    native.set_cwd(stored)
    return calls


@pytest.mark.parametrize("stored", ["c:/work/repo", "C:\\Work\\Repo\\"])
def test_simulated_windows_equivalent_cwd_keeps_exact_indexed_scope(native, monkeypatch, stored):
    calls = windows_model(native, monkeypatch, stored)
    assert native.lookup()
    assert any(call[0] == "samefile" for call in calls)


@pytest.mark.parametrize("fault", ["different_repo", "drive_relative", "root_relative", "missing", "redirect", "case_sensitive_collision", "resolve_error"])
def test_simulated_windows_wrong_or_redirected_resource_is_not_equivalent(native, monkeypatch, fault):
    options = {
        "different_repo": dict(stored=r"C:\Other\Repo", same_resource=False),
        "drive_relative": dict(stored=r"C:Work\Repo"),
        "root_relative": dict(stored=r"\Work\Repo"),
        "missing": dict(stored=r"C:\Work\Missing", exists=False),
        "redirect": dict(stored=r"C:\Alias\Repo", resolved=r"C:\Work\Repo"),
        "case_sensitive_collision": dict(stored=r"C:\Work\repo", same_resource=False),
        "resolve_error": dict(stored=r"C:\Work\Repo", error=RuntimeError),
    }
    windows_model(native, monkeypatch, **options[fault])
    assert not native.lookup()


def test_native_redirect_to_expected_repo_is_refused_without_following_alias_as_authority(native, monkeypatch):
    alias = native.home.parent / "alias"
    alias.mkdir()
    native.set_cwd(str(alias))
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        return native.repo if path == alias else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert not native.lookup()


@pytest.mark.parametrize("field,value", [("source", "cli"), ("thread_source", "agent"), ("archived", "0"), ("archived", 0.0)])
def test_canonical_path_does_not_relax_exact_native_row_types(native, field, value):
    native.set_cwd(str(native.repo) + "/")
    with sqlite3.connect(native.database) as db:
        db.execute("UPDATE threads SET " + field + "=?", (value,))
    assert not native.lookup()


@pytest.mark.parametrize("stored,expected,normalized", [
    (r"\\?\C:\Work\Repo", r"C:\Work\Repo", r"C:\Work\Repo"),
    (r"\\?\c:\work\repo", r"C:\Work\Repo", r"c:\work\repo"),
    (r"\\?\UNC\server\share\Repo", r"\\server\share\Repo", r"\\server\share\Repo"),
    (r"\\?\unc\SERVER\share\repo", r"\\server\share\Repo", r"\\SERVER\share\repo"),
])
def test_simulated_extended_windows_anchor_is_only_a_spelling(native, monkeypatch, stored, expected, normalized):
    calls = windows_model(native, monkeypatch, stored, expected=expected, normalized=normalized,
        resolved=expected)
    assert native.lookup()
    assert any(call[0] == "samefile" and call[1] == str(PureWindowsPath(stored)) for call in calls)


def test_python39_unc_parser_shape_uses_the_same_whitelisted_anchor(monkeypatch):
    """Shape retained from actual 3.9.12 Windows and local 3.9.6 diagnostics."""
    stored = r"\\?\unc\SERVER\share\repo"
    class LegacyUNC(PureWindowsPath):
        def __str__(self): return stored
        @property
        def drive(self): return "\\\\?\\"
        @property
        def root(self): return ""
        @property
        def parts(self): return ("\\\\?\\", "unc", "SERVER", "share", "repo")
    monkeypatch.setattr(stream, "Path", PureWindowsPath)
    assert stream._repository_spelling(LegacyUNC(stored)) == PureWindowsPath(r"\\SERVER\share\repo")


@pytest.mark.parametrize("same_resource", [True, False])
def test_legacy_unc_absolute_flag_does_not_replace_original_resource_proof(native, monkeypatch, same_resource):
    stored, expected = r"\\?\unc\SERVER\share\repo", r"\\server\share\Repo"
    calls = windows_model(native, monkeypatch, stored, expected=expected,
        normalized=r"\\SERVER\share\repo", resolved=expected,
        same_resource=same_resource, legacy_unc_absolute=True)
    assert native.lookup() is same_resource
    assert ("resolve", str(PureWindowsPath(stored))) in calls
    assert ("samefile", str(PureWindowsPath(stored)), expected) in calls


@pytest.mark.parametrize("stored,resolved,same_resource", [
    (r"\\.\C:\Work\Repo", r"C:\Work\Repo", True),
    (r"\\?\GLOBALROOT\Device\HarddiskVolume1\Work\Repo", r"C:\Work\Repo", True),
    (r"\\?\Volume{11111111-2222-4333-8444-555555555555}\Work\Repo", r"C:\Work\Repo", True),
    (r"\\?\C:Work\Repo", r"C:\Work\Repo", True),
    (r"\\?\C:\Work\..\Work\Repo", r"C:\Work\Repo", True),
    (r"\\?\C:\Alias\Repo", r"C:\Work\Repo", True),
    (r"\\?\C:\Work\repo", r"C:\Work\Repo", False),
    (r"\\?\UNC\server\share\..\Repo", r"C:\Work\Repo", True),
    (r"\\?\UNC\server", r"C:\Work\Repo", True),
])
def test_simulated_extended_namespace_is_not_redirect_or_device_permission(native, monkeypatch, stored, resolved, same_resource):
    normalized = r"C:\Work\repo" if stored == r"\\?\C:\Work\repo" and not same_resource else None
    calls = windows_model(native, monkeypatch, stored, resolved=resolved,
        same_resource=same_resource, normalized=normalized)
    assert not native.lookup()
    if normalized is not None:
        assert ("samefile", str(PureWindowsPath(stored)), r"C:\Work\Repo") in calls


def test_posix_extended_looking_text_is_not_normalized():
    path = PurePosixPath(r"/repo/\\?\C:\Work\Repo")
    assert stream._repository_spelling(path) is path
