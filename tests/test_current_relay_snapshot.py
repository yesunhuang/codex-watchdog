import ast
import base64
from datetime import datetime, timezone
import hashlib
import json

import pytest

from codex_watchdog import control_state as cs
from codex_watchdog.current_relay_snapshot import CURRENT_RELAY_SOURCE
from test_remote_control import helper as remote_helper
from codex_watchdog.remote_control import RemoteControlClient


SID = "11111111-2222-4333-8444-555555555555"
NOW = 1800000000.0


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.time, "time", lambda: NOW)
    return cs.ControlStore(tmp_path / "home", SID, tmp_path / "repo", boot_id="fixture")


def entry(n, provider="slack", created=None):
    value = dict(ticket_schema=1, created_at=stamp(NOW - 20 + n if created is None else created),
                 event_fingerprint=hashlib.sha256(str(n).encode()).hexdigest())
    if provider == "slack":
        value.update(channel_id="C12345678", thread_ts="1799999%03d.000001" % n)
    elif provider == "lark":
        value.update(provider=provider, scope="a" * 64, chat_id="oc_fixture123", message_id="om_fixture%03d" % n)
    else:
        value.update(provider=provider, scope="a" * 64, chat_id="private:42", message_id=str(n))
    return value


def merge(store, values):
    with cs.control_file_lock(store.lock_path):
        return store.merge_relay_mappings({}, values)


def test_n4_per_provider_immutable_creation_expiry_and_no_resurrection(store):
    for provider in ("slack", "lark", "onebot"):
        merge(store, [entry(n, provider) for n in range(5)])
    mappings = store.relay_mappings()
    assert len(mappings) == 12
    merge(store, [entry(0)])  # Evicted callback cannot restore the old parent.
    assert len(store.slack_mappings()) == 4
    before = cs.control_mapping_path(store).read_bytes()
    merge(store, [entry(4)])
    assert cs.control_mapping_path(store).read_bytes() == before
    assert {v["created_at"] for v in store.slack_mappings()} == {entry(n)["created_at"] for n in range(1, 5)}
    assert cs.control_current_mappings(store, NOW + 72 * 3600) == []
    merge(store, [entry(4)])  # Simulated rollback clock retains expired floor.
    assert store.relay_mappings() == []


def test_close_newest_preserves_older_active_and_late_duplicate_stays_closed(store):
    merge(store, [entry(n) for n in range(4)])
    key = cs.control_mapping_identity(store, entry(3))[1]
    store.retire_relay_mappings("slack", [key])
    assert len(store.slack_mappings()) == 3
    merge(store, [entry(3)])
    assert len(store.slack_mappings()) == 3
    merge(store, [entry(4)])
    assert len(store.slack_mappings()) == 4


def test_close_before_late_mapping_publication_requires_actual_creation_proof(store):
    pending = entry(1)
    key = cs.control_mapping_identity(store, pending)[1]
    with pytest.raises(cs.ControlError, match="retirement_timestamp_required"):
        store.retire_relay_mappings("slack", [key])
    store.retire_relay_mappings("slack", [key], created_at_by_key={key: pending["created_at"]})
    merge(store, [pending])
    assert store.slack_mappings() == []


def test_import_fences_recent_closed_callbacks_including_empty_providers(store):
    store.import_current_mappings(NOW, [entry(1)])
    clone = cs.ControlStore(store.codex_home, SID, store.repo_path, boot_id="another-boot")
    merge(clone, [entry(2), entry(2, "lark"), entry(2, "onebot")])
    assert clone.relay_mappings() == [dict(schema_version=1, **entry(1))]


def test_import_publishes_active_exceptions_floor_and_marker_in_one_commit(store, monkeypatch):
    calls = []
    original = cs.control_atomic_json
    def capture(path, value):
        calls.append(json.loads(json.dumps(value)))
        original(path, value)
    monkeypatch.setattr(cs, "control_atomic_json", capture)
    store.import_current_mappings(NOW, [entry(1)])
    assert len(calls) == 1
    assert calls[0]["legacy_import_complete"] is True
    assert all(state["floor"] == [NOW, "~"] for state in calls[0]["providers"].values())


