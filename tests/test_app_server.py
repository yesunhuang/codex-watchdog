from __future__ import annotations

import json
import queue
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from codex_watchdog import app_server
from codex_watchdog.app_server import AppServerError, StdioAppServer


def start(tmp_path: Path, monkeypatch, script: str):
    path = tmp_path / "fake server.py"
    path.write_text(script, encoding="utf-8")
    popen = subprocess.Popen
    monkeypatch.setattr(app_server.subprocess, "Popen", lambda argv, **kw: popen([sys.executable, "-u", str(path)], **kw))
    events = []
    client = StdioAppServer("unused", tmp_path, tmp_path, events.append)
    return client, events


def test_stdio_handles_approval_during_request_without_faking_consent(tmp_path, monkeypatch):
    client, events = start(tmp_path, monkeypatch, '''
import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({"id":"approval", "method":"item/commandExecution/requestApproval", "params":{"command":"private"}}), flush=True)
answer=json.loads(sys.stdin.readline())
print(json.dumps({"id":request["id"], "result":{"answer":answer}}), flush=True)
sys.stdin.read()
''')
    try:
        response = client.request("initialize", {})
        assert response["answer"] == {"id": "approval", "result": {"decision": "decline"}}
        assert events == [{"method": "watchdog/approvalRequired"}]
    finally:
        client.close()
    assert client.process.poll() is not None


def test_stdio_rejects_unknown_client_requests_and_discards_output(tmp_path, monkeypatch):
    client, events = start(tmp_path, monkeypatch, '''
import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({"method":"item/agentMessage/delta", "params":{"delta":"private reply"}}), flush=True)
print(json.dumps({"id":"approval", "method":"item/tool/requestUserInput", "params":{}}), flush=True)
answer=json.loads(sys.stdin.readline())
print(json.dumps({"id":request["id"], "result":{"answer":answer}}), flush=True)
sys.stdin.read()
''')
    try:
        response = client.request("initialize", {})
        assert response["answer"]["error"]["code"] == -32601
        assert events == [{"method": "watchdog/approvalRequired"}]
        assert client.messages.empty()
    finally:
        client.close()


@pytest.mark.parametrize("output", ["not-json", '"scalar"', "x" * (1024 * 1024 + 1)],
                         ids=["invalid_json", "non_object", "oversized"])
def test_malformed_or_oversized_transport_fails_closed(tmp_path, monkeypatch, output):
    payload = tmp_path / "output.txt"
    payload.write_text(output)
    client, _ = start(tmp_path, monkeypatch, '''
import pathlib,sys
sys.stdin.readline()
print(pathlib.Path("output.txt").read_text(), flush=True)
sys.stdin.read()
''')
    try:
        with pytest.raises(AppServerError, match="protocol_failed"):
            client.request("initialize", {}, timeout=3)
    finally:
        client.close()


def test_server_eof_is_not_retried(tmp_path, monkeypatch):
    client, _ = start(tmp_path, monkeypatch, "import sys\nsys.stdin.readline()\n")
    try:
        with pytest.raises(AppServerError, match="exited"):
            client.request("initialize", {}, timeout=3)
        assert client.next_id == 1
    finally:
        client.close()


def test_pump_drains_pending_status_events_before_owner_checks_idle():
    client = object.__new__(StdioAppServer)
    client.messages = queue.Queue(maxsize=128)
    client.failed = threading.Event()
    events = []
    client.on_event = events.append
    for state in ("active", "idle", "active", "idle"):
        client.messages.put({"method": "thread/status/changed", "params": {"status": {"type": state}}})
    client.pump(timeout=0)
    assert [event["params"]["status"]["type"] for event in events] == ["active", "idle", "active", "idle"]
    assert client.messages.empty()
