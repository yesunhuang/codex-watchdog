"""Bounded stdio client for retaining an existing first-party Codex thread.

No prompt generation, model selection, automatic consent, or reconnect retry.
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


class AppServerError(RuntimeError):
    pass


class StdioAppServer:
    def __init__(self, executable: str, codex_home: Path, cwd: Path,
                 on_event: Callable[[Dict[str, Any]], None]) -> None:
        self.on_event = on_event
        self.process = subprocess.Popen(
            [executable, "-c", "features.code_mode_host=true", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd), env={**os.environ, "CODEX_HOME": str(codex_home)},
        )
        self.messages: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=128)
        self.failed = threading.Event()
        self.stopping = threading.Event()
        self.next_id = 0
        self.readers = [threading.Thread(target=target, daemon=True)
                        for target in (self._read, self._drain_stderr)]
        for reader in self.readers:
            reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while not self.stopping.is_set():
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    return
                if len(line) > 1024 * 1024:
                    raise ValueError()
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError()
                # Discard content-bearing notifications immediately. Do not journal
                # assistant output, tool output, prompts, or raw server errors.
                if "id" in value or value.get("method") in (
                    "thread/status/changed", "turn/started", "turn/completed",
                ):
                    self.messages.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            self.failed.set()

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
            raise AppServerError("app_server_unexpected_response")
        else:
            self.on_event(message)

    def request(self, method: str, params: Dict[str, Any], timeout: float = 30) -> Dict[str, Any]:
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
                return message["result"]
            self._event(message)
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