def test_import_crash_before_atomic_publish_leaves_sources_and_can_retry(store, monkeypatch):
    original = cs.control_atomic_json
    def fail(*args):
        raise OSError("fixture import before publication")
    monkeypatch.setattr(cs, "control_atomic_json", fail)
    with pytest.raises(OSError):
        store.import_current_mappings(NOW, [entry(1)])
    assert not cs.control_mapping_path(store).exists()
    monkeypatch.setattr(cs, "control_atomic_json", original)
    assert store.import_current_mappings(NOW, [entry(1)])["active"] == 1


@pytest.mark.parametrize("age,eligible", [(72*3600-1, True), (72*3600, False), (72*3600+1, False)])
def test_absolute_age_boundary(store, age, eligible):
    merge(store, [entry(1, created=NOW-age)])
    assert bool(store.slack_mappings()) is eligible


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(created_at="2026-02-30T00:00:00Z"),
    lambda v: v.update(created_at=stamp(NOW + 1)),
    lambda v: v.update(thread_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    lambda v: v.update(ticket_schema=True),
    lambda v: v.update(activation_generation=True),
])
def test_invalid_callback_cannot_mutate_snapshot(store, mutate):
    merge(store, [entry(1)])
    path = cs.control_mapping_path(store)
    before = path.read_bytes()
    value = entry(2)
    mutate(value)
    with pytest.raises(cs.ControlError):
        merge(store, [value])
    assert path.read_bytes() == before


def test_collision_unknown_keys_and_restart_handoff_preserve_current_state(store):
    merge(store, [entry(1)])
    path = cs.control_mapping_path(store)
    value = json.loads(path.read_text())
    value["future"] = {"keep": True}
    value["providers"]["slack"]["entries"][0]["unknown"] = "keep"
    cs.control_atomic_json(path, value)
    clone = cs.ControlStore(store.codex_home, SID, store.repo_path, boot_id="changed-boot")
    merge(clone, [entry(1)])
    assert clone.slack_mappings()[0]["unknown"] == "keep"
    assert json.loads(path.read_text())["future"] == {"keep": True}
    different = dict(entry(1), created_at=stamp(NOW - 1))
    before = path.read_bytes()
    with pytest.raises(cs.ControlError, match="immutable_collision"):
        merge(clone, [different])
    assert path.read_bytes() == before


def test_atomic_write_failure_does_not_publish_partial_callback(store, monkeypatch):
    merge(store, [entry(1)])
    path = cs.control_mapping_path(store)
    original = path.read_bytes()
    def refuse(*args):
        raise OSError("fixture before commit")
    monkeypatch.setattr(cs, "control_atomic_json", refuse)
    assert merge(store, [entry(2), entry(3)])["status"] == "projection_unavailable"
    assert path.read_bytes() == original


def test_explicit_legacy_import_only_current_n4_and_cleanup_preserves_sources(store):
    directory = store.directory / "slack-threads"
    directory.mkdir(parents=True)
    originals = {}
    for n in range(6):
        value = dict(schema_version=1, **entry(n))
        key = cs.control_mapping_identity(store, value)[1]
        path = directory / (key + ".json")
        path.write_text(json.dumps(value))
        originals[path] = path.read_bytes()
    old = dict(schema_version=1, **entry(7, created=NOW - 72 * 3600))
    oldpath = directory / (cs.control_mapping_identity(store, old)[1] + ".json")
    oldpath.write_text(json.dumps(old))
    assert store.relay_mappings() == []  # No implicit historical import.
    with pytest.raises(cs.ControlError, match="active_import_required"):
        store.import_current_mappings(NOW)
    assert store.import_current_mappings(NOW, [entry(n) for n in range(2, 6)])["active"] == 4
    assert store.import_current_mappings(NOW, [])["status"] == "already_initialized"
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert store.cleanup_legacy_mappings(budget=16, now=NOW)["removed"] == 1
    assert not oldpath.exists()
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_no_new_live_ticket_from_legacy_envelope_without_creation(store):
    value = entry(1)
    value.pop("ticket_schema")
    value.pop("created_at")
    merge(store, [value])
    assert store.slack_mappings() == []


def test_snapshot_corruption_and_callback_cap_fail_closed(store):
    merge(store, [entry(1)])
    path = cs.control_mapping_path(store)
    path.write_text('{"schema_version":1,"providers":[]}')
    original = path.read_bytes()
    assert store.relay_mappings() == []
    assert store.relay_mapping_status == "control_current_mappings_invalid"
    assert path.read_bytes() == original
    with pytest.raises(cs.ControlError, match="callback_too_large"):
        merge(store, [entry(1)] * 13)


