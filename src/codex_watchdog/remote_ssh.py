from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
from typing import Any, Callable, Dict, Mapping, Optional

from .notifications import notification_workspace_label


_REMOTE_AUTHORITY_PREFIX = "ssh-remote+"
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_PLINK_TARGET = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@"
    r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?)$"
)
_PRIMARY_TRANSPORT_FAILURES = frozenset(
    {"remote_ssh_failed", "remote_ssh_auth_or_transport_failed"}
)
_REMOTE_SCRIPT = r"""
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
OID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
ACK = re.compile(r"^Queued message ([0-9a-fA-F-]{36}) for thread ([0-9a-fA-F-]{36})\.$")
CACHE_KEY = "agentSessions.model.cache"
RESOURCE_PREFIX = "openai-codex://route/local/"
ALLOWED_GIT = frozenset({
    "cat-file", "check-ref-format", "config", "diff", "for-each-ref",
    "ls-files", "ls-remote", "remote", "rev-list", "rev-parse", "status",
    "symbolic-ref",
})


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def sha(value):
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def canonical_uuid(value):
    if not isinstance(value, str) or UUID.fullmatch(value) is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def read_json_database(path, query, parameters=()):
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        connection.execute("PRAGMA query_only = ON")
        return list(connection.execute(query, parameters))


def window_sessions(storage_key):
    database = (
        Path.home()
        / ".vscode-server"
        / "data"
        / "User"
        / "workspaceStorage"
        / storage_key
        / "state.vscdb"
    )
    if not database.is_file():
        return None, "remote_vscode_session_cache_unavailable"
    try:
        rows = read_json_database(
            database, "SELECT value FROM ItemTable WHERE key = ?", (CACHE_KEY,)
        )
        if len(rows) != 1:
            return None, "remote_vscode_session_cache_unavailable"
        entries = json.loads(rows[0][0])
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None, "remote_vscode_session_cache_unavailable"
    if not isinstance(entries, list):
        return None, "remote_vscode_session_cache_unavailable"
    result = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("providerType") != "openai-codex":
            continue
        resource = entry.get("resource")
        if not isinstance(resource, str) or not resource.startswith(RESOURCE_PREFIX):
            return None, "remote_vscode_session_cache_malformed"
        session = canonical_uuid(resource[len(RESOURCE_PREFIX):])
        if session is None:
            return None, "remote_vscode_session_cache_malformed"
        result.add(session)
    return result, None


def log_session_state(session):
    marker = re.compile(
        rb"(?:^|\s)conversationId=" + re.escape(session.encode("ascii")) + rb"(?=\s|$)"
    )
    paths = sorted(
        Path.home().glob(".vscode-server/data/logs/*/exthost*/openai.chatgpt/Codex.log"),
        key=lambda path: str(path),
        reverse=True,
    )
    states = []
    for path in paths[:12]:
        role = None
        active = None
        latest = None
        try:
            with path.open("rb") as handle:
                for index, line in enumerate(handle):
                    if marker.search(line) is None:
                        continue
                    changed = False
                    if b"thread_stream_role_changed" in line:
                        match = re.search(rb"(?:^|\s)role=([^\s]+)(?=\s|$)", line)
                        role = match.group(1) if match is not None else None
                        changed = True
                    elif b"maybe_resume_success" in line:
                        match = re.search(
                            rb"(?:^|\s)assignedStreamRole=([^\s]+)(?=\s|$)", line
                        )
                        role = match.group(1) if match is not None else None
                        changed = True
                    elif b"maybe_resume_failed" in line:
                        role = None
                        changed = True
                    if b"thread_stream_view_activity_changed" in line:
                        match = re.search(rb"(?:^|\s)active=(true|false)(?=\s|$)", line)
                        if match is not None:
                            active = match.group(1) == b"true"
                        match = re.search(
                            rb"(?:^|\s)streamRole=([^\s]+)(?=\s|$)", line
                        )
                        if match is not None and match.group(1) != b"null":
                            role = match.group(1)
                        changed = True
                    if changed:
                        stamp = re.match(rb"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", line)
                        latest = (
                            stamp.group(0) if stamp is not None else b"",
                            str(path),
                            index,
                            role,
                            active,
                        )
        except OSError:
            continue
        if latest is not None:
            states.append(latest)
    if not states:
        return False, False
    latest = max(states, key=lambda item: item[:3])
    return latest[3] == b"owner", latest[4] is True


def resolve_session(repo_path, storage_key, expected_sessions=None):
    if expected_sessions is None:
        loaded, issue = window_sessions(storage_key)
        if loaded is None:
            return None, issue
    else:
        loaded = set(expected_sessions)
    state = Path.home() / ".codex" / "state_5.sqlite"
    if not state.is_file():
        return None, "remote_codex_state_unavailable"
    try:
        rows = read_json_database(
            state,
            "SELECT id, cwd FROM threads WHERE archived = 0 "
            "AND source = 'vscode' AND thread_source = 'user'",
        )
    except (OSError, sqlite3.Error):
        return None, "remote_codex_state_unavailable"
    expected = os.path.normpath(repo_path)
    candidates = {}
    for raw_session, raw_cwd in rows:
        session = canonical_uuid(raw_session)
        if (
            session is not None
            and session in loaded
            and isinstance(raw_cwd, str)
            and os.path.normpath(raw_cwd) == expected
        ):
            owner, active = log_session_state(session)
            if owner:
                candidates[session] = active
    if len(candidates) == 1:
        return next(iter(candidates)), None
    active_candidates = {
        session for session, active in candidates.items() if active
    }
    if len(active_candidates) == 1:
        return next(iter(active_candidates)), None
    if expected_sessions is not None:
        return None, "remote_thread_claim_unverified"
    return None, (
        "remote_thread_ambiguous" if len(candidates) > 1 else "remote_thread_unresolved"
    )


def rollout_path(session):
    matches = list((Path.home() / ".codex" / "sessions").glob(
        "**/rollout-*" + session + ".jsonl"
    ))
    return matches[0] if len(matches) == 1 else None


def rollout_completion(session):
    path = rollout_path(session)
    if path is None:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 1048576))
            content = handle.read()
    except OSError:
        return None
    events = []
    for line in content.splitlines()[1 if size > len(content) else 0:]:
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    latest = None
    lifecycle = None
    for event in events:
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        if payload.get("type") == "task_started":
            lifecycle = "started"
        elif payload.get("type") == "task_complete":
            turn = payload.get("turn_id")
            output = payload.get("last_agent_message")
            if isinstance(turn, str) and isinstance(output, str) and output:
                latest = {
                    "turn_id": turn,
                    "completed_at": event.get("timestamp"),
                    "final_output": output[:32000],
                    "final_output_sha256": sha(output),
                    "final_output_chars": len(output),
                }
                lifecycle = "completed"
    return latest if lifecycle == "completed" else None


def git(repo, *arguments):
    if not arguments or arguments[0] not in ALLOWED_GIT:
        raise ValueError("prohibited_git_subcommand")
    return subprocess.run(
        ["git", "-C", repo, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def git_observation(repo):
    result = {
        "status": "blocked",
        "topology": None,
        "head_oid": None,
        "upstream_oid": None,
        "dirty_tracked": False,
        "untracked_present": False,
        "blockers": [],
    }
    try:
        top = git(repo, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or os.path.normpath(top.stdout.strip()) != os.path.normpath(repo):
            result["blockers"] = ["remote_repo_root_mismatch"]
            return result
        status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
        branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        head = git(repo, "rev-parse", "--verify", "HEAD^{commit}")
        if status.returncode != 0 or branch.returncode != 0 or head.returncode != 0:
            result["blockers"] = ["remote_git_inspection_failed"]
            return result
        records = [item for item in status.stdout.split("\0") if item]
        result["untracked_present"] = any(item.startswith("?? ") for item in records)
        result["dirty_tracked"] = any(not item.startswith("?? ") for item in records)
        head_oid = head.stdout.strip()
        if OID.fullmatch(head_oid) is None:
            result["blockers"] = ["remote_git_output_malformed"]
            return result
        branch_name = branch.stdout.strip()
        remote = git(repo, "config", "--get", "branch." + branch_name + ".remote")
        merge = git(repo, "config", "--get", "branch." + branch_name + ".merge")
        if remote.returncode != 0 or merge.returncode != 0:
            result.update(head_oid=head_oid, blockers=["missing_upstream"])
            return result
        remote_name = remote.stdout.strip()
        merge_ref = merge.stdout.strip()
        advertised = git(
            repo, "ls-remote", "--exit-code", "--refs", "--", remote_name, merge_ref
        )
        lines = [line.split() for line in advertised.stdout.splitlines() if line.strip()]
        if advertised.returncode != 0 or len(lines) != 1 or len(lines[0]) != 2:
            result.update(head_oid=head_oid, blockers=["ls_remote_failed"])
            return result
        remote_oid = lines[0][0]
        if OID.fullmatch(remote_oid) is None or lines[0][1] != merge_ref:
            result.update(head_oid=head_oid, blockers=["remote_git_output_malformed"])
            return result
        topology = "equal" if head_oid == remote_oid else "remote_changed"
        if topology != "equal":
            present = git(repo, "cat-file", "-e", remote_oid + "^{commit}")
            if present.returncode == 0:
                counts = git(repo, "rev-list", "--left-right", "--count", head_oid + "..." + remote_oid)
                values = counts.stdout.split()
                if counts.returncode == 0 and len(values) == 2 and all(value.isdigit() for value in values):
                    left, right = (int(value) for value in values)
                    topology = {
                        (True, False): "local_ahead",
                        (False, True): "remote_ahead",
                        (True, True): "diverged",
                        (False, False): "equal",
                    }[(left > 0, right > 0)]
        result.update(
            status="observed",
            topology=topology,
            head_oid=head_oid,
            upstream_oid=remote_oid,
            blockers=[],
        )
        return result
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["blockers"] = ["remote_git_inspection_failed"]
        result["error_sha256"] = sha(str(exc))
        result["error_chars"] = len(str(exc))
        return result


def queue_database():
    pinned = Path.home() / ".codex" / "queue_1.sqlite"
    return pinned if pinned.is_file() else None


def queue_snapshot(database, thread, message=None):
    if database is None:
        return None, None
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        connection.execute("PRAGMA query_only = ON")
        item = None
        if message is not None:
            item = connection.execute(
                "SELECT payload_json FROM queued_items WHERE id = ? AND thread_id = ?",
                (message, thread),
            ).fetchone()
        revision = connection.execute(
            "SELECT revision FROM queued_thread_revisions WHERE thread_id = ?", (thread,)
        ).fetchone()
    return (item[0] if item else None, int(revision[0]) if revision else 0)


def wake_record_path(instruction_id):
    root = Path.home() / ".codex-watchdog" / "remote-wake"
    root.mkdir(parents=True, exist_ok=True)
    return root / (sha(instruction_id) + ".json")


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def codex_executable():
    matches = sorted(glob.glob(
        str(Path.home() / ".vscode-server/extensions/openai.chatgpt-*/bin/linux-*/codex")
    ))
    return matches[-1] if matches else None


def rollout_started(record):
    path = Path(record.get("rollout_path", ""))
    baseline = record.get("rollout_baseline_offset")
    if not path.is_file() or not isinstance(baseline, int):
        return None
    try:
        size = path.stat().st_size
        if size - baseline > 4194304:
            return None
        with path.open("rb") as handle:
            handle.seek(baseline)
            content = handle.read()
    except OSError:
        return None
    marker = "[CODEX_WATCHDOG_WAKE id={} sha256={}]".format(
        record.get("instruction_id"), record.get("prompt_sha256")
    )
    started = set()
    messages = set()
    for line in content.splitlines():
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        turn = payload.get("turn_id")
        if payload.get("type") == "task_started" and isinstance(turn, str):
            started.add(turn)
        if payload.get("type") == "item_completed" and isinstance(turn, str):
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                for part in item.get("content", []):
                    if isinstance(part, dict) and str(part.get("text", "")).startswith(marker + "\n"):
                        messages.add(turn)
    matches = started & messages
    return next(iter(matches)) if len(matches) == 1 else None


def observe_wake(record):
    state = record.get("state", "uncertain")
    if state not in ("enqueued", "consumed_or_started", "started"):
        return record
    started = rollout_started(record)
    if started is not None:
        record.update(state="started", started_turn_id=started)
    else:
        database = queue_database()
        try:
            item, revision = queue_snapshot(
                database, record["thread_id"], record.get("queue_message_id")
            )
        except (OSError, sqlite3.Error):
            item, revision = None, None
        if item is not None:
            record["queue_row_seen"] = True
        elif (
            revision is not None
            and (
                record.get("queue_row_seen") is True
                or revision >= int(record.get("queue_baseline_revision") or 0) + 2
            )
        ):
            record["state"] = "consumed_or_started"
        record["queue_revision"] = revision
    atomic_json(wake_record_path(record["instruction_id"]), record)
    return record


def dispatch_wake(request, session):
    instruction_id = request["instruction_id"]
    prompt = request["prompt"]
    digest = sha(prompt)
    path = wake_record_path(instruction_id)
    if path.is_file():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "uncertain", "reason": "remote_wake_record_malformed"}
        if record.get("thread_id") != session or record.get("prompt_sha256") != digest:
            return {"status": "uncertain", "reason": "remote_wake_id_collision"}
        return observe_wake(record)
    database = queue_database()
    try:
        _, baseline_revision = queue_snapshot(database, session)
    except (OSError, sqlite3.Error):
        baseline_revision = None
    rollout = rollout_path(session)
    try:
        offset = rollout.stat().st_size if rollout is not None else None
    except OSError:
        offset = None
    record = {
        "instruction_id": instruction_id,
        "thread_id": session,
        "prompt_sha256": digest,
        "state": "dispatching",
        "queue_baseline_revision": baseline_revision,
        "rollout_path": str(rollout) if rollout is not None else None,
        "rollout_baseline_offset": offset,
    }
    atomic_json(path, record)
    executable = codex_executable()
    if executable is None:
        record.update(state="uncertain", reason="remote_codex_executable_unavailable")
        atomic_json(path, record)
        return record
    marker = "[CODEX_WATCHDOG_WAKE id={} sha256={}]\n{}".format(
        instruction_id, digest, prompt
    )
    try:
        completed = subprocess.run(
            [executable, "queue", "--thread", session, "--message", marker],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env={**os.environ, "CODEX_HOME": str(Path.home() / ".codex")},
        )
        output = completed.stdout.strip()
        match = ACK.fullmatch(output)
        if completed.returncode == 0 and match and canonical_uuid(match.group(2)) == session:
            record.update(
                state="enqueued",
                queue_message_id=canonical_uuid(match.group(1)),
                returncode=completed.returncode,
            )
        else:
            record.update(
                state="uncertain",
                returncode=completed.returncode,
                stdout_sha256=sha(output) if output else None,
                stderr_sha256=sha(completed.stderr) if completed.stderr else None,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        record.update(state="uncertain", reason="remote_queue_failed", error_sha256=sha(str(exc)))
    atomic_json(path, record)
    return record


def run(request):
    repo = request.get("repo_path")
    storage_key = request.get("storage_key")
    expected_sessions = request.get("expected_session_ids")
    if (
        not isinstance(repo, str)
        or not repo.startswith("/")
        or not isinstance(storage_key, str)
        or re.fullmatch(r"[0-9a-f]{32}", storage_key) is None
        or (
            expected_sessions is not None
            and (
                not isinstance(expected_sessions, list)
                or not expected_sessions
                or len(expected_sessions) > 64
                or any(canonical_uuid(value) != value for value in expected_sessions)
                or len(set(expected_sessions)) != len(expected_sessions)
            )
        )
    ):
        emit({"status": "error", "reason": "remote_request_invalid"})
        return
    session, issue = resolve_session(repo, storage_key, expected_sessions)
    if session is None:
        emit({"status": "unavailable", "reason": issue})
        return
    result = {
        "status": "ok",
        "session_id": session,
        "repo_path": repo,
        "git": git_observation(repo),
        "completion": rollout_completion(session),
    }
    pending_id = request.get("pending_instruction_id")
    if isinstance(pending_id, str):
        path = wake_record_path(pending_id)
        if path.is_file():
            try:
                result["wake"] = observe_wake(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                result["wake"] = {"state": "uncertain", "reason": "remote_wake_record_malformed"}
    if request.get("action") == "wake":
        result["wake"] = dispatch_wake(request, session)
    emit(result)
"""


