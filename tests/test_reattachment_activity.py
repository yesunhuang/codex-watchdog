"""Transient ``attachment_at`` proof: parsers, resolver plumbing, lifecycle.

Synthetic fixtures only. An exact typed successful reopen (or armed typed
fresh creation) of the current owner yields one immutable local log time;
anything weaker yields none. Persisted shapes and routing are unchanged.
"""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codex_watchdog.session_activity import TTL_SECONDS
from codex_watchdog.session_lifecycle import SessionLifecycle, _attachment_activity
from codex_watchdog.windows_vscode_discovery import WindowsNativeCollector
from codex_watchdog.workspace_discovery import (
    HISTORY_ATTACHED_SOURCE,
    LiveVSCodeWindow,
    SessionResolution,
    _codex_log_session_evidence,
    _history_attached_resolution,
)
from codex_watchdog.workspace_registry import (
    _ENTRY_KEYS,
    TrackedWorkspace,
    WorkspaceRegistryFormatError,
)

from test_workspace_discovery import SESSION_CURRENT, new_format_discovery, target_window

SID = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
TAG = "[info] [extension-app-server-manager]"
SPAWN = "[info] [CodexMcpConnection] Spawning codex app-server\n"


def at(second):
    return f"2026-11-20 12:00:{second:02d}.250"


def epoch(second):
    parsed = time.strptime(f"2026-11-20 12:00:{second:02d}", "%Y-%m-%d %H:%M:%S")
    return time.mktime(parsed) + 0.25


def view(second, *, active="true", history="false", sid=SID):
    return (f"{at(second)} {TAG} thread_stream_view_activity_changed active={active} "
            f"conversationId={sid} historyAttached={history} resumeState=resumed\n")


def resume(second, fields="assignedHistoryAttachment=true", *, thread=SID, prefix=None, sid=SID):
    head = at(second) if prefix is None else prefix
    tail = "" if thread is None else f" threadId={thread}"
    return (f"{head} {TAG} maybe_resume_success {fields} conversationId={sid}{tail}\n").lstrip()


def created(second, *, thread=SID):
    return (f"{at(second)} {TAG} Conversation created beforeConversationAddedDurationMs=104 "
            f"conversationId={SID} durationMs=442 threadId={thread} threadStartKind=default "
            "usedPrewarmedThread=false vmEvent=thread_created vmProduct=unknown\n")


def role(second, value):
    return f"{at(second)} {TAG} thread_stream_role_changed conversationId={SID} role={value}\n"


FAILED = f"{TAG} maybe_resume_failed conversationId={SID}\n"
INACTIVE = f"{TAG} thread_stream_view_activity_changed active=false conversationId={SID}\n"

PROVEN = {
    "typed_resume": ([view(1), resume(2)], 2),
    "armed_typed_creation": ([view(1, history="true"), created(2)], 2),
    "kept_by_later_active_attached_view": ([view(1), resume(2), view(3, history="true")], 2),
    "legacy_owner_exact_identity": ([resume(2, "assignedStreamRole=owner")], 2),
    "later_reopen_replaces_older": ([view(1), resume(2), resume(5)], 5),
}
REFUSED = {
    "unarmed_creation": [created(2)],
    "creation_after_unattached_view": [view(1), created(2)],
    "creation_with_mismatched_thread": [view(1, history="true"), created(2, thread=OTHER)],
    "failed_resume": [view(1), FAILED],
    "failed_after_resume": [view(1), resume(2), FAILED],
    "inactive_before_resume": [INACTIVE, resume(2)],
    "inactive_after_resume": [view(1), resume(2), INACTIVE],
    "follower_after_resume": [view(1), resume(2), role(3, "follower")],
    "unknown_after_resume": [view(1), resume(2), role(3, "unknown")],
    "revoked_after_resume": [view(1), resume(2), view(3, history="false")],
    "spawn_replacement_after_resume": [view(1), resume(2), SPAWN],
    "follower_resume": [resume(2, "assignedStreamRole=follower assignedHistoryAttachment=true")],
    "attachment_false": [view(1), resume(2, "assignedHistoryAttachment=false")],
    "attachment_malformed": [view(1), resume(2, "assignedHistoryAttachment=1")],
    "attachment_duplicated": [
        view(1), resume(2, "assignedHistoryAttachment=true assignedHistoryAttachment=true")],
    "mismatched_thread": [view(1), resume(2, thread=OTHER)],
    "unstamped_resume": [view(1), resume(2, prefix="")],
    "malformed_stamp": [view(1), resume(2, prefix="2026-13-40 12:00:02.250")],
    "unparseable_fresh_supersedes_older": [view(1), resume(2), resume(3, prefix="")],
    "quoted_event": [
        view(1),
        f"{at(2)} {TAG} relayed text=maybe_resume_success assignedHistoryAttachment=true "
        f"conversationId={SID} threadId={SID}\n"],
    "legacy_owner_mismatched_thread": [resume(2, "assignedStreamRole=owner", thread=OTHER)],
    "legacy_owner_without_thread": [resume(2, "assignedStreamRole=owner", thread=None)],
}