def test_embedded_source_keeps_supported_python36_grammar():
    ast.parse(CURRENT_RELAY_SOURCE, feature_version=(3, 6))


def test_cleanup_advances_in_stable_scheduler_but_recreated_rpc_has_no_progress_claim(store):
    store.import_current_mappings(NOW, [])
    directory = store.directory / "slack-threads"
    directory.mkdir()
    for n in range(20):
        (directory / ("corrupt%02d.json" % n)).write_text("{}")
    value = dict(schema_version=1, **entry(30, created=NOW-72*3600-1))
    oldpath = directory / (cs.control_mapping_identity(store, value)[1] + ".json")
    oldpath.write_text(json.dumps(value))
    # Deterministic dirent order, preserving actual filesystem objects.
    real = cs.os.scandir
    class Ordered:
        def __init__(self, path):
            with real(path) as iterator:
                rows = list(iterator)
            rows.sort(key=lambda row: (not row.name.startswith("corrupt"), row.name))
            self.rows = iter(rows)
        def __next__(self):
            return next(self.rows)
        def close(self):
            pass
    from unittest.mock import patch
    with patch.object(cs.os, "scandir", Ordered):
        for _ in range(2):
            clone = cs.ControlStore(store.codex_home, SID, store.repo_path, boot_id="rpc")
            assert clone.cleanup_legacy_mappings(16, NOW)["removed"] == 0
        assert oldpath.exists()
        assert store.cleanup_legacy_mappings(16, NOW)["removed"] == 0
        assert store.cleanup_legacy_mappings(16, NOW)["removed"] == 1
    assert not oldpath.exists()


def corrupt_projection(store, raw=b'{"schema_version":1,"providers":[]}'):
    path = cs.control_mapping_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def trusted_floors(now=NOW):
    return {provider: [now-100, "~"] for provider in ("slack", "lark", "onebot")}


def test_empty_merge_never_reads_or_replaces_corrupt_optional_projection(store, monkeypatch):
    path = corrupt_projection(store)
    original = path.read_bytes()
    def unexpected(*args):
        pytest.fail("empty callback touched optional projection")
    monkeypatch.setattr(cs, "control_mapping_load", unexpected)
    monkeypatch.setattr(cs, "control_atomic_json", unexpected)
    assert merge(store, []) == {"status": "no_op"}
    assert path.read_bytes() == original


@pytest.mark.parametrize("raw", [b"{", b"null", b'{"schema_version":2}',
                                  b"x"*131073, b"["*1500+b"]"*1500], ids=lambda value: "bytes-"+str(len(value))+"-"+hashlib.sha256(value).hexdigest()[:8])
def test_nonempty_callback_defers_only_corrupt_projection_without_reconstruction(store, raw):
    path = corrupt_projection(store, raw)
    assert merge(store, [entry(1)])["status"] == "projection_unavailable"
    assert store.relay_mapping_status.startswith("control_")
    assert path.read_bytes() == raw
    assert not (store.directory/"current-relay-mappings.reconstruction-backup").exists()


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(created_at=stamp(NOW+1)),
    lambda value: value.update(thread_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    lambda value: value.update(target=[]),
    lambda value: value.update(target={"thread_id":"wrong-session"}),
    lambda value: value.update(ticket_schema=True),
    lambda value: value.update(activation_generation=0),
    lambda value: value.update(thread_ts=1799999001.000001),
    lambda value: value.update(provider="lark", scope="bad"),
])
def test_incoming_validation_still_refuses_before_corrupt_projection(store, mutate):
    path = corrupt_projection(store)
    original = path.read_bytes()
    value = entry(1)
    mutate(value)
    with pytest.raises(cs.ControlError):
        merge(store, [value])
    assert path.read_bytes() == original


