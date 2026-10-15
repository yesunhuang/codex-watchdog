"""Cold snapshots preserve retention boundaries and live pins without writes."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

import pytest

from codex_watchdog import relay_authority_migration as migration


SID = "11111111-2222-4333-8444-555555555555"
NS = "slack/C12345678/relay-state.json"


def snapshot(version=3):
    records = [dict(namespace=NS, kind="events", key="event", fingerprint=None,
        thread_id=SID, active=1, created_at="2026-10-01T00:00:00Z",
        value=json.dumps(dict(thread_id=SID, state="uncertain", instruction_id="slackbot:fixture"))),
        dict(namespace=NS, kind="session_acl", key="acl", fingerprint=None,
        thread_id=SID, active=0, created_at=None,
        value=json.dumps(dict(thread_id=SID, future_setting="preserve", allowed_users=["U12345678"])))]
    return dict(schema_version=1, source_schema=version, namespaces=[NS], records=records,
        recent_records=[dict(namespace=NS, kind="events", key="event", born=100,
            expires=259300, protected=1, session=SID)] if version == 3 else [],
        current_boundaries=[dict(namespace=NS, session="", floor=0, checked_at=600),
            dict(namespace=NS, session=SID, floor=50, checked_at=600)] if version == 3 else [])


@pytest.mark.parametrize("version", [2, 3])
def test_lossless_roundtrip_and_existing_destination_idempotence(tmp_path, version):
    value = snapshot(version)
    path = tmp_path/"state.sqlite3"
    digest = migration.write_authority_snapshot(path, value)
    before = path.read_bytes()
    loaded = migration.read_authority_snapshot(path)
    assert migration.snapshot_digest(loaded) == digest
    assert migration.write_authority_snapshot(path, value) == digest
    assert path.read_bytes() == before
    assert json.loads(loaded["records"][1]["value"])["future_setting"] == "preserve"
    assert loaded["source_schema"] == version
    assert loaded["recent_records"] == value["recent_records"]
    assert loaded["current_boundaries"] == value["current_boundaries"]
    rows, flat_digest = migration.read_snapshot(path)
    assert rows == loaded["records"]
    if version == 2:
        old = json.dumps(dict(schema_version=2, namespaces=loaded["namespaces"], rows=rows),
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        assert flat_digest == hashlib.sha256(old.encode()).hexdigest()
    else:
        assert flat_digest == digest


@pytest.mark.parametrize("field,change", [
    ("recent_records", lambda row: row.update(born=101)),
    ("recent_records", lambda row: row.update(expires=259301)),
    ("recent_records", lambda row: row.update(protected=0)),
    ("current_boundaries", lambda row: row.update(checked_at=601)),
    ("current_boundaries", lambda row: row.update(floor=1)),
])
def test_every_retention_field_changes_logical_digest(tmp_path, field, change):
    original = snapshot()
    changed = deepcopy(original)
    change(changed[field][0])
    assert migration.snapshot_digest(changed) != migration.snapshot_digest(original)
    migration.write_authority_snapshot(tmp_path/"changed.sqlite3", changed)


@pytest.mark.parametrize("mutate", [
    lambda value: value["recent_records"].clear(),
    lambda value: value["recent_records"][0].update(session="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    lambda value: value["recent_records"][0].update(key="missing"),
    lambda value: value["recent_records"][0].update(protected=True),
    lambda value: value["recent_records"][0].update(born=float("nan")),
    lambda value: value["recent_records"][0].update(expires=-1),
    lambda value: value["current_boundaries"][0].update(checked_at=float("inf")),
    lambda value: value["current_boundaries"][0].update(floor=601),
    lambda value: value["current_boundaries"].append(dict(value["current_boundaries"][0])),
    lambda value: value["namespaces"].clear(),
    lambda value: value.update(unknown_persistent_state=[]),
])
def test_bad_snapshot_refuses_before_destination_creation(tmp_path, mutate):
    value = snapshot()
    mutate(value)
    path = tmp_path/"state.sqlite3"
    with pytest.raises(ValueError):
        migration.write_authority_snapshot(path, value)
    assert not path.exists()


def test_valid_but_different_destination_is_not_replaced(tmp_path):
    path = tmp_path/"state.sqlite3"
    migration.write_authority_snapshot(path, snapshot())
    before = path.read_bytes()
    value = snapshot()
    value["recent_records"][0]["protected"] = 0
    with pytest.raises(ValueError, match="destination_exists"):
        migration.write_authority_snapshot(path, value)
    assert path.read_bytes() == before


def test_wal_snapshot_includes_retention_and_pin_changes(tmp_path):
    path = tmp_path/"state.sqlite3"
    migration.write_authority_snapshot(path, snapshot())
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        before = migration.snapshot_digest(migration.read_authority_snapshot(path))
        writer.execute("UPDATE recent_records SET protected=0")
        writer.execute("UPDATE current_boundaries SET checked_at=800")
        writer.commit()
        main_bytes = path.read_bytes()
        value = migration.read_authority_snapshot(path)
        assert value["recent_records"][0]["protected"] == 0
        assert all(row["checked_at"] == 800 for row in value["current_boundaries"])
        assert migration.snapshot_digest(value) != before
        assert path.read_bytes() == main_bytes


@pytest.mark.parametrize("object_sql", ["CREATE TABLE unknown_authorization(value TEXT)",
    "CREATE TABLE sqliteX_authorization(value TEXT)",
    "CREATE VIEW sqliteX_authorization AS SELECT * FROM records",
    "CREATE VIEW hidden_auth AS SELECT * FROM records",
    "CREATE TRIGGER hidden_auth AFTER DELETE ON records BEGIN SELECT 1; END"])
def test_unknown_persistent_semantics_refuse_without_source_repair(tmp_path, object_sql):
    path = tmp_path/"state.sqlite3"
    migration.write_authority_snapshot(path, snapshot())
    with sqlite3.connect(path) as writer:
        writer.execute(object_sql)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="snapshot_invalid"):
        migration.read_authority_snapshot(path)
    assert path.read_bytes() == before


def test_crash_before_publish_retains_source_and_retry_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path/"state.sqlite3"
    link = os.link
    monkeypatch.setattr(migration.os, "link", lambda *args: (_ for _ in ()).throw(OSError("fixture crash")))
    with pytest.raises(OSError):
        migration.write_authority_snapshot(path, snapshot())
    assert not path.exists() and list(tmp_path.iterdir()) == []
    monkeypatch.setattr(migration.os, "link", link)
    migration.write_authority_snapshot(path, snapshot())
    assert migration.read_authority_snapshot(path)["recent_records"][0]["protected"] == 1


def test_crash_after_publish_never_overwrites_or_resets_a_pin(tmp_path, monkeypatch):
    path = tmp_path/"state.sqlite3"
    link = os.link
    def crash_after(source, target):
        link(source, target)
        raise OSError("fixture crash after publication")
    monkeypatch.setattr(migration.os, "link", crash_after)
    with pytest.raises(OSError):
        migration.write_authority_snapshot(path, snapshot())
    before = path.read_bytes()
    monkeypatch.setattr(migration.os, "link", link)
    migration.write_authority_snapshot(path, snapshot())
    assert path.read_bytes() == before


def test_alias_source_or_destination_is_never_followed(tmp_path):
    path = tmp_path/"state.sqlite3"
    migration.write_authority_snapshot(path, snapshot())
    alias = tmp_path/"alias.sqlite3"
    try:
        alias.symlink_to(path)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) in (50, 1314):
            pytest.skip("native symlink capability unavailable")
        raise
    before = path.read_bytes()
    with pytest.raises(ValueError):
        migration.read_authority_snapshot(alias)
    with pytest.raises(ValueError):
        migration.write_authority_snapshot(alias, snapshot())
    assert path.read_bytes() == before


def test_equal_timestamp_ticket_eviction_keeps_original_insertion_rank(tmp_path):
    from codex_watchdog.reply_tickets import ReplyTickets
    from codex_watchdog.relay import RelayTarget
    source = tmp_path/"source"
    copied = tmp_path/"copied"
    def journal(runtime):
        return ReplyTickets(runtime/"slack/poll-relay-state.json", runtime/"locks/test.lock",
                            "slack", runtime, lambda: {}, clock=lambda: 1000)
    original = journal(source)
    stamp = datetime.fromtimestamp(1000, timezone.utc).isoformat().replace("+00:00", "Z")
    def record(target, key):
        entry = dict(target=RelayTarget("fixture", SID, "process_local").to_dict(),
                     event_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
                     ticket_schema=1, created_at=stamp)
        with target.transaction() as db:
            target.record(db, key, entry)
    for key in ("z", "a", "b", "c"):
        record(original, key)
    state = migration.read_authority_snapshot(original.database)
    assert [key[2] for key in state["record_order"]] == ["z", "a", "b", "c"]
    destination = journal(copied)
    migration.write_authority_snapshot(destination.database, state)
    for target in (original, destination):
        record(target, "d")
        with target.transaction() as db:
            active = {row[0] for row in db.execute("SELECT key FROM records WHERE active=1 AND kind='threads'")}
        assert active == {"a", "b", "c", "d"}


@pytest.mark.parametrize("order", [[], [[NS, "events", "missing"], [NS, "session_acl", "acl"]],
    [[NS, "events", "event"], [NS, "events", "event"]]])
def test_invalid_creation_rank_is_not_silently_rebuilt(tmp_path, order):
    state = snapshot()
    state["record_order"] = order
    with pytest.raises(ValueError):
        migration.write_authority_snapshot(tmp_path/"copy.sqlite3", state)