def shared_proofs(tmp_path, lines):
    log = tmp_path / "Codex.log"
    log.write_text(SPAWN + "".join(lines), encoding="utf-8")
    proofs = {"stale": 1.0}  # Must be cleared, never merged.
    _codex_log_session_evidence(log, (SID,), attachment_at=proofs)
    return proofs


def windows_proofs(lines):
    collector = WindowsNativeCollector(".", reader=object())  # Pure parser only.
    data = (SPAWN + "".join(lines)).encode("utf-8")
    return dict(collector.codex_log_generation(data).attachment_at)


@pytest.mark.parametrize("case", sorted(PROVEN))
def test_exact_fresh_attachment_emits_immutable_timestamp_in_both_parsers(tmp_path, case):
    lines, second = PROVEN[case]
    expected = {SID: epoch(second)}
    assert shared_proofs(tmp_path, lines) == expected
    assert windows_proofs(lines) == expected
    # Reparsing the identical log yields the identical (not refreshed) stamp.
    assert shared_proofs(tmp_path, lines) == expected


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_weaker_or_revoked_evidence_retains_no_attachment_proof(tmp_path, case):
    lines = REFUSED[case]
    assert shared_proofs(tmp_path, lines) == {}
    assert windows_proofs(lines) == {}


def test_parser_without_output_mapping_is_unchanged(tmp_path):
    log = tmp_path / "Codex.log"
    log.write_text(SPAWN + view(1) + resume(2), encoding="utf-8")
    with_map = _codex_log_session_evidence(log, (SID,), attachment_at={})
    assert _codex_log_session_evidence(log, (SID,)) == with_map
    assert with_map[2] == {SID}


# Resolver plumbing -----------------------------------------------------------

def history(tmp_path, *, loaded=(SID,), attached=(SID,), writer=101, proofs=None):
    window = LiveVSCodeWindow("1", 10, 101, tmp_path / "Codex.log")
    return _history_attached_resolution(
        loaded, {session: (None, True) for session in loaded}, set(attached), set(),
        [window], tmp_path, lambda _lock: writer, lambda _lock: True,
        attachment_at={SID: 1234.5} if proofs is None else proofs)


def test_history_resolution_carries_stamp_only_when_resolved(tmp_path):
    accepted = history(tmp_path)
    assert (accepted.status, accepted.session_id, accepted.source) == (
        "resolved", SID, HISTORY_ATTACHED_SOURCE)
    assert accepted.attachment_at == 1234.5
    assert history(tmp_path, proofs={}).attachment_at is None
    ambiguous = history(tmp_path, loaded=(SID, OTHER), attached=(SID, OTHER))
    assert ambiguous.session_id is None and ambiguous.attachment_at is None
    peer = history(tmp_path, writer=202)
    assert peer.session_id is None and peer.attachment_at is None