@pytest.mark.parametrize("active", [False, True])
def test_actual_remote_acquire_git_path_survives_corrupt_secondary_projection(remote_helper, tmp_path,
                                                                            monkeypatch, active):
    adapter, target, store, clock, writer = remote_helper
    monkeypatch.setattr(cs.time, "time", lambda: NOW)
    path = corrupt_projection(store)
    original = path.read_bytes()
    client = RemoteControlClient(adapter, tmp_path, instance="fixture", locality="fixture-host")
    mappings = [dict(entry(1), thread_id=SID)] if active else []
    result = client.acquire(target, {}, relay_mappings=mappings)
    assert result["status"] == "ok" and result["control"]["token"]["thread_id"] == SID
    assert result["git"]["head_oid"] == "a"*40
    assert path.read_bytes() == original
    # A new controller/store lifetime must not require historical repair first.
    assert client.acquire(target, {}, relay_mappings=mappings)["status"] == "ok"


def test_completed_send_receipt_is_truthful_despite_unavailable_mapping_and_never_replayed(store):
    token = store.attach("fixture", "fixture-host", "vscode")
    value = entry(1)
    fingerprint = value["event_fingerprint"]
    prepared = store.prepare_notification(token, fingerprint, fingerprint)
    path = corrupt_projection(store)
    original = path.read_bytes()
    finished = store.finish_notification(token, fingerprint, prepared["operation_id"],
                                         {"status":"sent"}, [value])
    assert finished["state"] == "completed" and finished["result"] == {"status":"sent"}
    assert finished["relay_mapping_status"] == "projection_unavailable"
    assert store.read()["external_effect"] is None and path.read_bytes() == original
    clone = cs.ControlStore(store.codex_home, SID, store.repo_path, boot_id="fixture")
    duplicate = clone.prepare_notification(token, fingerprint, fingerprint)
    assert duplicate["duplicate"] and duplicate["state"] == "completed"
    assert duplicate["operation_id"] == prepared["operation_id"]


def test_size_failure_of_optional_publication_preserves_unknown_snapshot_and_independent_callback(store):
    merge(store, [entry(1)])
    path = cs.control_mapping_path(store)
    value = json.loads(path.read_text())
    value["future"] = "x"*(131072-len(path.read_bytes())-20)
    cs.control_atomic_json(path, value)
    original = path.read_bytes()
    assert merge(store, [entry(2)])["status"] == "projection_unavailable"
    assert path.read_bytes() == original


def test_trusted_reconstruction_preserves_active_exceptions_generations_and_fences_late_callbacks(store):
    path = corrupt_projection(store)
    original = path.read_bytes()
    current = dict(entry(1), activation_generation=2, route_generation=3)
    floors = trusted_floors()
    floors["lark"] = [NOW+10, "~"]  # Preserve an authoritative floor across clock rollback.
    assert store.reconstruct_current_mappings([current], floors, NOW)["status"] == "reconstructed"
    snapshot = json.loads(path.read_text())
    assert snapshot["providers"]["lark"]["floor"] == [NOW+10, "~"]
    assert snapshot["providers"]["slack"]["floor"] == [NOW, "~"]
    assert snapshot["providers"]["slack"]["entries"][0]["activation_generation"] == 2
    backup = store.directory/"current-relay-mappings.reconstruction-backup"
    assert base64.b64decode(json.loads(backup.read_text())["raw_base64"]) == original
    clone = cs.ControlStore(store.codex_home, SID, store.repo_path, boot_id="restarted")
    merge(clone, [entry(2),entry(2,"lark"),entry(2,"onebot")])
    assert clone.relay_mappings() == [dict(schema_version=1, **current)]
    stable = path.read_bytes(), backup.read_bytes()
    assert clone.reconstruct_current_mappings([], trusted_floors(), NOW)["status"] == "already_available"
    assert (path.read_bytes(),backup.read_bytes()) == stable


@pytest.mark.parametrize("active,floors", [
    ([entry(1)]*2, trusted_floors()),
    ([entry(n) for n in range(5)], trusted_floors()),
    ([entry(1,created=NOW+1)], trusted_floors()),
    ([entry(1,created=NOW-72*3600)], trusted_floors()),
    ([], {"slack":[NOW,"~"]}),
    ([], dict(trusted_floors(), slack=[True,"~"])),
    ([], dict(trusted_floors(), slack=[NOW,"wrong"])),
])
def test_untrusted_or_incomplete_reconstruction_view_never_creates_backup_or_replaces(store, active, floors):
    path = corrupt_projection(store)
    original = path.read_bytes()
    with pytest.raises(cs.ControlError):
        store.reconstruct_current_mappings(active,floors,NOW)
    assert path.read_bytes() == original
    assert not (store.directory/"current-relay-mappings.reconstruction-backup").exists()