@dataclass(frozen=True)
class RemoteSshTarget:
    authority: str
    repo_path: str
    storage_key: str
    expected_session_ids: tuple[str, ...] = ()

    @property
    def host(self) -> str:
        value = self.authority
        if value.startswith(_REMOTE_AUTHORITY_PREFIX):
            value = value[len(_REMOTE_AUTHORITY_PREFIX) :]
        if _HOST.fullmatch(value) is None or value.startswith("-"):
            raise ValueError("remote SSH authority is invalid")
        return value

    @property
    def workspace_id(self) -> str:
        identity = f"{self.authority}\0{self.repo_path}"
        return "vscode-remote-" + hashlib.sha256(identity.encode()).hexdigest()[:32]

    @property
    def label(self) -> str:
        locality = self.host.split(".", 1)[0]
        return notification_workspace_label(
            self.workspace_id, PurePosixPath(self.repo_path), locality=locality
        )


class RemoteSshAdapter:
    """Run a compact, fail-closed probe beside one Remote-SSH workspace."""

    def __init__(
        self,
        *,
        ssh_executable: str = "ssh",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 40.0,
    ) -> None:
        self.ssh_executable = ssh_executable
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def probe(
        self,
        target: RemoteSshTarget,
        *,
        pending_instruction_id: Optional[str] = None,
        wake: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            "repo_path": target.repo_path,
            "storage_key": target.storage_key,
        }
        if target.expected_session_ids:
            request["expected_session_ids"] = list(target.expected_session_ids)
        if pending_instruction_id is not None:
            request["pending_instruction_id"] = pending_instruction_id
        if wake is not None:
            request.update(
                action="wake",
                instruction_id=wake["instruction_id"],
                prompt=wake["prompt"],
            )
        script = _REMOTE_SCRIPT + "\nrun(" + repr(request) + ")\n"
        try:
            completed = self.runner(
                [
                    self.ssh_executable,
                    "-T",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "PubkeyAuthentication=yes",
                    "-o",
                    "PreferredAuthentications=publickey",
                    "-o",
                    "ConnectTimeout=10",
                    target.host,
                    "python3 -",
                ],
                input=script,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self._failure("remote_ssh_failed", str(exc))
        output = completed.stdout.strip()
        if completed.returncode != 0:
            return self._failure(
                "remote_ssh_auth_or_transport_failed", completed.stderr.strip()
            )
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return self._failure("remote_adapter_output_malformed", output)
        if not isinstance(result, dict):
            return self._failure("remote_adapter_output_malformed", output)
        return result

    @staticmethod
    def _failure(reason: str, detail: str) -> Dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": reason,
            "error_sha256": hashlib.sha256(
                detail.encode("utf-8", errors="replace")
            ).hexdigest(),
            "error_chars": len(detail),
        }


