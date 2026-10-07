"""POSIX offline migration acceptance using node state and native receipts.

Pure CLI guards live in test_linux_relay_migration_cli.py and pure merge,
cursor and snapshot coverage lives in test_relay_authority_migration.py.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from codex_watchdog import linux_relay_migration as migration
from codex_watchdog import relay_authority_rollback as rollback
from codex_watchdog.control_state import ControlError
from codex_watchdog.models import sha256_text
from codex_watchdog.relay import RelayTarget
from codex_watchdog.relay_authority_migration import read_snapshot
from codex_watchdog.slack_poll import SlackPollingThreadStore


SID = "11111111-2222-4333-8444-000000000001"
OTHER = "11111111-2222-4333-8444-000000000002"
CHANNEL = "C12345678"
PARENT = "1791388800.000001"
NOW = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc).timestamp()
PRINCIPAL = dict(team_id="T12345678", user_id="U12345678",
                 bot_id="B12345678", app_id="A12345678")


def write_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    if os.name != "posix" or not hasattr(os, "getuid"):
        pytest.skip("offline Linux relay migration requires native POSIX UID, private modes and record locks")
    monkeypatch.setattr("codex_watchdog.slack_mapping.utc_now", lambda: "2026-10-07T16:00:00Z")
    home = tmp_path / "codex-home"
    home.mkdir()
    repo = tmp_path / "retained-repo"
    repo.mkdir()
    runtimes, stores = {}, {}
    for node in ("login6", "login3"):
        base = home / "watchdog-nodes" / node
        write_json(base / "node.json", dict(schema_version=1, node=node, codex_home=str(home)))
        runtime = base / "watchdog-control" / SID / "runtime"
        runtime.mkdir(parents=True)
        write_json(runtime.parent / "owner.json", dict(schema_version=1, thread_id=SID,
            repo_path=str(repo), epoch=7, owner=None, external_effect=None,
            native_boot_id="old-native-boot", writer_pid=987654,
            state="HANDOFF", remote_state=dict(pending_instruction_id=None)))
        for filename, payload in (
            ("profile.json", '{"user_setting":"retain"}\n'),
            ("notifications.env", "DUMMY_TOKEN=synthetic-secret-preserve\n"),
            ("native-queue.json", '{"native":"keep-node-local"}\n'),
        ):
            (runtime / filename).write_text(payload)
        store = SlackPollingThreadStore(runtime)
        store.record_thread(CHANNEL, PARENT,
                            RelayTarget("control-fixture", SID, "process_local"), "a" * 64)
        key = store.thread_key(CHANNEL, PARENT)
        write_json(runtime / "slack" / "poll-cursors.json",
                   dict(schema_version=1, after=key, threads={key: "1791389141.470039"}))
        runtimes[node], stores[node] = runtime, store
    grant = dict(schema_version=1, provider="slack", scope=CHANNEL, thread_id=SID,
                 principals=[PRINCIPAL], created_at="2026-10-07T16:00:00Z")
    journal = stores["login6"].journal
    with journal.transaction() as db:
        journal.put(db, "slack_bot_grants", sha256_text("slack\0" + CHANNEL + "\0bot_grant\0" + SID), grant)
    database = home / "state_5.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE threads(id TEXT,cwd TEXT,archived INTEGER,source TEXT,thread_source TEXT)")
        db.execute("INSERT INTO threads VALUES(?,?,0,'vscode','user')", (SID, str(repo)))
    quiescence = tmp_path / "native-quiescence.json"
    write_json(quiescence, dict(schema_version=1, purpose="offline-relay-migration", codex_home=str(home),
        nodes=[dict(node=node, verified_at="2026-10-07T18:00:00Z", MainPID=0,
                    ActiveState="inactive", no_watchdog_processes=True, boot_id="fresh-native-" + node)
               for node in runtimes]))
    return dict(home=home, repo=repo, runtimes=runtimes, stores=stores,
                quiescence=quiescence, root=home / "watchdog-relay-authority")


def apply(fleet, **kwargs):
    return migration.apply_authority(fleet["home"], fleet["quiescence"], clock=lambda: NOW, **kwargs)


def files_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_preview_exposes_counts_and_hashes_without_journal_payload_or_writes(fleet):
    before = files_bytes(fleet["home"])
    plan = migration.plan_authority(fleet["home"])
    rendered = json.dumps(plan["summary"])
    assert "synthetic-secret-preserve" not in rendered
    assert not any(value in rendered for value in PRINCIPAL.values())
    assert "principals" not in rendered and "1791389141.470039" not in rendered
    assert plan["summary"]["nodes"] == ["login3", "login6"]
    assert len(plan["summary"]["sources"]) == 2
    assert plan["summary"]["sessions"] == {SID: str(fleet["repo"])}
    assert len(plan["summary"]["plan_sha256"]) == 64
    assert files_bytes(fleet["home"]) == before


def test_apply_preserves_saved_user_native_and_source_database_state(fleet):
    protected = [fleet["home"] / "state_5.sqlite"]
    for runtime in fleet["runtimes"].values():
        protected += [runtime / "profile.json", runtime / "notifications.env",
                      runtime / "native-queue.json", runtime.parent / "owner.json",
                      runtime / "slack" / "reply-tickets.sqlite3"]
    before = {path: path.read_bytes() for path in protected}
    plan = migration.plan_authority(fleet["home"])
    result = apply(fleet)
    assert result["status"] == "migrated" and result["plan_sha256"] == plan["summary"]["plan_sha256"]
    assert all(path.read_bytes() == contents for path, contents in before.items())
    canonical = fleet["root"] / "runtime" / "slack" / "reply-tickets.sqlite3"
    rows, _ = read_snapshot(canonical)
    assert sum(row["kind"] == "slack_bot_grants" for row in rows) == 1
    assert not any(row["kind"] in ("owner", "native_queue", "writer_pid") for row in rows)
    cursors = [json.loads(row["value"]) for row in rows if row["kind"] == "relay_cursors"]
    assert len(cursors) == 1 and cursors[0]["thread_id"] == SID
    assert next(iter(cursors[0]["value"]["threads"].values())) == "1791389141.470039"
    manifest = json.loads((fleet["root"] / "sessions" / (SID + ".json")).read_text())
    assert manifest["repo_path"] == str(fleet["repo"]) and manifest["state"] == "ready"
    assert manifest["plan_sha256"] == result["plan_sha256"]
    for source in plan["summary"]["sources"]:
        backup = Path(source["path"] + ".relay-authority-v1-backup")
        assert read_snapshot(backup)[1] == source["snapshot_sha256"]


def test_old_source_refusal_markers_prevent_accidental_legacy_restart(fleet):
    old_markers = {node: store.path.read_bytes() for node, store in fleet["stores"].items()}
    apply(fleet)
    for node, store in fleet["stores"].items():
        marker = json.loads(store.path.read_text())
        assert marker["schema_version"] == 3 and marker["storage"] == "cluster_provider_journals_v1"
        backup = store.path.with_name(store.path.name + ".relay-authority-v1-backup")
        assert backup.read_bytes() == old_markers[node]
        with pytest.raises(ValueError, match="marker_invalid"):
            with store.journal.transaction():
                pass


def test_apply_is_one_time_and_does_not_repeat_import_or_change_saved_state(fleet, monkeypatch):
    first = apply(fleet)
    before = files_bytes(fleet["home"])
    monkeypatch.setattr(migration, "plan_authority", lambda _: pytest.fail("ready activation must not re-import histories"))
    second = apply(fleet)
    assert second == dict(status="already_migrated", plan_sha256=first["plan_sha256"])
    assert files_bytes(fleet["home"]) == before


@pytest.mark.parametrize("change,reason", [
    (lambda value: value["nodes"].pop(), "quiescence_incomplete"),
    (lambda value: value["nodes"][0].update(MainPID=1), "old_listener_not_quiescent"),
    (lambda value: value["nodes"][0].update(MainPID=False), "old_listener_not_quiescent"),
    (lambda value: value["nodes"][0].update(no_watchdog_processes=False), "old_listener_not_quiescent"),
    (lambda value: value["nodes"][0].update(verified_at="2026-10-07T17:54:59Z"), "old_listener_not_quiescent"),
    (lambda value: value["nodes"].append(dict(value["nodes"][0])), "old_listener_not_quiescent"),
])
def test_unverified_or_stale_node_receipt_never_publishes_ready(fleet, change, reason):
    receipt = json.loads(fleet["quiescence"].read_text())
    change(receipt)
    write_json(fleet["quiescence"], receipt)
    with pytest.raises(ControlError, match=reason):
        apply(fleet)
    assert not (fleet["root"] / "cluster.json").exists()
    assert not (fleet["root"] / "runtime").exists()


def test_receipt_must_be_private_without_changing_its_permissions(fleet):
    fleet["quiescence"].chmod(0o644)
    with pytest.raises(ControlError, match="not_private"):
        apply(fleet)
    assert fleet["quiescence"].stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("field", ["external_effect", "pending_instruction_id"])
def test_live_or_uncertain_native_effect_blocks_before_source_fencing(fleet, field):
    path = fleet["runtimes"]["login3"].parent / "owner.json"
    owner = json.loads(path.read_text())
    if field == "external_effect":
        owner[field] = dict(id="preserve-this-effect")
    else:
        owner["remote_state"][field] = "historical-instruction-do-not-replay"
    write_json(path, owner)
    before = fleet["stores"]["login3"].path.read_bytes()
    with pytest.raises(ControlError, match="native_effect_pending"):
        apply(fleet)
    assert fleet["stores"]["login3"].path.read_bytes() == before
    assert not (fleet["root"] / "cluster.json").exists()


def test_archived_session_history_is_retained_without_enrolling_replacement(fleet):
    with sqlite3.connect(fleet["home"] / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET archived=1")
    result = apply(fleet)
    assert result["sessions"] == {}
    assert not (fleet["root"] / "sessions" / (SID + ".json")).exists()
    rows, _ = read_snapshot(fleet["root"] / "runtime" / "slack" / "reply-tickets.sqlite3")
    assert any(row["kind"] == "slack_bot_grants" for row in rows)


def test_crash_during_source_backup_recovers_without_new_admission(fleet, monkeypatch):
    original = migration._backup_database
    calls = []
    def crash_once(path, expected):
        original(path, expected)
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("synthetic interruption after source backup")
    monkeypatch.setattr(migration, "_backup_database", crash_once)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        apply(fleet)
    assert not (fleet["root"] / "cluster.json").exists()
    monkeypatch.setattr(migration, "_backup_database", original)
    result = apply(fleet)
    assert result["status"] == "migrated"
    assert len(list(fleet["root"].glob("stage-*"))) == 0


def test_source_change_after_backup_is_detected_before_any_refusal_marker(fleet, monkeypatch):
    original = migration._backup_database
    changed = []
    def change_once(path, expected):
        original(path, expected)
        if not changed:
            with sqlite3.connect(path) as db:
                db.execute("UPDATE records SET active=0 WHERE kind='threads'")
            changed.append(path)
    monkeypatch.setattr(migration, "_backup_database", change_once)
    marker = fleet["stores"]["login3"].path
    before = marker.read_bytes()
    with pytest.raises(ControlError, match="source_changed"):
        apply(fleet)
    assert marker.read_bytes() == before
    assert not (fleet["root"] / "cluster.json").exists()


def test_crash_after_runtime_publish_recovers_from_verified_prepared_intent(fleet, monkeypatch):
    original = migration.control_atomic_json
    crashed = []
    def crash_after_runtime(path, value):
        if path.parent.name == "sessions" and not crashed:
            crashed.append(True)
            raise RuntimeError("synthetic interruption after runtime publication")
        return original(path, value)
    monkeypatch.setattr(migration, "control_atomic_json", crash_after_runtime)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        apply(fleet)
    assert (fleet["root"] / "runtime").exists() and not (fleet["root"] / "cluster.json").exists()
    monkeypatch.setattr(migration, "control_atomic_json", original)
    assert apply(fleet)["status"] == "migrated"


@pytest.mark.parametrize("tamper", ["missing_database", "missing_session_manifest", "wrong_storage"])
def test_existing_ready_label_cannot_mask_incomplete_or_wrong_authority(fleet, tamper):
    apply(fleet)
    if tamper == "missing_database":
        (fleet["root"] / "runtime" / "slack" / "reply-tickets.sqlite3").unlink()
    elif tamper == "missing_session_manifest":
        (fleet["root"] / "sessions" / (SID + ".json")).unlink()
    else:
        cluster = fleet["root"] / "cluster.json"
        value = json.loads(cluster.read_text())
        value["storage"] = "wrong-authority-domain"
        write_json(cluster, value)
    with pytest.raises((ControlError, ValueError)):
        apply(fleet)


def test_quarantine_has_durable_private_audit_and_backup_provenance(fleet):
    journal = fleet["stores"]["login3"].journal
    with journal.transaction() as db:
        key = journal.get(db, "threads", fleet["stores"]["login3"].thread_key(CHANNEL, PARENT))
        key["target"]["workspace_id"] = "different-historic-workspace"
        journal.put(db, "threads", fleet["stores"]["login3"].thread_key(CHANNEL, PARENT), key)
    result = apply(fleet)
    assert result["quarantines"] == {"slack": 1}
    rows, _ = read_snapshot(fleet["root"] / "runtime" / "slack" / "reply-tickets.sqlite3")
    parent = next(row for row in rows if row["kind"] == "threads")
    assert parent["active"] == 0 and json.loads(parent["value"])["relay_authority_quarantine"] is True
    manifest = fleet["root"] / "migration-manifest.json"
    assert manifest.is_file()
    evidence = json.loads(manifest.read_text())
    assert evidence["plan_sha256"] == result["plan_sha256"]
    assert len(evidence["sources"]) == 2
    assert len(evidence["quarantines"]["slack"]) == 1
    assert len(evidence["quarantines"]["slack"][0]["variants"]) == 2
    assert manifest.stat().st_mode & 0o077 == 0


def test_runtime_symlink_cannot_import_or_fence_files_outside_node_boundary(fleet, tmp_path):
    import shutil
    runtime = fleet["runtimes"]["login3"]
    outside = tmp_path / "unapproved-runtime"
    shutil.move(str(runtime), outside)
    runtime.symlink_to(outside, target_is_directory=True)
    before = files_bytes(outside)
    with pytest.raises((ControlError, ValueError)):
        migration.plan_authority(fleet["home"])
    assert files_bytes(outside) == before


def test_marker_symlink_is_refused_without_replacing_or_following_external_file(fleet, tmp_path):
    marker = fleet["stores"]["login3"].path
    outside = tmp_path / "unapproved-marker.json"
    outside.write_bytes(marker.read_bytes())
    marker.unlink()
    marker.symlink_to(outside)
    before = outside.read_bytes()
    with pytest.raises((ControlError, ValueError)):
        apply(fleet)
    assert marker.is_symlink() and outside.read_bytes() == before


def test_cursor_only_advance_changes_frozen_plan_and_blocks_stale_import(fleet, monkeypatch):
    original = migration._backup_database
    changed = []
    def advance_once(path, expected):
        original(path, expected)
        if not changed:
            cursor = fleet["runtimes"]["login3"] / "slack" / "poll-cursors.json"
            value = json.loads(cursor.read_text())
            key = next(iter(value["threads"]))
            value["threads"][key] = "1791389141.470040"
            write_json(cursor, value)
            changed.append(True)
    monkeypatch.setattr(migration, "_backup_database", advance_once)
    with pytest.raises(ControlError, match="source_changed"):
        apply(fleet)
    assert not (fleet["root"] / "cluster.json").exists()


def test_runtime_ancestor_symlink_without_a_cursor_is_also_refused(fleet, tmp_path):
    import shutil
    runtime = fleet["runtimes"]["login3"]
    (runtime / "slack" / "poll-cursors.json").unlink()
    outside = tmp_path / "unapproved-cursorless-runtime"
    shutil.move(str(runtime), outside)
    runtime.symlink_to(outside, target_is_directory=True)
    before = files_bytes(outside)
    with pytest.raises((ControlError, ValueError)):
        migration.plan_authority(fleet["home"])
    assert files_bytes(outside) == before


def test_authority_root_symlink_cannot_create_or_publish_state_outside_boundary(fleet, tmp_path):
    outside = tmp_path / "unapproved-authority"
    outside.mkdir()
    fleet["root"].symlink_to(outside, target_is_directory=True)
    with pytest.raises((ControlError, ValueError)):
        apply(fleet)
    assert list(outside.iterdir()) == []


def test_existing_state_cannot_take_fresh_install_shortcut(fleet):
    original = {store.path: store.path.read_bytes() for store in fleet["stores"].values()}
    with pytest.raises(ControlError, match="migration_required"):
        migration.initialize_empty_authority(fleet["home"])
    assert not (fleet["root"] / "cluster.json").exists()
    assert all(path.read_bytes() == data for path, data in original.items())


def test_fresh_install_enrolls_only_the_verified_existing_exact_session(fleet):
    import shutil
    from codex_watchdog.relay_authority import SessionRelayAuthority
    for runtime in fleet["runtimes"].values():
        shutil.rmtree(runtime / "slack")
    migration.initialize_empty_authority(fleet["home"])
    cluster = json.loads((fleet["root"] / "cluster.json").read_text())
    assert cluster["fresh_install"] is True
    assert not (fleet["root"] / "sessions" / (SID + ".json")).exists()
    proof = dict(relay_epoch=3, relay_owner=dict(node="login3", boot_id="current-boot", instance="exact-instance"))
    calls = []
    def admitted_native_epoch():
        calls.append(SID)
        return proof
    store = SimpleNamespace(codex_home=fleet["home"], thread_id=SID, repo_path=str(fleet["repo"]))
    authority = SessionRelayAuthority(store, {}, SimpleNamespace(capture_relay_epoch=admitted_native_epoch), "control-fixture")
    authority.activate()
    manifest = json.loads((fleet["root"] / "sessions" / (SID + ".json")).read_text())
    assert manifest["thread_id"] == SID and manifest["repo_path"] == str(fleet["repo"])
    assert authority.expected == proof and calls == [SID, SID]
    with sqlite3.connect(fleet["home"] / "state_5.sqlite") as db:
        assert db.execute("SELECT id FROM threads").fetchall() == [(SID,)]


def create_native_receipt(fleet, *, node="login6", offset=0, instruction="slack:fixture-native", same_event=True):
    store = fleet["stores"]["login6"]
    event_key = "fixture-native-event"
    prompt = "preserve exact original request; never replay"
    if same_event:
        claimed, _ = store.claim_reply(event_key=event_key, channel_id=CHANNEL, thread_ts=PARENT,
            instruction_id=instruction, text=prompt)
        assert claimed
        store.finish_reply(event_key, state_value="delivered", delivery_status="enqueued")
    rollout = fleet["home"] / "original-native-rollout.jsonl"
    if not rollout.exists():
        rollout.write_text('{"type":"event_msg","payload":{"type":"irrelevant"}}\n')
    value = dict(schema_version=1, thread_id=SID, instruction_id=instruction,
                 prompt_sha256=sha256_text(prompt), rollout_path=str(rollout),
                 rollout_baseline_offset=offset, queue_message_id=OTHER,
                 native_pid=765432, native_boot_id="do-not-copy-this-boot", status="enqueued")
    path = fleet["home"] / "watchdog-nodes" / node / "remote-wake" / (sha256_text(instruction) + ".json")
    write_json(path, value)
    return path, value


def test_native_boundary_hydration_preserves_proof_without_pids_queue_replay_or_completion(fleet):
    path, receipt = create_native_receipt(fleet)
    original = path.read_bytes()
    plan = migration.plan_authority(fleet["home"])
    native = [json.loads(row["value"]) for row in plan["merged"]["slack"]
              if row["kind"] == "relay_native_receipts"]
    assert len(native) == 1
    assert set(native[0]["receipt"]) == {"thread_id", "instruction_id", "prompt_sha256",
        "rollout_path", "rollout_baseline_offset", "queue_message_id"}
    assert native[0]["receipt"]["queue_message_id"] == receipt["queue_message_id"]
    assert native[0]["progress"]["terminal"] is False
    assert "native_pid" not in json.dumps(native) and "do-not-copy-this-boot" not in json.dumps(native)
    apply(fleet)
    assert path.read_bytes() == original
    rows, _ = read_snapshot(fleet["root"] / "runtime" / "slack" / "reply-tickets.sqlite3")
    event = next(row for row in rows if row["kind"] == "events")
    assert event["active"] == 1 and json.loads(event["value"]).get("native_completed") is not True


def test_conflicting_invalid_native_boundary_is_not_discarded_to_select_valid_other_node(fleet):
    create_native_receipt(fleet)
    create_native_receipt(fleet, node="login3", offset=1, same_event=False)
    plan = migration.plan_authority(fleet["home"])
    assert not any(row["kind"] == "relay_native_receipts" for row in plan["merged"]["slack"])
    assert next(row for row in plan["merged"]["slack"] if row["kind"] == "events")["active"] == 1


def run_rollback(fleet):
    return rollback.rollback_authority(fleet["home"], fleet["quiescence"], clock=lambda: NOW)


def test_pre_first_use_rollback_restores_only_known_original_json_and_preserves_all_durable_evidence(fleet):
    source_json = {}
    for runtime in fleet["runtimes"].values():
        for path in (runtime / "slack").glob("*.json"):
            source_json[path] = path.read_bytes()
    protected = [fleet["home"] / "state_5.sqlite"]
    for runtime in fleet["runtimes"].values():
        protected += [runtime.parent / "owner.json", runtime / "profile.json",
                      runtime / "notifications.env", runtime / "native-queue.json",
                      runtime / "slack" / "reply-tickets.sqlite3"]
    before = {path: path.read_bytes() for path in protected}
    apply(fleet)
    canonical = fleet["root"] / "runtime"
    canonical_before = files_bytes(canonical)
    result = run_rollback(fleet)
    assert result["status"] == "rolled_back" and result["canonical_runtime_retained"] is True
    assert all(path.read_bytes() == data for path, data in source_json.items())
    assert all(path.read_bytes() == data for path, data in before.items())
    assert files_bytes(canonical) == canonical_before
    assert json.loads((fleet["root"] / "cluster.json").read_text())["state"] == "rolled_back"
    after = files_bytes(fleet["home"])
    assert run_rollback(fleet)["status"] == "already_rolled_back"
    assert files_bytes(fleet["home"]) == after
    for store in fleet["stores"].values():
        with store.journal.transaction() as db:
            assert db.execute("SELECT 1 FROM records WHERE kind='slack_bot_grants'").fetchone() in (None, (1,))


def test_post_admission_or_any_canonical_state_change_refuses_stale_rollback(fleet):
    apply(fleet)
    marker = fleet["stores"]["login6"].path
    before = marker.read_bytes()
    database = fleet["root"] / "runtime" / "slack" / "reply-tickets.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE records SET active=0 WHERE kind='threads'")
    with pytest.raises(ControlError, match="recovery_runtime_changed"):
        run_rollback(fleet)
    assert marker.read_bytes() == before
    assert json.loads((fleet["root"] / "cluster.json").read_text())["state"] == "ready"


def test_tampered_original_json_backup_blocks_all_restoration_before_candidate_fence(fleet):
    apply(fleet)
    path = fleet["stores"]["login6"].path
    backup = path.with_name(path.name + ".relay-authority-v1-backup")
    backup.write_bytes(backup.read_bytes() + b" ")
    current = {store.path: store.path.read_bytes() for store in fleet["stores"].values()}
    with pytest.raises(ControlError, match="original_json_changed"):
        run_rollback(fleet)
    assert all(path.read_bytes() == value for path, value in current.items())
    assert json.loads((fleet["root"] / "cluster.json").read_text())["state"] == "ready"


def test_interrupted_rollback_keeps_candidate_fenced_and_resumes_exact_original_restoration(fleet, monkeypatch):
    apply(fleet)
    original = rollback._restore_bytes
    calls = []
    def interrupted(path, data):
        original(path, data)
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("synthetic rollback interruption")
    monkeypatch.setattr(rollback, "_restore_bytes", interrupted)
    with pytest.raises(RuntimeError, match="rollback interruption"):
        run_rollback(fleet)
    assert json.loads((fleet["root"] / "cluster.json").read_text())["state"] == "rolled_back"
    assert not (fleet["root"] / "rollback-complete.json").exists()
    monkeypatch.setattr(rollback, "_restore_bytes", original)
    assert run_rollback(fleet)["status"] == "rolled_back"
    assert run_rollback(fleet)["status"] == "already_rolled_back"


def test_rollback_manifest_index_cannot_be_rebound_to_modified_original_backup(fleet):
    apply(fleet)
    manifest_path = fleet["root"] / "migration-manifest.json"
    value = json.loads(manifest_path.read_text())
    entry = value["json_sources"][0]
    backup = Path(entry["path"] + ".relay-authority-v1-backup")
    changed = backup.read_bytes() + b" "
    backup.write_bytes(changed)
    import hashlib
    entry["sha256"] = hashlib.sha256(changed).hexdigest()
    write_json(manifest_path, value)
    with pytest.raises(ControlError, match="manifest_mismatch"):
        run_rollback(fleet)
    assert json.loads((fleet["root"] / "cluster.json").read_text())["state"] == "ready"


def test_unknown_staged_authorization_is_not_promoted_as_if_it_came_from_frozen_sources(fleet):
    from codex_watchdog.reply_tickets import ReplyTickets
    plan = migration.plan_authority(fleet["home"])
    stage = fleet["root"] / ("stage-" + plan["summary"]["plan_sha256"])
    journal = ReplyTickets(stage / "slack" / "migration-state.json", stage / "locks" / "migration.lock",
                           "slack", stage, lambda: {})
    with journal.transaction() as db:
        namespace = "slack/poll-relay-state.json"
        db.execute("INSERT OR IGNORE INTO sources VALUES (?)", (namespace,))
        unexpected = dict(schema_version=1, provider="slack", scope="C87654321", thread_id=SID,
                          principals=[PRINCIPAL], created_at="2026-10-07T16:00:00Z")
        key = sha256_text("slack\0C87654321\0bot_grant\0" + SID)
        db.execute("INSERT INTO records(namespace,kind,key,value) VALUES(?,?,?,?)",
                   (namespace, "slack_bot_grants", key, json.dumps(unexpected)))
    with pytest.raises((ControlError, ValueError)):
        apply(fleet)
    assert not (fleet["root"] / "cluster.json").exists()
    assert journal.database.exists()  # Unknown staged evidence is retained.


def test_interruption_after_provider_stage_commit_recovers_identical_cursor_rows(fleet, monkeypatch):
    original = migration.control_atomic_json
    def interrupted(path, value):
        if path.name == "migration-import.json" and path.parent.name.startswith("stage-"):
            raise RuntimeError("synthetic pre-import-marker interruption")
        return original(path, value)
    monkeypatch.setattr(migration, "control_atomic_json", interrupted)
    with pytest.raises(RuntimeError, match="pre-import-marker"):
        apply(fleet)
    assert not (fleet["root"] / "cluster.json").exists()
    monkeypatch.setattr(migration, "control_atomic_json", original)
    assert apply(fleet)["status"] == "migrated"
    rows, _ = read_snapshot(fleet["root"] / "runtime" / "slack" / "reply-tickets.sqlite3")
    cursors = [json.loads(row["value"]) for row in rows if row["kind"] == "relay_cursors"]
    assert len(cursors) == 1 and next(iter(cursors[0]["value"]["threads"].values())) == "1791389141.470039"


def seed_notifications(fleet):
    event = sha256_text("original-outbound-notification")
    key = sha256_text("existing-session-history-key")
    originals = {}
    for runtime in fleet["runtimes"].values():
        history = runtime / "notifications" / "last-events.json"
        dual = runtime / "notifications" / "dual-deliveries.json"
        write_json(history, dict(schema_version=1, last_events={key: dict(
            fingerprint=event, recorded_at="2026-10-07T16:00:00Z", future="retained")}, future="original"))
        write_json(dual, dict(schema_version=1, events={event: dict(slack="sent", lark="uncertain")}))
        originals[history], originals[dual] = history.read_bytes(), dual.read_bytes()
    return originals


def test_pre_first_use_rollback_also_restores_only_indexed_original_outbound_receipt_markers(fleet):
    originals = seed_notifications(fleet)
    apply(fleet)
    canonical = fleet["root"] / "runtime"
    before = files_bytes(canonical)
    for path in originals:
        assert json.loads(path.read_bytes())["schema_version"] == 3
    result = run_rollback(fleet)
    assert result["status"] == "rolled_back"
    assert all(path.read_bytes() == value for path, value in originals.items())
    assert files_bytes(canonical) == before


def test_any_canonical_notification_mutation_refuses_stale_rollback_before_source_restoration(fleet):
    originals = seed_notifications(fleet)
    apply(fleet)
    canonical = fleet["root"] / "runtime" / "session-notifications" / SID / "notifications"
    path = canonical / "last-events.json"
    state = json.loads(path.read_text())
    state["last_events"][sha256_text("new-outbound")] = dict(
        fingerprint=sha256_text("new-outbound-result"), recorded_at="2026-10-07T18:00:00Z")
    write_json(path, state)
    before = {path: path.read_bytes() for path in originals}
    with pytest.raises(ControlError, match="recovery_runtime_changed"):
        run_rollback(fleet)
    assert all(path.read_bytes() == value for path, value in before.items())
    assert json.loads((fleet["root"] / "cluster.json").read_text())["state"] == "ready"


def migration_cli(fleet, monkeypatch, *actions):
    from codex_watchdog import cli
    monkeypatch.setattr("codex_watchdog.linux_binding.locality_identity", lambda: "native-fixture-host")
    return cli.main(["--codex-home", str(fleet["home"]), "linux-relay-authority-migrate", *actions])


def test_cli_preview_is_read_only_private_and_never_prepares_messaging(fleet, monkeypatch, capsys):
    before = files_bytes(fleet["home"])
    monkeypatch.setattr("codex_watchdog.messaging_setup.prepare_launch",
                        lambda *_args, **_kwargs: pytest.fail("migration must never reconfigure messaging"))
    assert migration_cli(fleet, monkeypatch) == 0
    output = capsys.readouterr().out
    receipt = json.loads(output)
    assert receipt["status"] == "preview" and receipt["records"]["slack"] == 2
    assert not any(value in output for value in PRINCIPAL.values())
    assert "synthetic-secret-preserve" not in output and PARENT not in output
    assert files_bytes(fleet["home"]) == before