def _stamped_target_log(target_log, fields):
    target_log.write_text(
        SPAWN + view(1, sid=SESSION_CURRENT)
        + resume(2, fields, thread=SESSION_CURRENT, sid=SESSION_CURRENT),
        encoding="utf-8")


def _attachment_by_session(snapshot):
    return {item.session_id: getattr(item, "attachment_at", None)
            for item in snapshot.effective_workspaces}


@pytest.mark.parametrize("fields,source", [
    ("assignedHistoryAttachment=true", HISTORY_ATTACHED_SOURCE),
    ("assignedStreamRole=owner assignedHistoryAttachment=true", "codex_state_vscode_live_owner"),
])
def test_discovery_plumbs_exact_owner_stamp_without_changing_routing(tmp_path, fields, source):
    discovery, target_log, _windows = new_format_discovery(tmp_path, fields=fields)
    _stamped_target_log(target_log, fields)

    snapshot = discovery.snapshot()

    target = target_window(snapshot)
    assert (target.session_id, target.session_source, target.reason) == (
        SESSION_CURRENT, source, None)
    attachments = _attachment_by_session(snapshot)
    assert attachments[SESSION_CURRENT] == epoch(2)
    # The other window's legacy role-change carries no fresh reopen event.
    assert all(value is None for sid, value in attachments.items() if sid != SESSION_CURRENT)


def test_discovery_follower_attachment_is_rejected_without_stamp(tmp_path):
    fields = "assignedStreamRole=follower assignedHistoryAttachment=true"
    discovery, target_log, _windows = new_format_discovery(tmp_path, fields=fields)
    _stamped_target_log(target_log, fields)

    snapshot = discovery.snapshot()

    assert target_window(snapshot).session_id is None
    assert SESSION_CURRENT not in _attachment_by_session(snapshot)
    assert all(value is None for value in _attachment_by_session(snapshot).values())


# Lifecycle ---------------------------------------------------------------------

