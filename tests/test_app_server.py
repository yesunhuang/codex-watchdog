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


def start(tmp_path: Path, monkeypatch, script: str, stdout_wrapper=None):
    path = tmp_path / "fake server.py"
    path.write_text(script, encoding="utf-8")
    popen = subprocess.Popen
    def launch(argv, **kw):
        process = popen([sys.executable, "-u", str(path)], **kw)
        if stdout_wrapper is not None:
            process.stdout = stdout_wrapper(process.stdout)
        return process
    monkeypatch.setattr(app_server.subprocess, "Popen", launch)
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


@pytest.mark.parametrize("output", ["not-json", '"scalar"', "x" * (1024 * 1024 + 1), "[" * 4000],
                         ids=["invalid_json", "non_object", "oversized", "deep_json"])
def test_bad_frame_does_not_disable_following_control_or_allow_ambiguous_writes(tmp_path, monkeypatch, output):
    payload = tmp_path / "output.txt"
    payload.write_text(output)
    client, _ = start(tmp_path, monkeypatch, '''
import pathlib,sys,json
r=json.loads(sys.stdin.readline())
print(pathlib.Path("output.txt").read_text(), flush=True)
print(json.dumps({"id":r["id"],"result":{"ok":True}}), flush=True)
for line in sys.stdin:
    r=json.loads(line)
    print(json.dumps({"id":r["id"],"result":{"ok":True}}), flush=True)
''')
    try:
        assert client.request("initialize", {}, timeout=3)=={"ok":True}
        assert client.readers[0].is_alive() and not client.failed.is_set()
        generation, reason = client.recovery_snapshot()
        assert reason == "app_server_frame_discarded"
        before = client.next_id
        with pytest.raises(AppServerError, match="recovery_required"):
            client.request("thread/resume", {}, timeout=3)
        assert client.next_id == before
        assert client.request("thread/read", {}, timeout=3)=={"ok":True}
        assert client.observation_verified(generation)
        assert client.recovery_snapshot()[1] is None
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


def test_large_disposable_frame_preserves_completion_and_response(tmp_path, monkeypatch):
    client, events = start(tmp_path, monkeypatch, '''
import json,sys
r=json.loads(sys.stdin.readline())
print(json.dumps({"method":"codex/event/item_completed","params":{"private":"x"*(3*1024*1024)}}),flush=True)
print(json.dumps({"method":"turn/completed","params":{"threadId":"same-thread","turn":{"id":"same-turn"}}}),flush=True)
print(json.dumps({"id":r["id"],"result":{"ok":True}}),flush=True)
sys.stdin.read()
''')
    try:
        assert client.request("thread/read", {}, timeout=5)=={"ok":True}
        assert events==[{"method":"turn/completed","params":{"threadId":"same-thread","turn":{"id":"same-turn"}}}]
        assert client.recovery_snapshot()[1]=="app_server_frame_discarded"
        assert not client.failed.is_set() and client.messages.empty()
    finally:
        client.close()


def test_queue_backpressure_preserves_backlog_and_reader(tmp_path, monkeypatch):
    client, events = start(tmp_path, monkeypatch, '''
import json,sys
for i in range(300):
    print(json.dumps({"method":"turn/started","params":{"n":i}}),flush=True)
print(json.dumps({"method":"turn/completed","params":{}}),flush=True)
r=json.loads(sys.stdin.readline())
print(json.dumps({"id":r["id"],"result":{"ok":True}}),flush=True)
sys.stdin.read()
''')
    try:
        deadline=time.monotonic()+5
        while client.recovery_snapshot()[1] is None and time.monotonic()<deadline:
            time.sleep(.01)
        assert client.recovery_snapshot()[1]=="app_server_queue_backpressure"
        assert client.messages.qsize()<=128 and client.readers[0].is_alive()
        assert client.request("thread/read", {}, timeout=5)=={"ok":True}
        assert [e["params"]["n"] for e in events[:-1]]==list(range(300))
        assert events[-1]["method"]=="turn/completed"
        assert not client.failed.is_set()
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["transient", "closed", "unusable"])
def test_transient_read_error_quarantines_boundary_and_recovers(tmp_path, monkeypatch, failure):
    class Interrupted:
        def __init__(self, stream):self.stream=stream;self.first=True
        def __getattr__(self, name):return getattr(self.stream,name)
        def readline(self, size):
            if self.first or failure == "unusable":
                self.first=False
                if failure == "closed":
                    self.stream.close()
                raise OSError("synthetic private transport detail")
            return self.stream.readline(size)
    client, events = start(tmp_path, monkeypatch, '''
import json,sys
r=json.loads(sys.stdin.readline())
print('quarantined partial frame',flush=True)
print(json.dumps({"method":"turn/completed","params":{}}),flush=True)
print(json.dumps({"id":r["id"],"result":{"ok":True}}),flush=True)
sys.stdin.read()
''', stdout_wrapper=Interrupted)
    try:
        assert client.request("thread/read", {}, timeout=5)=={"ok":True}
        assert events==[{"method":"turn/completed","params":{}}]
        assert client.recovery_snapshot()[1]=="app_server_read_error"
        assert not client.failed.is_set() and client.readers[0].is_alive()
    finally:
        client.close()


