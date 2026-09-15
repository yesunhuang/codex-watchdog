"""Bounded stdio client for retaining an existing first-party Codex thread.

Observation can recover; ambiguous requests never authorize a repeated effect.
No prompt generation, model selection, or automatic consent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional

from .process_environment import codex_process_environment


class AppServerError(RuntimeError):
    pass


class StdioAppServer:
    def __init__(self, executable: str, codex_home: Path, cwd: Path,
                 on_event: Callable[[Dict[str, Any]], None]) -> None:
        self.on_event = on_event
        self.process = subprocess.Popen(
            [executable, "-c", "features.code_mode_host=true", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd), env=codex_process_environment(codex_home),
        )
        self.messages: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=128)
        self.failed = threading.Event()
        self.stopping = threading.Event()
        self.next_id = 0
        self._recovery_lock = threading.Lock()
        self._recovery_generation = 0
        self._recovery_reason = None
        self._last_read_generation = None
        self._request_lock = threading.Lock()
        # A duplicate read endpoint lets a broken Python stream be replaced
        # without taking the existing Codex writer away from its running turn.
        self._stdout_backup = os.dup(self.process.stdout.fileno())
        self.readers = [threading.Thread(target=target, daemon=True)
                        for target in (self._read, self._drain_stderr)]
        for reader in self.readers:
            reader.start()

    def recovery_snapshot(self):
        with self._recovery_lock:
            return self._recovery_generation, self._recovery_reason

    def _problem(self, reason):
        with self._recovery_lock:
            self._recovery_generation += 1
            self._recovery_reason = reason

    def observation_verified(self, generation):
        """Caller has checked the correlated thread/read identity and owner."""
        with self._recovery_lock:
            if generation != self._last_read_generation or generation != self._recovery_generation:
                return False
            self._recovery_reason = None
            return True

    def _read(self) -> None:
        assert self.process.stdout is not None
        draining = False
        delay = 0.1
        stalled = False
        while not self.stopping.is_set():
            try:
                line = self.process.stdout.readline(65536 if draining else 1024 * 1024 + 1)
            except (OSError, ValueError):
                if not stalled:
                    self._problem("app_server_read_error")
                stalled = True
                # The failed read's partial data is ambiguous. Quarantine it
                # through the next newline before accepting another frame.
                draining = True
                if self.process.stdout.closed or delay >= 0.2:
                    try:
                        self.process.stdout.close()
                        self.process.stdout = os.fdopen(os.dup(self._stdout_backup), "rb")
                    except (OSError, ValueError):
                        pass
                self.stopping.wait(delay)
                delay = min(2.0, delay * 2)
                continue
            if not line:
                if self.process.poll() is not None:
                    return
                if not stalled:
                    self._problem("app_server_read_eof")
                stalled = True
                self.stopping.wait(delay)
                delay = min(2.0, delay * 2)
                continue
            stalled = False
            delay = 0.1
            if draining:
                draining = not line.endswith(b"\n")
                continue
            if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
                self._problem("app_server_frame_discarded")
                draining = not line.endswith(b"\n")
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                self._problem("app_server_frame_discarded")
                continue
            # Content stays out of the queue and all recovery diagnostics.
            if "id" not in value and value.get("method") not in (
                "thread/status/changed", "turn/started", "turn/completed",
            ):
                continue
            pressure = False
            while not self.stopping.is_set():
                try:
                    self.messages.put(value, timeout=0.1)
                    break
                except queue.Full:
                    if not pressure:
                        self._problem("app_server_queue_backpressure")
                        pressure = True

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            while self.process.stderr.read(4096):
                pass
        except OSError:
            pass

    def _send(self, value: Dict[str, Any]) -> None:
        try:
            assert self.process.stdin is not None
            self.process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._problem("app_server_write_failed")
            raise AppServerError("app_server_write_failed") from exc

    def _get(self, timeout: float) -> Optional[Dict[str, Any]]:
        if self.failed.is_set():
            raise AppServerError("app_server_protocol_failed")
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            if self.process.poll() is not None:
                raise AppServerError("app_server_exited")
            return None

    def _event(self, message: Dict[str, Any]) -> None:
        if "id" in message and "method" in message:
            method = message["method"]
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self._send({"id": message["id"], "result": {"decision": "decline"}})
            else:
                self._send({"id": message["id"], "error": {
                    "code": -32601, "message": "unattended WatchDog client cannot answer this request",
                }})
            self.on_event({"method": "watchdog/approvalRequired"})
        elif "id" in message:
            # A timed-out request can finish late. It must never satisfy a new
            # request, but it also must not permanently kill future observation.
            self._problem("app_server_unexpected_response")
        else:
            self.on_event(message)

    def request(self, method: str, params: Dict[str, Any], timeout: float = 30) -> Dict[str, Any]:
        with self._request_lock:
            return self._request(method, params, timeout)

    def _request(self, method, params, timeout):
        generation, reason = self.recovery_snapshot()
        if reason is not None and method not in ("initialize", "thread/read"):
            raise AppServerError("app_server_recovery_required")
        self.next_id += 1
        expected = self.next_id
        self._send({"id": expected, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._get(min(0.2, max(0.001, deadline - time.monotonic())))
            if message is None:
                continue
            if message.get("id") == expected and "method" not in message:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise AppServerError("app_server_request_failed")
                if method == "thread/read":
                    self._last_read_generation = generation
                return message["result"]
            self._event(message)
        self._problem("app_server_request_timeout")
        raise AppServerError("app_server_request_timeout")

    def initialize(self) -> None:
        from . import __version__
        self.request("initialize", {
            "clientInfo": {"name": "codex_watchdog_linux", "version": __version__},
            "capabilities": {"experimentalApi": True},
        })
        self._send({"method": "initialized", "params": {}})

    def pump(self, timeout: float = 0.2) -> None:
        message = self._get(timeout)
        if message is not None:
            self._event(message)
        # A Linux observation interval must not throttle native status handling
        # to one event per cycle. Drain the bounded queue before using its state.
        for _ in range(self.messages.maxsize):
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self._event(message)

    def close(self) -> None:
        self.stopping.set()
        assert self.process.stdin is not None
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # This client owns this exact child only. No PID/name-based killing.
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        os.close(self._stdout_backup)