THREAD = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def lifecycle_case(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = TrackedWorkspace.create("fixture", repo, THREAD)
    clock = [1800000000.0]
    config = SimpleNamespace(interactive_providers=("slack",), slack_relay_configured=True,
                             slack_channel_id="C12345678")
    calls = []

    def notify(event):
        calls.append(event)
        return SimpleNamespace(to_dict=lambda: dict(status="sent"))

    notifier = SimpleNamespace(config=config, slack_thread_store=object(), notify=notify)
    monkeypatch.setattr("codex_watchdog.session_lifecycle.SessionRoutes.destination",
                        lambda *args: "C87654321")
    lifecycle = SessionLifecycle(tmp_path, notifier, clock=lambda: clock[0])
    return SimpleNamespace(tmp=tmp_path, workspace=workspace, clock=clock, calls=calls,
                           notifier=notifier, lifecycle=lifecycle)


def idle(genuine=None):
    return dict(exact=True, genuine_at=genuine, turn_active=False, queue_active=False)


def make_dormant(case):
    first = case.lifecycle.observe(case.workspace, idle(case.clock[0]), verified_owner=True)
    assert first["status"] == "ACTIVE" and first["generation"] == 1 and len(case.calls) == 1
    case.clock[0] += TTL_SECONDS + 10
    state = case.lifecycle.observe(case.workspace, idle(), verified_owner=True)
    assert state["status"] == "DORMANT"
    return state


def test_dormant_original_session_reactivates_once_on_fresh_exact_attachment(lifecycle_case):
    case = lifecycle_case
    make_dormant(case)
    attached = replace(case.workspace, attachment_at=case.clock[0] - 5)

    state = case.lifecycle.observe(attached, idle(), verified_owner=True)

    assert state["status"] == "ACTIVE" and state["generation"] == 2
    assert state["last_activity_at"] == case.clock[0] - 5
    assert len(case.calls) == 1 + 1
    assert case.calls[-1].relay_target.thread_id == THREAD

    # Identical proof on later ticks, also after reconstruction, is not new activity.
    for lifecycle in (case.lifecycle,
                      SessionLifecycle(case.tmp, case.notifier, clock=lambda: case.clock[0])):
        case.clock[0] += 60
        again = lifecycle.observe(attached, idle(), verified_owner=True)
        assert again["status"] == "ACTIVE" and again["generation"] == 2
        assert again["last_activity_at"] == attached.attachment_at
        assert len(case.calls) == 2

    # Genuine inactivity eventually resumes dormancy; the old proof is then stale.
    case.clock[0] = attached.attachment_at + TTL_SECONDS
    state = case.lifecycle.observe(attached, idle(), verified_owner=True)
    assert state["status"] == "DORMANT"
    case.clock[0] += 60
    state = case.lifecycle.observe(attached, idle(), verified_owner=True)
    assert state["status"] == "DORMANT" and state["generation"] == 2
    assert len(case.calls) == 2


@pytest.mark.parametrize("kind", ["malformed", "future", "stale", "boolean"])
def test_invalid_attachment_proof_is_ignored_without_blocking_other_work(lifecycle_case, kind):
    case = lifecycle_case
    make_dormant(case)
    now = case.clock[0]
    value = {"malformed": "not-a-time", "future": now + 100,
             "stale": now - TTL_SECONDS, "boolean": True}[kind]
    bad = replace(case.workspace, attachment_at=value)

    state = case.lifecycle.observe(bad, idle(), verified_owner=True)
    assert state["status"] == "DORMANT" and state["generation"] == 1
    assert len(case.calls) == 1

    # Independent genuine activity in the same observation still proceeds.
    state = case.lifecycle.observe(bad, idle(now - 1), verified_owner=True)
    assert state["status"] == "ACTIVE" and state["generation"] == 2


@pytest.mark.parametrize("verified,exact", [(False, True), (True, False)])
def test_unverified_owner_or_inexact_signals_cannot_activate(lifecycle_case, verified, exact):
    case = lifecycle_case
    make_dormant(case)
    attached = replace(case.workspace, attachment_at=case.clock[0] - 5)
    signals = dict(idle(), exact=exact)

    state = case.lifecycle.observe(attached, signals, verified_owner=verified)

    assert state["status"] == "UNKNOWN"
    assert not case.lifecycle.is_active(THREAD)
    assert len(case.calls) == 1


def test_attachment_activity_tolerates_missing_attribute(lifecycle_case):
    store = lifecycle_case.lifecycle._store()
    assert _attachment_activity(SimpleNamespace(session_id=THREAD), store) is None
    assert _attachment_activity(lifecycle_case.workspace, store) is None


# Compatibility -----------------------------------------------------------------

def test_tracked_workspace_persisted_shape_and_equality_unchanged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base = TrackedWorkspace.create("fixture", repo, THREAD)
    stamped = replace(base, attachment_at=1234.5)

    assert set(stamped.to_dict()) == set(_ENTRY_KEYS)
    assert stamped.to_dict() == base.to_dict()
    restored = TrackedWorkspace.from_dict(stamped.to_dict())
    assert restored.attachment_at is None and restored == base == stamped
    assert hash(stamped) == hash(base)
    assert "attachment_at" not in repr(stamped)
    positional = TrackedWorkspace(base.workspace_id, base.repo_root, base.session_id,
                                  base.execution_locality, base.registered_at)
    assert positional == base and positional.attachment_at is None
    with pytest.raises(WorkspaceRegistryFormatError):
        TrackedWorkspace.from_dict(dict(base.to_dict(), attachment_at=1234.5))


def test_session_resolution_shape_and_equality_unchanged():
    old = SessionResolution("resolved", SID, HISTORY_ATTACHED_SOURCE, None)
    stamped = SessionResolution("resolved", SID, HISTORY_ATTACHED_SOURCE, None, 1234.5)

    assert old.attachment_at is None
    assert old == stamped and hash(old) == hash(stamped)
    assert stamped.to_dict() == old.to_dict() == {
        "status": "resolved", "session_id": SID,
        "source": HISTORY_ATTACHED_SOURCE, "reason": None,
    }
    assert "attachment_at" not in repr(stamped)
