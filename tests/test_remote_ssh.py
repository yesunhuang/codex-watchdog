from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from codex_watchdog.remote_ssh import (
    _REMOTE_SCRIPT,
    FallbackRemoteSshAdapter,
    RemoteSshAdapter,
    RemoteSshTarget,
    SharedPlinkRemoteSshAdapter,
    remote_ssh_adapter_from_environment,
)


def completed(stdout: str, *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_target_derives_stable_identity_and_human_locality_label() -> None:
    target = RemoteSshTarget(
        "ssh-remote+hpc-login.example.edu", "/home/operator/ProjectAlpha", "a" * 32,
    )

    assert target.host == "hpc-login.example.edu"
    assert target.label == "ProjectAlpha @ hpc-login"
    assert target.workspace_id.startswith("vscode-remote-")


def test_adapter_sends_probe_over_stdin_without_shell_interpolation() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return completed(json.dumps({"status": "ok", "session_id": "thread"}))

    target = RemoteSshTarget(
        "ssh-remote+example.invalid",
        "/home/user/repo",
        "b" * 32,
        expected_session_ids=("11111111-2222-4333-8444-555555555555",),
    )
    result = RemoteSshAdapter(runner=runner).probe(target)

    assert result["status"] == "ok"
    command, options = calls[0]
    assert command[:10] == [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "PubkeyAuthentication=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "ConnectTimeout=10",
    ]
    assert command[-2:] == ["example.invalid", "python3 -"]
    assert options["input"].startswith(_REMOTE_SCRIPT)
    assert "/home/user/repo" in options["input"]
    assert (
        "'expected_session_ids': ['11111111-2222-4333-8444-555555555555']"
        in options["input"]
    )
    assert options.get("shell", False) is False


def test_expected_session_skips_remote_window_cache_but_requires_remote_proof(
    tmp_path: Path,
) -> None:
    session = "11111111-2222-4333-8444-555555555555"
    other_session = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    state = tmp_path / ".codex" / "state_5.sqlite"
    state.parent.mkdir()
    state.touch()
    namespace = {"__name__": "remote_adapter_test"}
    exec(_REMOTE_SCRIPT, namespace)
    namespace["Path"] = type("HomePath", (), {"home": staticmethod(lambda: tmp_path)})
    namespace["window_sessions"] = lambda _storage: (_ for _ in ()).throw(
        AssertionError("a local session claim must skip remote window-cache lookup")
    )
    namespace["read_json_database"] = lambda *_args, **_kwargs: [
        (session, "/home/user/repo"),
        (other_session, "/home/user/repo"),
    ]
    namespace["log_session_state"] = lambda candidate: (candidate == session, False,)

    resolved, issue = namespace["resolve_session"](
        "/home/user/repo", "b" * 32, [session, other_session]
    )

    assert resolved == session
    assert issue is None

    namespace["log_session_state"] = lambda _candidate: (False, False)
    resolved, issue = namespace["resolve_session"](
        "/home/user/repo", "b" * 32, [session, other_session]
    )

    assert resolved is None
    assert issue == "remote_thread_claim_unverified"


def test_active_owner_disambiguates_multiple_verified_remote_claims(
    tmp_path: Path,
) -> None:
    session = "11111111-2222-4333-8444-555555555555"
    other_session = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    state = tmp_path / ".codex" / "state_5.sqlite"
    state.parent.mkdir()
    state.touch()
    namespace = {"__name__": "remote_adapter_test"}
    exec(_REMOTE_SCRIPT, namespace)
    namespace["Path"] = type("HomePath", (), {"home": staticmethod(lambda: tmp_path)})
    namespace["read_json_database"] = lambda *_args, **_kwargs: [
        (session, "/home/user/repo"),
        (other_session, "/home/user/repo"),
    ]
    states = {session: (True, False), other_session: (True, True)}
    namespace["log_session_state"] = states.__getitem__

    resolved, issue = namespace["resolve_session"](
        "/home/user/repo", "b" * 32, [session, other_session]
    )

    assert resolved == other_session
    assert issue is None

    states[session] = (True, True)
    resolved, issue = namespace["resolve_session"](
        "/home/user/repo", "b" * 32, [session, other_session]
    )

    assert resolved is None
    assert issue == "remote_thread_claim_unverified"


def test_remote_log_state_retains_owner_and_latest_view_activity(
    tmp_path: Path,
) -> None:
    session = "11111111-2222-4333-8444-555555555555"
    log = (
        tmp_path
        / ".vscode-server"
        / "data"
        / "logs"
        / "20260903T004823"
        / "exthost2"
        / "openai.chatgpt"
        / "Codex.log"
    )
    log.parent.mkdir(parents=True)
    log.write_text(
        "\n".join(
            (
                "2026-09-03 01:28:54.711 [info] thread_stream_role_changed "
                f"conversationId={session} role=owner",
                "2026-09-03 01:28:55.711 [info] "
                "thread_stream_view_activity_changed active=true "
                f"conversationId={session} streamRole=owner",
                "2026-09-03 01:28:56.711 [info] "
                "thread_stream_view_activity_changed active=false "
                f"conversationId={session} streamRole=owner",
            )
        ),
        encoding="utf-8",
    )
    namespace = {"__name__": "remote_adapter_test"}
    exec(_REMOTE_SCRIPT, namespace)
    namespace["Path"] = type("HomePath", (), {"home": staticmethod(lambda: tmp_path)})

    assert namespace["log_session_state"](session) == (True, False)


def test_remote_request_rejects_malformed_session_claims_without_crashing() -> None:
    namespace = {"__name__": "remote_adapter_test"}
    exec(_REMOTE_SCRIPT, namespace)
    emitted = []
    namespace["emit"] = emitted.append

    namespace["run"](
        {
            "repo_path": "/home/user/repo",
            "storage_key": "b" * 32,
            "expected_session_ids": [{}],
        }
    )

    assert emitted == [{"status": "error", "reason": "remote_request_invalid"}]


def test_remote_subprocesses_remain_compatible_with_python_36(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = {"__name__": "remote_adapter_test"}
    exec(_REMOTE_SCRIPT, namespace)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return completed("")

    monkeypatch.setattr(namespace["subprocess"], "run", runner)
    namespace["git"]("/tmp/repo", "status")

    assert calls[0][1]["universal_newlines"] is True
    assert "text" not in calls[0][1]


def test_adapter_hashes_transport_errors_instead_of_returning_raw_stderr() -> None:
    adapter = RemoteSshAdapter(
        runner=lambda *_args, **_kwargs: completed(
            "", returncode=255, stderr="private authentication detail"
        )
    )
    target = RemoteSshTarget("ssh-remote+example.invalid", "/home/user/repo", "c" * 32)

    result = adapter.probe(target)

    assert result["status"] == "unavailable"
    assert result["reason"] == "remote_ssh_auth_or_transport_failed"
    assert "private" not in json.dumps(result)
    assert result["error_chars"] == len("private authentication detail")


def test_shared_plink_requires_live_upstream_before_sending_probe() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return completed("", returncode=1, stderr="no upstream")

    target = RemoteSshTarget(
        "ssh-remote+hpc-login.example.edu", "/home/user/repo", "d" * 32,
    )
    result = SharedPlinkRemoteSshAdapter(
        "user@hpc-login.example.edu", plink_executable="plink.exe", runner=runner,
    ).probe(target)

    assert result["status"] == "unavailable"
    assert result["reason"] == "remote_duo_upstream_unavailable"
    assert len(calls) == 1
    assert calls[0][0] == [
        "plink.exe",
        "-batch",
        "-ssh",
        "-shareexists",
        "user@hpc-login.example.edu",
    ]
    assert "no upstream" not in json.dumps(result)


def test_shared_plink_reuses_upstream_and_preserves_probe_contract() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if "-shareexists" in command:
            return completed("")
        return completed(json.dumps({"status": "ok", "session_id": "thread"}))

    target = RemoteSshTarget(
        "ssh-remote+hpc-login.example.edu", "/home/user/repo", "e" * 32,
    )
    result = SharedPlinkRemoteSshAdapter(
        "user@hpc-login.example.edu", plink_executable="plink.exe", runner=runner,
    ).probe(target, pending_instruction_id="git:pending")

    assert result == {
        "status": "ok",
        "session_id": "thread",
        "transport": "plink_shared_connection",
    }
    assert calls[1][0] == [
        "plink.exe",
        "-batch",
        "-ssh",
        "-share",
        "user@hpc-login.example.edu",
        "python3 -",
    ]
    assert calls[1][1]["input"].startswith(_REMOTE_SCRIPT)
    assert "'pending_instruction_id': 'git:pending'" in calls[1][1]["input"]
    assert calls[1][1].get("shell", False) is False


def test_fallback_only_runs_for_matching_host_primary_transport_failure() -> None:
    class FakeAdapter:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def probe(self, target, **options):
            self.calls.append((target, options))
            return self.result

    class FakeFallback(FakeAdapter):
        def supports(self, target):
            return target.host == "hpc-login.example.edu"

    target = RemoteSshTarget(
        "ssh-remote+hpc-login.example.edu", "/home/user/repo", "f" * 32,
    )
    primary = FakeAdapter(
        {"status": "unavailable", "reason": "remote_ssh_auth_or_transport_failed"}
    )
    fallback = FakeFallback({"status": "ok", "transport": "shared"})
    adapter = FallbackRemoteSshAdapter(primary, fallback)

    assert adapter.probe(target) == {"status": "ok", "transport": "shared"}
    assert len(primary.calls) == len(fallback.calls) == 1

    primary.result = {"status": "unavailable", "reason": "remote_thread_unresolved"}
    assert adapter.probe(target) == primary.result
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 1

    primary.result = {"status": "ok", "session_id": "thread"}
    assert adapter.probe(target) == primary.result
    assert len(primary.calls) == 3
    assert len(fallback.calls) == 1

    primary.result = {
        "status": "unavailable",
        "reason": "remote_ssh_auth_or_transport_failed",
    }
    gpu_lab = RemoteSshTarget(
        "ssh-remote+gpu-lab-personal", "/home/user/repo", "a" * 32
    )
    assert adapter.probe(gpu_lab) == primary.result
    assert len(primary.calls) == 4
    assert len(fallback.calls) == 1


def test_environment_fallback_is_opt_in_and_validates_target() -> None:
    assert isinstance(remote_ssh_adapter_from_environment({}), RemoteSshAdapter)
    configured = remote_ssh_adapter_from_environment(
        {
            "CODEX_WATCHDOG_DUO_PLINK_TARGET": ("user@hpc-login.example.edu"),
            "CODEX_WATCHDOG_PLINK_EXE": "plink.exe",
        }
    )
    assert isinstance(configured, FallbackRemoteSshAdapter)
    with pytest.raises(ValueError, match="user@host"):
        remote_ssh_adapter_from_environment(
            {"CODEX_WATCHDOG_DUO_PLINK_TARGET": "unsafe target"}
        )


@pytest.mark.parametrize(
    "subcommand",
    ("add", "commit", "fetch", "merge", "pull", "push", "rebase", "reset"),
)
def test_remote_adapter_rejects_mutating_git_before_process_execution(
    tmp_path: Path, subcommand: str
) -> None:
    namespace = {"__name__": "remote_adapter_test"}
    exec(_REMOTE_SCRIPT, namespace)

    with pytest.raises(ValueError, match="prohibited_git_subcommand"):
        namespace["git"](str(tmp_path), subcommand)
