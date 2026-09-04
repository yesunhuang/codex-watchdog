"""Exercise Stop hooks through a fresh App Server, the path used by VS Code."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_watchdog.storage import InstructionStore  # noqa: E402
from tools.run_native_hook_probe import (  # noqa: E402
    _audit_records,
    _hooks_document,
    _temporary_project_hooks,
)


@dataclass
class TurnProbe:
    name: str
    turn_id: str
    agent_messages: List[str]
    waiting_observed: bool
    instruction_submitted: bool
    model_events_during_wait: int
    completed_status: Optional[str]
    grace_expired: bool


@dataclass
class QueueWakeProbe:
    command_returncode: int
    command_stdout: str
    command_stderr: str
    turn_id: Optional[str]
    agent_messages: List[str]
    completed_status: Optional[str]
    waiting_observed: bool
    grace_expired: bool


class AppServerClient:
    def __init__(
        self, command: Sequence[str], env: Optional[Dict[str, str]] = None
    ) -> None:
        self.process = subprocess.Popen(
            list(command),
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._stdin = self.process.stdin
        self.messages: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.responses: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._pending_responses: Dict[Any, Dict[str, Any]] = {}
        self.stderr_lines: List[str] = []
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._next_id = 1
        self.observed: List[Dict[str, Any]] = []
        self.client_approval_requests: List[str] = []

    def close(self) -> None:
        try:
            self._stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self._stdout_thread.join(timeout=2)
        self._stderr_thread.join(timeout=2)

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(
        self, method: str, params: Dict[str, Any], timeout: float = 30.0
    ) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._pending_responses.pop(request_id, None)
            if message is None:
                try:
                    message = self.responses.get(
                        timeout=max(0.001, min(0.2, deadline - time.monotonic()))
                    )
                except queue.Empty:
                    if self.process.poll() is not None:
                        raise RuntimeError(
                            "app-server exited unexpectedly: "
                            + "\n".join(self.stderr_lines[-20:])
                        )
                    continue
            if message.get("id") != request_id:
                self._pending_responses[message.get("id")] = message
                continue
            if "error" in message:
                raise RuntimeError(f"{method} failed: {message['error']}")
            return message["result"]
        raise TimeoutError(f"timed out waiting for {method}")

    def get(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        if self.process.poll() is not None and self.messages.empty():
            raise RuntimeError(
                "app-server exited unexpectedly: " + "\n".join(self.stderr_lines[-20:])
            )
        try:
            message = self.messages.get(timeout=max(0.001, timeout))
        except queue.Empty:
            return None
        self.observed.append(message)
        method = message.get("method")
        if "id" in message and method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            self.client_approval_requests.append(str(method))
            self._send({"id": message["id"], "result": {"decision": "decline"}})
        elif "id" in message and method:
            self.client_approval_requests.append(str(method))
            self._send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "unsupported probe client request",
                    },
                }
            )
        return message

    def _send(self, value: Dict[str, Any]) -> None:
        self._stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self._stdin.flush()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                self.stderr_lines.append("invalid JSON on app-server stdout")
                continue
            if isinstance(value, dict):
                if "id" in value and ("result" in value or "error" in value):
                    self.responses.put(value)
                else:
                    self.messages.put(value)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip("\r\n"))


def _is_model_event(message: Dict[str, Any]) -> bool:
    method = message.get("method", "")
    if method in ("turn/started", "turn/completed", "item/agentMessage/delta"):
        return True
    if method in ("item/started", "item/completed"):
        item = message.get("params", {}).get("item", {})
        return item.get("type") in ("agentMessage", "reasoning")
    return False


def _agent_message(message: Dict[str, Any]) -> Optional[str]:
    if message.get("method") != "item/completed":
        return None
    item = message.get("params", {}).get("item", {})
    if item.get("type") != "agentMessage":
        return None
    text = item.get("text")
    return text if isinstance(text, str) else None


def _run_turn(
    client: AppServerClient,
    thread_id: str,
    runtime: Path,
    name: str,
    prompt: str,
    instruction: Optional[Tuple[str, str]],
    wait_probe_seconds: float,
    timeout_seconds: float,
) -> TurnProbe:
    before_audits = {record.get("audit_id") for record in _audit_records(runtime)}
    response = client.request(
        "turn/start",
        {"threadId": thread_id, "input": [{"type": "text", "text": prompt}],},
        timeout=30,
    )
    turn_id = response["turn"]["id"]
    deadline = time.monotonic() + timeout_seconds
    messages: List[str] = []
    waiting_observed = False
    instruction_submitted = False
    model_events_during_wait = -1
    completed_status = None

    while time.monotonic() < deadline:
        records = [
            record
            for record in _audit_records(runtime)
            if record.get("audit_id") not in before_audits
            and record.get("turn_id") == turn_id
        ]
        if not waiting_observed and any(
            record.get("outcome") == "waiting" for record in records
        ):
            waiting_observed = True
            baseline = sum(1 for item in client.observed if _is_model_event(item))
            end_wait = time.monotonic() + wait_probe_seconds
            while time.monotonic() < end_wait:
                message = client.get(timeout=min(0.05, end_wait - time.monotonic()))
                if message is not None:
                    text = _agent_message(message)
                    if text is not None:
                        messages.append(text)
            current = sum(1 for item in client.observed if _is_model_event(item))
            model_events_during_wait = current - baseline
            if instruction is not None:
                InstructionStore(runtime).submit(
                    instruction[0], "appserver_probe", instruction[1]
                )
                instruction_submitted = True

        message = client.get(timeout=0.05)
        if message is None:
            continue
        params = message.get("params", {})
        text = _agent_message(message)
        if (
            text is not None
            and params.get("threadId") == thread_id
            and params.get("turnId") == turn_id
        ):
            messages.append(text)
        if message.get("method") == "turn/completed":
            turn = message.get("params", {}).get("turn", {})
            if turn.get("id") == turn_id:
                completed_status = turn.get("status")
                break
    else:
        raise TimeoutError(f"turn {turn_id} did not complete")

    records = [
        record
        for record in _audit_records(runtime)
        if record.get("audit_id") not in before_audits
        and record.get("turn_id") == turn_id
    ]
    return TurnProbe(
        name=name,
        turn_id=turn_id,
        agent_messages=messages,
        waiting_observed=waiting_observed,
        instruction_submitted=instruction_submitted,
        model_events_during_wait=model_events_during_wait,
        completed_status=completed_status,
        grace_expired=any(
            record.get("outcome") == "grace_expired_parked" for record in records
        ),
    )


def _run_queue_wake(
    client: AppServerClient,
    codex: str,
    thread_id: str,
    runtime: Path,
    timeout_seconds: float,
) -> QueueWakeProbe:
    before_audits = {record.get("audit_id") for record in _audit_records(runtime)}
    command = subprocess.run(
        [
            codex,
            "queue",
            "--dangerously-bypass-hook-trust",
            "-C",
            str(REPO_ROOT),
            "--thread",
            thread_id,
            "--message",
            (
                "This is a durable queue wake. Recall the continuation emitted for "
                "your immediately previous assistant reply. Reply exactly "
                "QUEUE_WAKE_SAME_THREAD:<that reply>. Do not call tools."
            ),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    deadline = time.monotonic() + timeout_seconds
    turn_id: Optional[str] = None
    messages: List[str] = []
    completed_status: Optional[str] = None

    while time.monotonic() < deadline:
        message = client.get(timeout=0.1)
        if message is None:
            continue
        params = message.get("params", {})
        if params.get("threadId") != thread_id:
            continue
        if message.get("method") == "turn/started":
            turn = params.get("turn", {})
            turn_id = turn.get("id")
        text = _agent_message(message)
        if text is not None and (turn_id is None or params.get("turnId") == turn_id):
            messages.append(text)
        if message.get("method") == "turn/completed":
            turn = params.get("turn", {})
            if turn_id is not None and turn.get("id") == turn_id:
                completed_status = turn.get("status")
                break

    records = [
        record
        for record in _audit_records(runtime)
        if record.get("audit_id") not in before_audits
        and (turn_id is None or record.get("turn_id") == turn_id)
    ]
    return QueueWakeProbe(
        command_returncode=command.returncode,
        command_stdout=command.stdout.strip(),
        command_stderr=command.stderr.strip(),
        turn_id=turn_id,
        agent_messages=messages,
        completed_status=completed_status,
        waiting_observed=any(record.get("outcome") == "waiting" for record in records),
        grace_expired=any(
            record.get("outcome") == "grace_expired_parked" for record in records
        ),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--grace-seconds", type=float, default=3.0)
    parser.add_argument("--wait-probe-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--hooks-only", action="store_true")
    parser.add_argument("--isolated-user-hooks-only", action="store_true")
    args = parser.parse_args(argv)

    if args.isolated_user_hooks_only:
        with tempfile.TemporaryDirectory(
            prefix="codex-watchdog-isolated-home-"
        ) as home_name, tempfile.TemporaryDirectory(
            prefix="codex-watchdog-isolated-runtime-"
        ) as runtime_name:
            isolated_home = Path(home_name)
            runtime = Path(runtime_name) / ".codex-watchdog"
            (isolated_home / "hooks.json").write_text(
                json.dumps(_hooks_document(runtime, args.grace_seconds), indent=2)
                + "\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(isolated_home)
            client = AppServerClient(
                [
                    args.codex,
                    "--enable",
                    "hooks",
                    "-C",
                    str(REPO_ROOT),
                    "app-server",
                    "--stdio",
                ],
                env=environment,
            )
            try:
                client.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex_watchdog_user_hook_probe",
                            "title": "Codex Watchdog User Hook Probe",
                            "version": "0.0.1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                client.notify("initialized", {})
                hooks_list = client.request(
                    "hooks/list", {"cwds": [str(REPO_ROOT)]}, timeout=30
                )
                print(json.dumps(hooks_list, indent=2, ensure_ascii=False))
                hooks = hooks_list.get("data", [{}])[0].get("hooks", [])
                return (
                    0
                    if hooks and all(hook.get("source") == "user" for hook in hooks)
                    else 1
                )
            finally:
                client.close()

    with tempfile.TemporaryDirectory(prefix="codex-watchdog-appserver-") as temp_name:
        runtime = Path(temp_name) / ".codex-watchdog"
        with _temporary_project_hooks(runtime, args.grace_seconds):
            client = AppServerClient(
                [
                    args.codex,
                    "--dangerously-bypass-hook-trust",
                    "--enable",
                    "hooks",
                    "-C",
                    str(REPO_ROOT),
                    "app-server",
                    "--stdio",
                ]
            )
            thread_id: Optional[str] = None
            archived = False
            try:
                client.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex_watchdog_probe",
                            "title": "Codex Watchdog Probe",
                            "version": "0.0.1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                client.notify("initialized", {})
                started = client.request(
                    "thread/start",
                    {
                        "model": args.model,
                        "cwd": str(REPO_ROOT),
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "auto_review",
                        "sandbox": "workspace-write",
                        "ephemeral": False,
                    },
                )
                thread_id = started["thread"]["id"]
                hooks_list = client.request(
                    "hooks/list", {"cwds": [str(REPO_ROOT)]}, timeout=30
                )
                if args.hooks_only:
                    print(json.dumps(hooks_list, indent=2, ensure_ascii=False))
                    hooks = hooks_list.get("data", [{}])[0].get("hooks", [])
                    return (
                        0
                        if hooks
                        and all(
                            hook.get("trustStatus") in ("trusted", "managed")
                            for hook in hooks
                        )
                        else 1
                    )
                turns = [
                    _run_turn(
                        client,
                        thread_id,
                        runtime,
                        "continue_once",
                        "Reply exactly APPSERVER_PHASE_ONE. Do not call tools.",
                        (
                            "appserver-probe-1",
                            "Reply exactly APPSERVER_PHASE_TWO. Do not call tools.",
                        ),
                        args.wait_probe_seconds,
                        args.timeout_seconds,
                    ),
                    _run_turn(
                        client,
                        thread_id,
                        runtime,
                        "second_cycle_same_thread",
                        "Reply exactly APPSERVER_PHASE_THREE. Do not call tools.",
                        (
                            "appserver-probe-2",
                            "Reply exactly APPSERVER_PHASE_FOUR. Do not call tools.",
                        ),
                        args.wait_probe_seconds,
                        args.timeout_seconds,
                    ),
                    _run_turn(
                        client,
                        thread_id,
                        runtime,
                        "grace_expiry",
                        "Reply exactly APPSERVER_PARK_PHASE. Do not call tools.",
                        None,
                        0.0,
                        args.timeout_seconds,
                    ),
                ]
                queue_wake = _run_queue_wake(
                    client, args.codex, thread_id, runtime, args.timeout_seconds,
                )
                client.request("thread/archive", {"threadId": thread_id}, timeout=30)
                archived = True
            finally:
                if thread_id is not None and not archived:
                    try:
                        client.request(
                            "thread/archive", {"threadId": thread_id}, timeout=5
                        )
                    except Exception:
                        pass
                client.close()

        summary = {
            "schema_version": 1,
            "codex_version": subprocess.run(
                [args.codex, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            ).stdout.strip(),
            "thread_id": thread_id,
            "turns": [asdict(turn) for turn in turns],
            "distinct_turn_ids": len({turn.turn_id for turn in turns}) == 3,
            "queue_wake": asdict(queue_wake),
            "hooks_list": hooks_list,
            "consumed_instruction_ids": sorted(
                instruction.instruction_id
                for instruction in InstructionStore(runtime).list_state("consumed")
            ),
            "audit_outcomes": [
                record.get("outcome") for record in _audit_records(runtime)
            ],
            "hook_notifications": [
                message.get("method")
                for message in client.observed
                if str(message.get("method", "")).startswith("hook/")
            ],
            "client_approval_requests": client.client_approval_requests,
            "stderr_tail": "\n".join(client.stderr_lines[-20:]),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        expected = {
            "APPSERVER_PHASE_ONE",
            "APPSERVER_PHASE_TWO",
            "APPSERVER_PHASE_THREE",
            "APPSERVER_PHASE_FOUR",
            "APPSERVER_PARK_PHASE",
        }
        observed = {
            message.strip() for turn in turns for message in turn.agent_messages
        }
        success = (
            all(turn.completed_status == "completed" for turn in turns)
            and all(turn.waiting_observed for turn in turns)
            and all(turn.model_events_during_wait == 0 for turn in turns[:2])
            and turns[-1].grace_expired
            and expected.issubset(observed)
            and summary["consumed_instruction_ids"]
            == ["appserver-probe-1", "appserver-probe-2"]
            and queue_wake.command_returncode == 0
            and queue_wake.turn_id is not None
            and queue_wake.completed_status == "completed"
            and queue_wake.waiting_observed
            and queue_wake.grace_expired
            and any(
                message.strip() == "QUEUE_WAKE_SAME_THREAD:APPSERVER_PARK_PHASE"
                for message in queue_wake.agent_messages
            )
        )
        return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