class SharedPlinkRemoteSshAdapter:
    """Reuse one operator-authenticated PuTTY/Plink SSH-2 upstream."""

    def __init__(
        self,
        plink_target: str,
        *,
        plink_executable: str = "plink",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 40.0,
    ) -> None:
        match = _PLINK_TARGET.fullmatch(plink_target)
        if match is None:
            raise ValueError("Plink target must have the form user@host")
        self.plink_target = plink_target
        self.host = match.group("host").lower()
        self.plink_executable = plink_executable
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def supports(self, target: RemoteSshTarget) -> bool:
        return target.host.lower() == self.host

    def probe(
        self,
        target: RemoteSshTarget,
        *,
        pending_instruction_id: Optional[str] = None,
        wake: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if not self.supports(target):
            return RemoteSshAdapter._failure(
                "remote_duo_fallback_host_mismatch", target.host
            )
        try:
            available = self.runner(
                [
                    self.plink_executable,
                    "-batch",
                    "-ssh",
                    "-shareexists",
                    self.plink_target,
                ],
                input="",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(self.timeout_seconds, 10.0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return RemoteSshAdapter._failure("remote_plink_check_failed", str(exc))
        if available.returncode != 0:
            return RemoteSshAdapter._failure(
                "remote_duo_upstream_unavailable", available.stderr.strip()
            )

        request: Dict[str, Any] = {
            "repo_path": target.repo_path,
            "storage_key": target.storage_key,
        }
        if target.expected_session_ids:
            request["expected_session_ids"] = list(target.expected_session_ids)
        if pending_instruction_id is not None:
            request["pending_instruction_id"] = pending_instruction_id
        if wake is not None:
            request.update(
                action="wake",
                instruction_id=wake["instruction_id"],
                prompt=wake["prompt"],
            )
        script = _REMOTE_SCRIPT + "\nrun(" + repr(request) + ")\n"
        try:
            completed = self.runner(
                [
                    self.plink_executable,
                    "-batch",
                    "-ssh",
                    "-share",
                    self.plink_target,
                    "python3 -",
                ],
                input=script,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return RemoteSshAdapter._failure("remote_plink_failed", str(exc))
        output = completed.stdout.strip()
        if completed.returncode != 0:
            return RemoteSshAdapter._failure(
                "remote_plink_shared_transport_failed", completed.stderr.strip()
            )
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return RemoteSshAdapter._failure("remote_adapter_output_malformed", output)
        if not isinstance(result, dict):
            return RemoteSshAdapter._failure("remote_adapter_output_malformed", output)
        return {**result, "transport": "plink_shared_connection"}


class FallbackRemoteSshAdapter:
    """Keep batch OpenSSH primary and select Plink only on transport failure."""

    def __init__(
        self, primary: RemoteSshAdapter, fallback: SharedPlinkRemoteSshAdapter,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def probe(
        self,
        target: RemoteSshTarget,
        *,
        pending_instruction_id: Optional[str] = None,
        wake: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        options = {
            "pending_instruction_id": pending_instruction_id,
            "wake": wake,
        }
        result = self.primary.probe(target, **options)
        if result.get(
            "reason"
        ) not in _PRIMARY_TRANSPORT_FAILURES or not self.fallback.supports(target):
            return result
        return self.fallback.probe(target, **options)


def remote_ssh_adapter_from_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> Any:
    """Create the normal adapter plus an explicitly configured Duo fallback."""

    values = os.environ if environ is None else environ
    plink_target = values.get("CODEX_WATCHDOG_DUO_PLINK_TARGET", "").strip()
    if not plink_target:
        return RemoteSshAdapter()
    plink_executable = values.get("CODEX_WATCHDOG_PLINK_EXE", "").strip() or "plink"
    return FallbackRemoteSshAdapter(
        RemoteSshAdapter(),
        SharedPlinkRemoteSshAdapter(plink_target, plink_executable=plink_executable),
    )
