"""Run the real Codex hook engine in a disposable local CLI conversation.

This is an explicit acceptance probe, not a unit test. It makes small model calls,
uses a generated hook definition with trust bypass, and deletes the temporary
workspace afterward. It never installs or edits the active project's hook layer.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_watchdog.storage import InstructionStore  # noqa: E402


@dataclass
class CycleResult:
    name: str
    returncode: int
    thread_id: Optional[str]
    agent_messages: List[str]
    waiting_observed: bool
    instruction_submitted: bool
    model_events_during_wait: int
    grace_expired: bool
    stderr_tail: str


class OutputCollector:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.lines: List[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float = 2.0) -> None:
        self._thread.join(timeout)

    def snapshot(self) -> List[str]:
        with self._lock:
            return list(self.lines)

    def _read(self) -> None:
        for line in self.stream:
            with self._lock:
                self.lines.append(line.rstrip("\r\n"))


def _audit_records(runtime: Path) -> List[Dict]:
    records = []
    for path in sorted((runtime / "audit").glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _model_event_count(lines: Iterable[str]) -> int:
    count = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event_type in ("turn.started", "turn.completed") or item.get("type") in (
            "agent_message",
            "reasoning",
        ):
            count += 1
    return count


def _parse_output(lines: Iterable[str]) -> Tuple[Optional[str], List[str]]:
    thread_id = None
    messages: List[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id") or event.get("threadId")
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                messages.append(text)
    return thread_id, messages


def _run_cycle(
    command: Sequence[str],
    runtime: Path,
    name: str,
    instruction: Optional[Tuple[str, str]],
    wait_probe_seconds: float,
    timeout_seconds: float,
) -> CycleResult:
    before_audits = {record.get("audit_id") for record in _audit_records(runtime)}
    process = subprocess.Popen(
        list(command),
        cwd=str(command_workspace(command)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = OutputCollector(process.stdout)
    stderr = OutputCollector(process.stderr)
    stdout.start()
    stderr.start()

    waiting_observed = False
    instruction_submitted = False
    events_during_wait = -1
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and process.poll() is None:
        records = [
            record
            for record in _audit_records(runtime)
            if record.get("audit_id") not in before_audits
        ]
        if any(record.get("outcome") == "waiting" for record in records):
            waiting_observed = True
            before_count = _model_event_count(stdout.snapshot())
            if wait_probe_seconds:
                time.sleep(wait_probe_seconds)
            after_count = _model_event_count(stdout.snapshot())
            events_during_wait = after_count - before_count
            if instruction is not None:
                InstructionStore(runtime).submit(
                    instruction[0], "native_probe", instruction[1]
                )
                instruction_submitted = True
            break
        time.sleep(0.05)

    try:
        returncode = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        returncode = -1
    stdout.join()
    stderr.join()
    output_lines = stdout.snapshot()
    error_lines = stderr.snapshot()
    thread_id, messages = _parse_output(output_lines)
    new_records = [
        record
        for record in _audit_records(runtime)
        if record.get("audit_id") not in before_audits
    ]
    grace_expired = any(
        record.get("outcome") == "grace_expired_parked" for record in new_records
    )
    return CycleResult(
        name=name,
        returncode=returncode,
        thread_id=thread_id,
        agent_messages=messages,
        waiting_observed=waiting_observed,
        instruction_submitted=instruction_submitted,
        model_events_during_wait=events_during_wait,
        grace_expired=grace_expired,
        stderr_tail="\n".join(error_lines[-20:]),
    )


def command_workspace(command: Sequence[str]) -> Path:
    for index, argument in enumerate(command):
        if argument in ("-C", "--cd") and index + 1 < len(command):
            return Path(command[index + 1])
    return Path.cwd()


def _join_hook_command(parts: Sequence[str], windows: bool) -> str:
    if not windows:
        return shlex.join(parts)
    unsafe = [part for part in parts if '"' in part or any(c.isspace() for c in part)]
    if unsafe:
        raise ValueError(
            "the current Windows hook runner requires a quote-free command; "
            "use space-free absolute paths"
        )
    return " ".join(parts)


def _hook_command(runtime: Path, grace_seconds: float) -> str:
    hook_script = REPO_ROOT / "tools" / "codex_watchdog_hook.py"
    return _join_hook_command(
        [
            sys.executable,
            str(hook_script),
            "--runtime",
            str(runtime),
            "hook",
            "--grace-seconds",
            f"{grace_seconds:g}",
            "--poll-seconds",
            "0.05",
            "--test-mode",
        ],
        windows=os.name == "nt",
    )


def _hooks_document(runtime: Path, grace_seconds: float) -> Dict:
    command = _hook_command(runtime, grace_seconds)
    return {
        "description": "Temporary native-hook acceptance probe",
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "commandWindows": command,
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "commandWindows": command,
                            "timeout": max(10, int(grace_seconds) + 5),
                        }
                    ]
                }
            ],
        },
    }


@contextmanager
def _temporary_project_hooks(runtime: Path, grace_seconds: float):
    """Install only for the lifetime of this fresh-process acceptance probe."""
    codex_dir = REPO_ROOT / ".codex"
    hook_path = codex_dir / "hooks.json"
    if hook_path.exists():
        raise RuntimeError(
            f"refusing to replace existing project hook file: {hook_path}"
        )
    hooks = _hooks_document(runtime, grace_seconds)
    codex_dir.mkdir(parents=True, exist_ok=True)
    hook_content = json.dumps(hooks, indent=2) + "\n"
    temporary_path = codex_dir / "hooks.watchdog-probe.tmp"
    temporary_path.write_text(hook_content, encoding="utf-8")
    os.replace(temporary_path, hook_path)
    try:
        yield
    finally:
        try:
            current_content = hook_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current_content = None
        if current_content == hook_content:
            hook_path.unlink(missing_ok=True)
        elif current_content is not None:
            print(
                f"refusing to remove concurrently modified hook file: {hook_path}",
                file=sys.stderr,
            )
        try:
            codex_dir.rmdir()
        except OSError:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--grace-seconds", type=float, default=3.0)
    parser.add_argument("--wait-probe-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="codex-watchdog-native-") as temp_name:
        runtime = Path(temp_name) / ".codex-watchdog"
        with _temporary_project_hooks(runtime, args.grace_seconds):
            base = [
                args.codex,
                "exec",
                "--json",
                "--dangerously-bypass-hook-trust",
                "--enable",
                "hooks",
                "--skip-git-repo-check",
                "-C",
                str(REPO_ROOT),
                "-m",
                args.model,
                "-c",
                'model_reasoning_effort="low"',
                "--sandbox",
                "read-only",
            ]
            first = _run_cycle(
                [*base, "Reply exactly NATIVE_PHASE_ONE. Do not call tools."],
                runtime,
                "continue_once",
                (
                    "native-probe-1",
                    "Reply exactly NATIVE_PHASE_TWO. Do not call tools.",
                ),
                args.wait_probe_seconds,
                args.timeout_seconds,
            )
            results = [first]
            if first.thread_id and first.returncode == 0 and first.waiting_observed:
                resume_base = [
                    args.codex,
                    "exec",
                    "resume",
                    "--json",
                    "--dangerously-bypass-hook-trust",
                    "--enable",
                    "hooks",
                    "--skip-git-repo-check",
                    "-m",
                    args.model,
                    "-c",
                    'model_reasoning_effort="low"',
                    first.thread_id,
                ]
                second = _run_cycle(
                    [
                        *resume_base,
                        "Reply exactly NATIVE_PHASE_THREE. Do not call tools.",
                    ],
                    runtime,
                    "second_cycle_same_thread",
                    (
                        "native-probe-2",
                        "Reply exactly NATIVE_PHASE_FOUR. Do not call tools.",
                    ),
                    args.wait_probe_seconds,
                    args.timeout_seconds,
                )
                results.append(second)
                third = _run_cycle(
                    [
                        *resume_base,
                        "Reply exactly NATIVE_PARK_PHASE. Do not call tools.",
                    ],
                    runtime,
                    "grace_expiry",
                    None,
                    0.0,
                    args.timeout_seconds,
                )
                results.append(third)

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
            "cycles": [asdict(result) for result in results],
            "consumed_instruction_ids": [
                instruction.instruction_id
                for instruction in InstructionStore(runtime).list_state("consumed")
            ],
            "inflight_instruction_ids": [
                instruction.instruction_id
                for instruction in InstructionStore(runtime).list_state("inflight")
            ],
            "audit_outcomes": [
                record.get("outcome") for record in _audit_records(runtime)
            ],
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))

        expected_messages = {
            "NATIVE_PHASE_ONE",
            "NATIVE_PHASE_TWO",
            "NATIVE_PHASE_THREE",
            "NATIVE_PHASE_FOUR",
            "NATIVE_PARK_PHASE",
        }
        observed_messages = {
            message.strip() for result in results for message in result.agent_messages
        }
        success = (
            len(results) == 3
            and all(result.returncode == 0 for result in results)
            and all(result.waiting_observed for result in results)
            and all(result.model_events_during_wait == 0 for result in results[:2])
            and results[-1].grace_expired
            and expected_messages.issubset(observed_messages)
            and sorted(summary["consumed_instruction_ids"])
            == ["native-probe-1", "native-probe-2"]
        )
        return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
