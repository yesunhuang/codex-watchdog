"""Portable relay state for exact sessions sharing an opted-in Linux home.

Provider journals share one namespace on the existing same-user filesystem.
Native control records and transports remain node-local. Import is an explicit
offline operation; neither a handoff nor a polling tick copies old journals.
"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from functools import wraps
from pathlib import Path
import threading

from .control_context import current_control, current_effect
from .control_state import ControlBusy, ControlError, control_read_json, control_atomic_json
from .node_observation import observation_lock
from .models import sha256_text
from .storage import StoreBusyError


# A real native event exceeded 1 MiB. Four rows per provider still cap a
# reconciliation cycle at twelve reads (24 MiB); larger records remain unread.
_NATIVE_COMPLETION_BUDGET = 2 * 1024 * 1024


def relay_guarded(method):
    """Fence socket ingress too, before its first ticket/UUID mutation."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        authority = getattr(self, "relay_authority", None)
        if authority is None:
            return method(self, *args, **kwargs)
        try:
            with authority.guard():
                return method(self, *args, **kwargs)
        except ControlBusy:
            from .relay import ReplyResult
            return ReplyResult("deferred", delivery_status="relay_authority_not_current")
    return guarded


def relay_ack_guarded(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        authority = getattr(self, "relay_authority", None)
        try:
            from .notification_senders import sender_fence
            with authority.guard() if authority is not None else nullcontext():
                runtime = authority.runtime if authority is not None else self.runtime
                with sender_fence(runtime):
                    return method(self, *args, **kwargs)
        except (ControlBusy, StoreBusyError):
            return None  # Never replay a command because its ACK was fenced.
    return guarded


def sender_guarded(method):
    """Exclude control sends from their durable claim through send/commit.

    Exclusion is not targeting authority. Existing exact grants, owner guards
    and one-shot claims still authorize every effect.
    """
    @wraps(method)
    def guarded(self, *args, **kwargs):
        from .notification_senders import sender_fence
        owner = getattr(self, "relay", self)
        authority = getattr(owner, "relay_authority", None)
        try:
            with authority.guard() if authority is not None else nullcontext():
                runtime = authority.runtime if authority is not None else owner.runtime
                with sender_fence(runtime):
                    return method(self, *args, **kwargs)
        except (ControlBusy, StoreBusyError):
            from .relay import ReplyResult
            return ReplyResult("deferred", delivery_status="notification_sender_busy")
    return guarded


class SessionRelayAuthority:
    def __init__(self, store, token, observation, workspace_id):
        self.store, self.token, self.observation = store, dict(token), observation
        self.thread_id, self.repo_path = store.thread_id, store.repo_path
        self.workspace_id = workspace_id
        self.root = store.codex_home / "watchdog-relay-authority"
        self.runtime = self.root / "runtime"
        self.notification_runtime = self.runtime / "session-notifications" / self.thread_id
        self.manifest = self.root / "sessions" / (self.thread_id + ".json")
        self.expected = None
        self._local = threading.local()

    def _ready(self, *, enroll=False):
        if (self.root.is_symlink() or self.root.resolve() != self.root
                or self.manifest.is_symlink() or self.manifest.parent.resolve() != self.manifest.parent
                or self.runtime.resolve() != self.runtime
                or (self.runtime / "locks").resolve() != self.runtime / "locks"):
            raise ControlError("relay_authority_path_invalid")
        if self.manifest.exists():
            selected = control_read_json(self.manifest)
            from .session_authority_retention import STORAGE, selected_session_runtime
            if selected.get("storage") == STORAGE:
                self.runtime = selected_session_runtime(self, selected)
                notification = "runtime/session-notifications/" + self.thread_id
                if selected.get("notifications_relative") != notification:
                    raise ControlError("session_authority_notification_path_invalid")
                self.notification_runtime = self.root / notification
                if selected.get("notification_plan_sha256") is not None:
                    from .relay_notification_migration import validate_notification_migration
                    validate_notification_migration(self.notification_runtime,
                                                    selected["notification_plan_sha256"])
                return
        # The old cluster/default stays usable by its unknown/unmigrated
        # sessions. Only the exact session manifest selects isolated storage.
        self.runtime = self.root / "runtime"
        self.notification_runtime = self.runtime / "session-notifications" / self.thread_id
        cluster = self.root / "cluster.json"
        if not cluster.exists():
            if not enroll:
                raise ControlBusy("relay_authority_migration_required")
            from .linux_relay_migration import initialize_empty_authority
            initialize_empty_authority(self.store.codex_home)
        value = control_read_json(cluster)
        if (value.get("state") != "ready" or value.get("codex_home") != str(self.store.codex_home)
                or value.get("storage") != "cluster_provider_journals_v1"):
            raise ControlError("relay_authority_cluster_invalid")
        if not self.manifest.exists():
            if not enroll:
                raise ControlBusy("relay_authority_migration_required")
            if self.thread_id in value.get("initial_sessions", {}):
                raise ControlError("relay_authority_manifest_missing")
            # Enrollment follows exact native observation, never a relay
            # target, hostname preference, or guessed replacement session.
            self.observation.capture_relay_epoch()
            control_atomic_json(self.manifest, dict(schema_version=1, thread_id=self.thread_id,
                repo_path=self.repo_path, state="ready", storage="cluster_provider_journals_v1",
                plan_sha256=value.get("plan_sha256")))
        value = control_read_json(self.manifest)
        if (value.get("thread_id") != self.thread_id
                or value.get("repo_path") != self.repo_path
                or value.get("state") != "ready"
                or value.get("storage") != "cluster_provider_journals_v1"):
            raise ControlError("relay_authority_manifest_invalid")
        if value.get("notification_plan_sha256") is not None:
            from .relay_notification_migration import validate_notification_migration
            validate_notification_migration(self.notification_runtime,
                                            value["notification_plan_sha256"])

    def activate(self):
        """Called only after NodeObservation admits its native owner proof."""
        self._ready(enroll=True)
        self.expected = self.observation.capture_relay_epoch()
        with self.guard():
            # Bind runs before admission, so only activation may prepare these
            # fixed parents. The selected sender fence creates the UUID leaf;
            # fresh notification entry alone creates its notifications state.
            from .notification_senders import _directory_id, _ensure_directory
            runtime = self.root / "runtime"
            parent = runtime / "session-notifications"
            if self.notification_runtime != parent / self.thread_id:
                raise ControlError("relay_authority_notification_path_invalid")
            try:
                root_id = _directory_id(self.root)
                runtime_id = _ensure_directory(runtime)
                _ensure_directory(parent)
                if (_directory_id(self.root) != root_id
                        or _directory_id(runtime) != runtime_id):
                    raise ValueError("relay authority notification parent changed")
            except (OSError, ValueError) as exc:
                raise ControlError("relay_authority_notification_path_invalid") from exc

    @contextmanager
    def guard(self):
        with observation_lock(self.observation.lock_path):
            if getattr(self._local, "depth", 0) == 0:
                self._ready()
            if self.expected is None:
                raise ControlBusy("relay_authority_not_selected")
            value = self.observation._read()
            current = {key: value.get(key) for key in ("relay_epoch", "relay_owner")} if value else None
            if current != self.expected:
                raise ControlBusy("relay_authority_stale_epoch")
            # A native effect may already hold this exact node-local lock.
            # Compose its capability instead of acquiring a second descriptor.
            selected = current_control()
            if selected is not None and selected[0].path == self.store.path and selected[1] == self.token:
                with current_effect():
                    if self.store.read().get("auto_paused") is True:
                        raise ControlBusy("relay_authority_paused")
            else:
                with self.store.guard(self.token) as local:
                    if local.get("auto_paused") is True:
                        raise ControlBusy("relay_authority_paused")
            depth = getattr(self._local, "depth", 0)
            # Current selection, capability and held locks fence this effect.
            # A historical transcript stamp is forensic evidence, not a lease
            # for new independent traffic or an executor handoff prerequisite.
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth

    @contextmanager
    def journal_lock(self, provider):
        # Observation -> provider journal -> SQLite. Reentrant locks use one
        # descriptor per path, avoiding lockf's close/unlock trap on GPFS.
        with self.guard():
            if provider not in ("slack", "lark", "onebot"):
                raise ControlError("relay_authority_provider_invalid")
            path = self.runtime / provider / "reply-tickets.sqlite3"
            if path.is_symlink() or path.resolve() != path:
                raise ControlError("relay_authority_path_invalid")
            selected = control_read_json(self.manifest)
            from .session_authority_retention import STORAGE
            configured = (selected if selected.get("storage") == STORAGE
                          else control_read_json(self.root / "cluster.json"))
            providers = configured.get("providers", ())
            if not isinstance(providers, (list, tuple)):
                raise ControlError("relay_authority_cluster_invalid")
            if provider in providers and not path.is_file():
                raise ControlError("relay_authority_database_missing")
            with observation_lock(self.runtime / "locks" / (provider + "-authority.lock")):
                yield

    def journal(self, provider):
        from .legacy_reply_tickets import select_reply_journal as ReplyTickets
        local_runtime = Path(self.store.read()["runtime_path"])
        path = local_runtime / provider / "authority-cursors.json"
        retention = getattr(self, "retention_binding", None)
        return ReplyTickets(path, local_runtime / "locks" / (provider + "-authority-cursors.lock"),
                            provider, local_runtime, lambda: {}, authority=self,
                            before_prune=getattr(retention, "before_prune", None),
                            retention_fence=getattr(retention, "fence", None))

    def read_cursor(self, provider, key, default):
        journal = self.journal(provider)
        with journal.transaction() as db:
            entry = journal.get(db, "relay_cursors", self.thread_id + ":" + key)
        return deepcopy(default if entry is None else entry["value"])

    def write_cursor(self, provider, key, state):
        journal = self.journal(provider)
        with journal.transaction() as db:
            journal.put(db, "relay_cursors", self.thread_id + ":" + key,
                        dict(thread_id=self.thread_id, value=deepcopy(state)))

    def record_native_delivery(self, thread_store, event_key, receipt, *, instruction_id=None,
                               prompt_sha256=None):
        """Best-effort bounded evidence; never discard an admitted dispatch ACK."""
        try:
            return self._record_native_delivery(thread_store, event_key, receipt,
                instruction_id=instruction_id, prompt_sha256=prompt_sha256)
        except Exception:
            # The original event/UUID reservation remains terminal for
            # automatic attempts even when optional evidence cannot be saved.
            return False

    def _record_native_delivery(self, thread_store, event_key, receipt, *, instruction_id=None,
                                prompt_sha256=None):
        from .relay_native_completion import completion_progress
        from .relay_completion_reconciliation import _BOUNDED_VALUE, _small_json
        # Validation is bounded and read-only. It never turns a transport ACK
        # or a disappeared queue row into model completion.
        if not isinstance(receipt, dict) or receipt.get("thread_id") != self.thread_id:
            raise ControlError("relay_authority_native_receipt_mismatch")
        fields = ("thread_id", "instruction_id", "prompt_sha256", "rollout_path",
                  "rollout_baseline_offset", "queue_message_id")
        receipt = {field: receipt.get(field) for field in fields}
        if any(isinstance(value, str) and len(value) > 4096 for value in receipt.values()):
            raise ControlError("relay_authority_native_receipt_invalid")
        progress, _ = completion_progress(receipt, budget=32)
        if progress.get("invalid"):
            raise ControlError("relay_authority_native_receipt_invalid")
        journal = thread_store.journal
        with journal.transaction() as db:
            key = sha256_text(event_key)
            row = db.execute("SELECT " + _BOUNDED_VALUE + ",thread_id FROM records "
                             "WHERE namespace=? AND kind='events' AND key=?",
                             (journal.namespace, key)).fetchone()
            event = _small_json(row[0]) if row else None
            if (journal.authority is not self or not isinstance(event, dict)
                    or row[1] != self.thread_id
                    or event.get("relay_authority_quarantine") is True
                    or event.get("instruction_id") != receipt["instruction_id"]
                    or event.get("text_sha256") != receipt["prompt_sha256"]
                    or instruction_id is not None and instruction_id != receipt["instruction_id"]
                    or prompt_sha256 is not None and prompt_sha256 != receipt["prompt_sha256"]):
                raise ControlError("relay_authority_native_receipt_mismatch")
            old_row = db.execute("SELECT " + _BOUNDED_VALUE + " FROM records "
                "WHERE namespace=? AND kind='relay_native_receipts' AND key=?",
                (journal.namespace, key)).fetchone()
            old = _small_json(old_row[0]) if old_row else None
            value = dict(thread_id=self.thread_id, receipt=deepcopy(receipt), progress=progress)
            if old is not None:
                if old.get("progress", {}).get("receipt_sha256") != progress.get("receipt_sha256"):
                    raise ControlError("relay_authority_native_receipt_collision")
                return True
            journal.put(db, "relay_native_receipts", key, value)
        return True

    def reconcile_completions(self):
        """Bounded optional forensics, isolated by provider and original row."""
        from .relay_completion_reconciliation import _BOUNDED_VALUE, _small_json, _settle
        result = dict(examined=0, completed=0, refusals=[])
        with self.guard():
            for provider in ("slack", "lark", "onebot"):
                database = self.runtime / provider / "reply-tickets.sqlite3"
                if not database.exists():
                    continue
                try:
                    journal = self.journal(provider)
                    examined, completed, refusals = 0, 0, []
                    with journal.transaction() as db:
                        saved = db.execute("SELECT " + _BOUNDED_VALUE + " FROM records "
                            "WHERE namespace=? AND kind='relay_reconcile_cursor' AND key=?",
                            (journal.namespace, self.thread_id)).fetchone()
                        cursor = _small_json(saved[0]) if saved else {}
                        if not isinstance(cursor, dict):
                            raise ValueError("relay_completion_cursor_invalid")
                        after = cursor.get("after", ("", ""))
                        if (not isinstance(after, (list, tuple)) or len(after) != 2
                                or not all(isinstance(item, str) and len(item) <= 1024 for item in after)):
                            raise ValueError("relay_completion_cursor_invalid")
                        query = ("SELECT namespace,key," + _BOUNDED_VALUE + " FROM records "
                                 "INDEXED BY session_relay_pending "
                                 "WHERE thread_id=? AND active=1 AND kind='events' ")
                        # Two index seeks and at most four bounded rows/provider.
                        rows = db.execute(query + "AND (namespace,key)>(?,?) ORDER BY namespace,key LIMIT 4",
                                          (self.thread_id, *after)).fetchall()
                        if len(rows) < 4:
                            rows += db.execute(query + "AND (namespace,key)<=(?,?) ORDER BY namespace,key LIMIT ?",
                                               (self.thread_id, *after, 4-len(rows))).fetchall()
                        for namespace, key, raw in rows:
                            try:
                                completed += int(_settle(db, self.store, namespace, key, raw,
                                                        _NATIVE_COMPLETION_BUDGET))
                            except Exception:
                                refusals.append(dict(provider=provider, reason="relay_completion_record_unavailable"))
                            examined += 1
                            cursor = dict(cursor, after=[namespace, key], thread_id=self.thread_id)
                            journal.put(db, "relay_reconcile_cursor", self.thread_id, cursor)
                    result["completed"] += completed
                    result["examined"] += examined
                    result["refusals"] += refusals
                except Exception:
                    # No provider or callback fault makes old unknown outcomes
                    # a prerequisite for fresh traffic through another journal.
                    result["refusals"].append(dict(provider=provider, reason="relay_completion_journal_unavailable"))
        return result

    def _queue_absent(self, receipt):
        from contextlib import closing
        import sqlite3
        paths = list(self.store.codex_home.glob("queue_*.sqlite"))
        if len(paths) != 1:
            return False
        try:
            with closing(sqlite3.connect(paths[0].as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                return db.execute("SELECT 1 FROM queued_items WHERE thread_id=? AND id=?",
                    (self.thread_id, receipt["queue_message_id"])).fetchone() is None
        except (sqlite3.Error, KeyError):
            return False

    def resolve_target(self, target):
        """Leave the immutable envelope intact; resolve only execution routing."""
        with self.guard():
            return self._resolve_target(target)

    def _resolve_target(self, target):
        if (target.thread_id != self.thread_id or target.workspace_id != self.workspace_id
                or target.execution_locality != "remote_ssh"
                or str(Path(target.remote_repo_path).resolve()) != self.repo_path):
            raise ControlError("relay_authority_exact_target_mismatch")
        from .remote_ssh import RemoteSshTarget
        local = self.store.read()["remote_target"]
        if local["repo_path"] != self.repo_path:
            raise ControlError("relay_authority_execution_target_mismatch")
        return RemoteSshTarget(local["authority"], self.repo_path, local["storage_key"], (self.thread_id,))

    def handoff_safe(self):
        """Preserve actual native FIFO work, independently of old audit outcomes."""
        from .linux_owner import pending_count
        try:
            if pending_count(self.store.codex_home, self.thread_id):
                return False
        except ValueError:
            return False
        return True


def bind_service_authority(service, authority):
    """Attach portable storage without changing the service's execution runtime."""
    notifier = getattr(service, "notifier", None)
    if notifier is not None:
        notifier.relay_authority = authority
        # Notification receipts are session-scoped; a handoff does not resend
        # a result that was already sent, or retry an uncertain provider effect.
        notifier.relay_runtime = authority.notification_runtime
        notifier.state_path = notifier.relay_runtime / "notifications" / "last-events.json"
        stores = [getattr(notifier, name, None) for name in
                  ("slack_thread_store", "lark_thread_store", "onebot_thread_store")]
        for store in stores:
            if store is not None:
                store.relay_authority = authority
    relay = getattr(service, "slack_reply_relay", None)
    if relay is not None:
        for item in getattr(relay, "relays", (relay,)):
            item.relay_authority = authority
            item.thread_store.relay_authority = authority