def test_late_response_never_satisfies_a_new_request(tmp_path, monkeypatch):
    client, _ = start(tmp_path, monkeypatch, '''
import json,sys,time
r=json.loads(sys.stdin.readline());time.sleep(.3)
print(json.dumps({"id":r["id"],"result":{"old":True}}),flush=True)
for line in sys.stdin:
    r=json.loads(line);print(json.dumps({"id":r["id"],"result":{"current":True}}),flush=True)
''')
    try:
        with pytest.raises(AppServerError, match="request_timeout"):
            client.request("thread/read", {}, timeout=.1)
        assert client.request("thread/read", {}, timeout=3)=={"current":True}
        assert client.recovery_snapshot()[1]=="app_server_unexpected_response"
        generation,_=client.recovery_snapshot()
        assert client.request("thread/read", {}, timeout=3)=={"current":True}
        assert client.observation_verified(generation)
    finally:
        client.close()


def test_content_pressure_is_discarded_without_decoding_payload(tmp_path, monkeypatch):
    original = json.loads
    def envelope_only(value, *args, **kwargs):
        if isinstance(value, bytes):
            assert b"DISPOSABLE_PAYLOAD" not in value
        return original(value, *args, **kwargs)
    monkeypatch.setattr(app_server.json, "loads", envelope_only)
    client, events = start(tmp_path, monkeypatch, '''
import json,sys
r=json.loads(sys.stdin.readline())
for i in range(300):
    print(json.dumps({"method":"item/agentMessage/delta","params":{"text":"DISPOSABLE_PAYLOAD"*4096}}),flush=True)
print(json.dumps({"method":"turn/completed","params":{"threadId":"exact","turn":{"id":"done"}}}),flush=True)
print(json.dumps({"id":r["id"],"result":{"ok":True}}),flush=True)
sys.stdin.read()
''')
    try:
        assert client.request("thread/read", {}, timeout=10) == {"ok": True}
        assert len(events) == 1 and events[0]["method"] == "turn/completed"
        assert client.recovery_snapshot()[1] is None and client.messages.empty()
    finally:
        client.close()


def test_status_backlog_keeps_latest_without_evicting_completion(tmp_path, monkeypatch):
    client, events = start(tmp_path, monkeypatch, '''
import json,sys
for i in range(1000):
    print(json.dumps({"method":"thread/status/changed","params":{"threadId":"exact","status":{"type":str(i)}}}),flush=True)
print(json.dumps({"method":"turn/completed","params":{"threadId":"exact"}}),flush=True)
r=json.loads(sys.stdin.readline())
print(json.dumps({"id":r["id"],"result":{"ok":True}}),flush=True)
sys.stdin.read()
''')
    try:
        deadline = time.monotonic() + 5
        while client.messages.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert client.messages.qsize() == 2
        assert client.request("thread/read", {}, timeout=3) == {"ok": True}
        assert len(events) == 2
        assert events[0]["params"]["status"]["type"] == "999"
        assert events[1]["method"] == "turn/completed"
        assert client.recovery_snapshot()[1] is None
    finally:
        client.close()


@pytest.mark.parametrize("encoded", [
    b'{"method":"unknown","params":{"id":"content"},"id":"request"}',
    b'{"params":{"method":"content"},"method":"turn/completed"}',
    b'{"method":"item/commandExecution/requestApproval","params":{},"id":"approval"}',
])
def test_content_discard_never_hides_reliable_envelope_fields(encoded):
    assert not app_server._discardable_notification(encoded)


def test_status_coalescing_preserves_reliable_order_and_other_threads():
    pending = app_server._ControlQueue(maxsize=5)
    def status(thread, value):
        return {"method":"thread/status/changed","params":{"threadId":thread,"status":{"type":value}}}
    completion = {"method":"turn/completed","params":{"threadId":"a"}}
    response = {"id":4,"result":{}}
    pending.put(status("a", "old"));pending.put(status("b", "idle"))
    pending.put(completion);pending.put(response);pending.put(status("a", "active"))
    assert pending.qsize() == 4
    assert [pending.get_nowait() for _ in range(4)] == [status("b", "idle"), completion, response, status("a", "active")]