def test_empty_authoritative_reconstruction_closes_all_providers_without_history_promotion(store):
    corrupt_projection(store)
    store.reconstruct_current_mappings([],trusted_floors(),NOW)
    merge(store,[entry(1),entry(1,"lark"),entry(1,"onebot")])
    assert store.relay_mappings() == []


def test_repeated_trusted_repair_rotates_only_managed_backup_preserving_unknown_metadata(store):
    path = corrupt_projection(store,b"first damaged projection")
    store.reconstruct_current_mappings([entry(1)],trusted_floors(),NOW)
    backup = store.directory/"current-relay-mappings.reconstruction-backup"
    value = json.loads(backup.read_text())
    value["future"] = {"keep":True}
    backup.write_text(json.dumps(value))
    path.write_bytes(b"second damaged projection")
    store.reconstruct_current_mappings([],trusted_floors(),NOW+1)
    after = json.loads(backup.read_text())
    assert base64.b64decode(after["raw_base64"]) == b"second damaged projection"
    assert after["future"] == {"keep":True}
    assert len(list(store.directory.glob("*.reconstruction-backup"))) == 1
    assert not list(store.directory.glob(".mapping-backup-*"))


@pytest.mark.parametrize("size,success", [(131072,True),(131073,False)])
def test_backup_envelope_covers_exact_full_source_bound_and_preserves_oversized_gap(store,size,success):
    raw = b"\xff"*size  # Worst arbitrary bytes are retained losslessly via base64.
    path = corrupt_projection(store,raw)
    if not success:
        with pytest.raises(cs.ControlError,match="recovery_source_too_large"):
            store.reconstruct_current_mappings([],trusted_floors(),NOW)
        assert path.read_bytes() == raw
        assert merge(store,[entry(1)])["status"] == "projection_unavailable"
        return
    store.reconstruct_current_mappings([],trusted_floors(),NOW)
    backup = (store.directory/"current-relay-mappings.reconstruction-backup").read_bytes()
    assert 131072 < len(backup) <= 262144
    assert base64.b64decode(json.loads(backup)["raw_base64"]) == raw
    # A second repair must be able to read the larger own artifact as well.
    path.write_bytes(raw)
    assert store.reconstruct_current_mappings([],trusted_floors(),NOW+1)["status"] == "reconstructed"


def test_unknown_backup_is_preserved_and_refuses_only_explicit_metadata_repair(store):
    path = corrupt_projection(store)
    original = path.read_bytes()
    backup = store.directory/"current-relay-mappings.reconstruction-backup"
    backup.write_bytes(b"unrecognized user evidence")
    with pytest.raises(cs.ControlError,match="recovery_backup_invalid"):
        store.reconstruct_current_mappings([],trusted_floors(),NOW)
    assert path.read_bytes() == original and backup.read_bytes() == b"unrecognized user evidence"
    assert merge(store,[entry(1)])["status"] == "projection_unavailable"


@pytest.mark.parametrize("failure", ["backup","snapshot"])
def test_reconstruction_crash_boundaries_preserve_source_and_allow_fresh_metadata_retry(store,monkeypatch,failure):
    path = corrupt_projection(store)
    original = path.read_bytes()
    atomic, replace = cs.control_atomic_json, cs.os.replace
    def fail_atomic(*args):
        raise OSError("fixture before snapshot publish")
    def fail_replace(*args):
        raise OSError("fixture before backup publish")
    if failure == "backup":
        monkeypatch.setattr(cs.os,"replace",fail_replace)
    else:
        monkeypatch.setattr(cs,"control_atomic_json",fail_atomic)
    with pytest.raises(OSError):
        store.reconstruct_current_mappings([entry(1)],trusted_floors(),NOW)
    assert path.read_bytes() == original
    backup = store.directory/"current-relay-mappings.reconstruction-backup"
    assert backup.exists() is (failure == "snapshot")
    assert not list(store.directory.glob(".mapping-backup-*"))
    monkeypatch.setattr(cs,"control_atomic_json",atomic)
    monkeypatch.setattr(cs.os,"replace",replace)
    assert store.reconstruct_current_mappings([entry(1)],trusted_floors(),NOW)["status"] == "reconstructed"
