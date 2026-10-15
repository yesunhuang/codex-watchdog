import ast
import hashlib
import json
import sqlite3

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.current_courier_snapshot import CURRENT_COURIER_SOURCE
from codex_watchdog.linux_owner import node_has_queued_wake


SID = "11111111-2222-4333-8444-555555555555"
QID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    with sqlite3.connect(path / "queue_1.sqlite") as db:
        db.executescript("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT,payload_json TEXT);"
                         "CREATE INDEX queued_items_thread_order_idx ON queued_items(thread_id,id);")
    return path


def receipt(home, qid=QID, instruction="slackbot:fixture"):
    digest = hashlib.sha256(b"fixture body").hexdigest()
    value = dict(thread_id=SID, queue_message_id=qid, instruction_id=instruction,
                 prompt_sha256=digest, state="enqueued", unknown="retained")
    path = cs.control_state_home(home) / "remote-wake" / (hashlib.sha256(instruction.encode()).hexdigest() + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    text = "[CODEX_WATCHDOG_WAKE id=%s sha256=%s]\nfixture body" % (instruction, digest)
    payload = json.dumps({"UserInput": {"client_id": OTHER, "content": [{"type": "text", "text": text}]}})
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("INSERT INTO queued_items VALUES(?,?,?)", (qid, SID, payload))
    return value, path


def test_current_queue_association_preserves_receipt_and_native_fifo(home):
    value, path = receipt(home)
    before = path.read_bytes()
    assert node_has_queued_wake(home, SID) is False
    assert cs.control_publish_courier_association(home, value)["status"] == "published"
    assert node_has_queued_wake(home, SID) is True
    assert cs.control_publish_courier_association(home, value)["status"] == "duplicate"
    assert path.read_bytes() == before
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        assert db.execute("SELECT id FROM queued_items").fetchall() == [(QID,)]
        db.execute("DELETE FROM queued_items WHERE id=?", (QID,))  # Native fixture consumer.
    assert node_has_queued_wake(home, SID) is False


def test_explicit_old_version_import_derives_only_actual_q_header(home):
    value, path = receipt(home)
    original = path.read_bytes()
    assert cs.control_import_current_couriers(home, SID)["imported"] == 1
    assert cs.control_import_current_couriers(home, SID)["imported"] == 0
    assert node_has_queued_wake(home, SID) is True
    assert path.read_bytes() == original


@pytest.mark.parametrize("field,bad", [
    ("thread_id", OTHER), ("queue_message_id", OTHER),
    ("prompt_sha256", "0" * 64), ("state", "uncertain"),
])
def test_wrong_or_uncertain_exact_receipt_cannot_authorize_import(home, field, bad):
    value, path = receipt(home)
    value[field] = bad
    path.write_text(json.dumps(value))
    original = path.read_bytes()
    assert cs.control_import_current_couriers(home, SID)["imported"] == 0
    assert node_has_queued_wake(home, SID) is False
    assert path.read_bytes() == original


def test_corrupt_point_cannot_starve_later_current_queue_association(home):
    bad, _ = receipt(home)
    cs.control_publish_courier_association(home, bad)
    cs.control_courier_path(home, SID, QID).write_text("{}")
    good, _ = receipt(home, OTHER, "git:fixture")
    cs.control_publish_courier_association(home, good)
    assert node_has_queued_wake(home, SID) is True


def test_same_id_changed_native_payload_does_not_authorize_resume(home):
    value, path = receipt(home)
    cs.control_publish_courier_association(home, value)
    before = path.read_bytes()
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("UPDATE queued_items SET payload_json=replace(payload_json,'fixture body','other body') WHERE id=?", (QID,))
    assert node_has_queued_wake(home, SID) is False
    assert path.read_bytes() == before


def test_cleanup_protects_actual_q_and_current_inflight_then_retires_only_point(home):
    value, original = receipt(home)
    cs.control_publish_courier_association(home, value)
    cleanup = cs.ControlCourierCleanup(home, SID)
    assert cleanup.step()["status"] == "current_work_inventory_required"
    assert cleanup.step(protected_queue_ids=[])["removed"] == 0
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("DELETE FROM queued_items WHERE id=?", (QID,))
    assert cleanup.step(protected_queue_ids=[QID])["removed"] == 0
    assert cleanup.step(protected_queue_ids=[])["removed"] == 1
    assert original.exists()  # Existing uncertainty/claims/history are A's retention scope.


def test_cleanup_takes_publication_lock_before_native_queue_snapshot(home, monkeypatch):
    value, _ = receipt(home)
    cs.control_publish_courier_association(home, value)
    original = cs.control_courier_queue
    checked = []
    def checked_queue(*args, **kwargs):
        with pytest.raises(cs.ControlBusy):
            cs.control_publish_courier_association(home, value)
        checked.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(cs, "control_courier_queue", checked_queue)
    assert cs.ControlCourierCleanup(home, SID).step(protected_queue_ids=[])["removed"] == 0
    assert checked == [True]


def test_current_node_collision_and_restart_keep_original_points(home, monkeypatch):
    value, path = receipt(home)
    original = path.read_bytes()
    cs.control_publish_courier_association(home, value)
    point = cs.control_courier_path(home, SID, QID)
    raw = point.read_bytes()
    monkeypatch.setattr(cs, "control_node_name", lambda: "other-node")
    assert node_has_queued_wake(home, SID) is False
    with pytest.raises(cs.ControlError, match="association_collision"):
        cs.control_publish_courier_association(home, value)
    assert point.read_bytes() == raw and path.read_bytes() == original


def test_boolean_schema_point_never_authorizes_collision_or_cleanup(home):
    value, _ = receipt(home)
    cs.control_publish_courier_association(home, value)
    point = cs.control_courier_path(home, SID, QID)
    record = json.loads(point.read_text())
    record["schema_version"] = True
    point.write_text(json.dumps(record))
    raw = point.read_bytes()
    assert node_has_queued_wake(home, SID) is False
    with pytest.raises(cs.ControlError, match="association_collision"):
        cs.control_publish_courier_association(home, value)
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("DELETE FROM queued_items WHERE id=?", (QID,))
    assert cs.ControlCourierCleanup(home, SID).step(protected_queue_ids=[])["removed"] == 0
    assert point.read_bytes() == raw


def test_publication_crash_remains_missing_not_false_resume(home, monkeypatch):
    value, _ = receipt(home)
    def fail(*args):
        raise OSError("fixture crash before replace")
    monkeypatch.setattr(cs, "control_atomic_json", fail)
    with pytest.raises(OSError):
        cs.control_publish_courier_association(home, value)
    assert node_has_queued_wake(home, SID) is False


def test_missing_or_nonindexed_native_queue_refuses(home):
    (home / "queue_1.sqlite").unlink()
    with pytest.raises(Exception, match="linux_queue_unavailable_or_ambiguous"):
        node_has_queued_wake(home, SID)
    with sqlite3.connect(home / "queue_1.sqlite") as db:
        db.execute("CREATE TABLE queued_items(id,thread_id,payload_json)")
    with pytest.raises(Exception, match="native_queue_index_required"):
        node_has_queued_wake(home, SID)


def test_embedded_source_keeps_python36_grammar():
    ast.parse(CURRENT_COURIER_SOURCE, feature_version=(3, 6))
