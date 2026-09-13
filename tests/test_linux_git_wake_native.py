"""Native courier/owner regression with disposable Git, state and protocol peers."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.linux_auto import LinuxAutoWatchdog
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="native Linux process and lock proof")
THREAD = "11111111-2222-4333-8444-555555555555"
PEER = r'''import fcntl,json,os,pathlib,sqlite3,sys,uuid
home=pathlib.Path(os.environ["CODEX_HOME"])
if "queue" in sys.argv:
    thread=sys.argv[sys.argv.index("--thread")+1]
    message=sys.argv[sys.argv.index("--message")+1]
    item=str(uuid.uuid4())
    with sqlite3.connect(home/"queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES (?,?,?)",(item,thread,json.dumps({"message":message})))
        db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?",(thread,))
    with (home/"calls.jsonl").open("a") as log: log.write(json.dumps({"thread":thread,"message":message})+"\n")
    print("Queued message "+item+" for thread "+thread+".")
    raise SystemExit(0)
lock=None
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    method=request["method"]; result={}
    if method in ("thread/read","thread/resume"):
        thread=request["params"]["threadId"]
        with sqlite3.connect(home/"state_5.sqlite") as db:
            cwd,rollout=db.execute("SELECT cwd,rollout_path FROM threads WHERE id=?",(thread,)).fetchone()
        if method=="thread/resume":
            lock=(home/"thread-writer-locks"/(thread+".lock")).open("a+b")
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with sqlite3.connect(home/"queue_1.sqlite") as db:
                items=db.execute("SELECT id,payload_json FROM queued_items WHERE thread_id=?",(thread,)).fetchall()
                for item,payload in items:
                    turn=str(uuid.uuid4()); message=json.loads(payload)["message"]
                    with open(rollout,"a") as log:
                        for event in ({"type":"task_started","turn_id":turn},
                                      {"type":"item_completed","turn_id":turn,"item":{"type":"UserMessage","content":[{"text":message}]}}):
                            log.write(json.dumps({"type":"event_msg","payload":event})+"\n")
                    db.execute("DELETE FROM queued_items WHERE id=?",(item,))
                    db.execute("UPDATE queued_thread_revisions SET revision=revision+1 WHERE thread_id=?",(thread,))
        result={"thread":{"id":thread,"cwd":cwd,"status":{"type":"idle"}}}
    elif method!="initialize": raise RuntimeError("unexpected method")
    print(json.dumps({"id":request["id"],"result":result}),flush=True)
'''


def test_parked_git_blocker_reaches_same_thread_once_with_native_processes(tmp_path, monkeypatch):
    home, repo = tmp_path / "codex", tmp_path / "project"
    repo.mkdir()
    (home / "sessions").mkdir(parents=True)
    (home / "thread-writer-locks").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for args in (("init", "-b", "main"), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--allow-empty", "-m", "fixture"),
                 ("remote", "add", "origin", str(tmp_path / "unavailable.git")),
                 ("config", "branch.main.remote", "origin"),
                 ("config", "branch.main.merge", "refs/heads/main")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    rollout = home / "sessions/thread.jsonl"
    rollout.write_text("{}\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id,cwd,source,thread_source,archived,rollout_path)")
        db.execute("INSERT INTO threads VALUES (?,?,'vscode','user',0,?)", (THREAD, str(repo), str(rollout)))
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id,thread_id,payload_json)")
        db.execute("CREATE TABLE queued_thread_revisions(thread_id,revision)")
        db.execute("INSERT INTO queued_thread_revisions VALUES (?,0)", (THREAD,))
    node = cs.control_node_directory(home)
    cs.control_atomic_json(node / "node.json", dict(schema_version=1, node=cs.control_node_name(), codex_home=str(home)))
    peer = tmp_path / "fixture-codex"
    peer.write_text("#!" + sys.executable + "\n" + PEER)
    peer.chmod(0o700)
    lock, ready, release = home / "thread-writer-locks" / (THREAD + ".lock"), tmp_path / "ready", tmp_path / "release"
    child = ('import fcntl,pathlib,sys,time\n'
             'lock,ready,release=map(pathlib.Path,sys.argv[1:4])\n'
             'with lock.open("a+b") as handle:\n'
             ' fcntl.flock(handle,fcntl.LOCK_EX)\n ready.touch()\n'
             ' while not release.exists(): time.sleep(0.02)\n')
    parent_code = 'import subprocess,sys; raise SystemExit(subprocess.call([sys.executable,"-c",' + repr(child) + ',*sys.argv[1:4],"app-server"]))'
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(lock), str(ready), str(release), "--type=extensionHost"])
    dog = LinuxAutoWatchdog(node / "runtime", home, executable=str(peer),
        service_factory=lambda runtime, **kw: MvpWatchdogService(runtime,
            notifier=EnvironmentNotifier(runtime, config=NotificationConfig()), **kw))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        assert dog.step(observe=False)[-1]["state"] == "observing"
        item = dog.controllers[THREAD]
        item["service"].remote_ssh_adapter.namespace["codex_executable"] = lambda: str(peer)
        release.touch()
        assert parent.wait(timeout=5) == 0
        assert dog.step(observe=False)[-1]["state"] == "owned"
        item["owner"]._idle_since -= 6
        assert dog.step(observe=False)[-1]["state"] == "parked"
        assert item["owner"].client is None
        assert dog.step()[-1]["state"] == "parked"
        saved = item["store"].read()["remote_state"]
        assert saved["last_git_attention"].startswith("git-attention:")
        assert saved["pending_remote_oid"] is None
        assert saved["pending_instruction_id"] == saved["last_git_attention"]
        assert dog.step()[-1]["state"] == "owned"
        assert item["store"].read()["remote_state"]["pending_instruction_id"] is None
        assert dog.step()[-1]["state"] == "owned"
        calls = [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]
        assert len(calls) == 1 and calls[0]["thread"] == THREAD
        assert "Git attention" in calls[0]["message"]
    finally:
        release.touch()
        parent.wait(timeout=10)
        for item in dog.controllers.values():
            if item["owner"].client is not None:
                item["owner"].client.close()
            item["locks"].close()
