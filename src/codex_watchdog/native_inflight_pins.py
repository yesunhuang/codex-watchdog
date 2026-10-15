"""Current exact-session pin associations, separate from provider schemas.

The sidecar and provider protection flags share the caller's SQLite transaction.
The caller holds the existing native-owner/observation/sender fences and provider
lock until commit.  No native record, auth value or receipt status is changed.
SQLite multi-file atomicity requires a file-backed rollback journal, not WAL.
See https://www.sqlite.org/lang_attach.html .
"""
from contextlib import closing, contextmanager, nullcontext
import json
import os
from pathlib import Path
import sqlite3

from .native_inflight import (NativeInflightError, MAX_REFERENCES, _digest, _json,
    _load, _live_work, _now, _scope, _sha, _uuid, _work, resolve_snapshot_pins,
    validate_inventory, validate_queue_inventory)
from .recent_state import RECENT_KINDS
from .storage import FileLock


_ALIAS = "watchdog_native_pins"


def _reference(ref):
    if (not isinstance(ref, (list, tuple)) or len(ref) != 3
            or any(not isinstance(v, str) or not 1 <= len(v) <= 512 or "\0" in v for v in ref)
            or ref[1] not in RECENT_KINDS
            or ref[0].split("/", 1)[0] not in ("slack", "lark", "onebot")
            or ".." in Path(ref[0]).parts or Path(ref[0]).is_absolute()):
        raise NativeInflightError("native_inflight_pin_reference_invalid")
    return tuple(ref)


def _association(value):
    if (not isinstance(value, dict) or _work(value) is None
            or value.get("queue_id") is not None and not _uuid(value["queue_id"])
            or not isinstance(value.get("refs"), list) or not 1 <= len(value["refs"]) <= MAX_REFERENCES):
        raise NativeInflightError("native_inflight_association_invalid")
    refs = [_reference(ref) for ref in value["refs"]]
    if len(set(refs)) != len(refs) or len({ref[0].split("/", 1)[0] for ref in refs}) != 1:
        raise NativeInflightError("native_inflight_association_invalid")
    result = dict(value)
    result["refs"] = [list(ref) for ref in sorted(refs)]
    if len(_json(result).encode("utf-8")) > 65536:
        raise NativeInflightError("native_inflight_association_too_large")
    return result


def _identity(value):
    return _sha(value["instruction_id"] + "\0" + value["prompt_sha256"])


def snapshot_associations(snapshot, inventory):
    """Explicit cold-export adapter; never called by ordinary maintenance.

    H-sized row discovery is confined to this migration operation.  Repeated
    per-work joins below are over the exported in-memory snapshot, not native
    history or a polling database.  No uncorrelated historical status is pinned.
    """
    rows = snapshot.get("records", snapshot.get("rows")) if isinstance(snapshot, dict) else snapshot
    all_pins = resolve_snapshot_pins(rows, inventory)
    results = []
    for work in sorted(_live_work(inventory)):
        # The resolver uses live identity only; isolate that identity without
        # changing any source row, callback boundary or native proof.
        narrowed = dict(inventory)
        narrowed["queue"] = dict(inventory["queue"], items=[item for item in inventory["queue"]["items"] if _work(item) == work])
        if _work(inventory["native"]) != work:
            narrowed["native"] = dict(status="idle", turn_id=None, instruction_id=None,
                                       prompt_sha256=None, input_sha256=None)
        narrowed["proof_sha256"] = _sha(_json({k: v for k, v in narrowed.items() if k != "proof_sha256"}))
        refs = resolve_snapshot_pins(rows, narrowed) & all_pins
        by_provider = {}
        for ref in refs:
            by_provider.setdefault(ref[0].split("/", 1)[0], []).append(list(ref))
        queue_ids = [item["queue_id"] for item in inventory["queue"]["items"] if _work(item) == work]
        if len(queue_ids) > 1:
            raise NativeInflightError("native_inflight_queue_identity_ambiguous")
        for provider_refs in by_provider.values():
            results.append(_association(dict(instruction_id=work[0], prompt_sha256=work[1],
                queue_id=queue_ids[0] if queue_ids else None, refs=provider_refs)))
    return results


class CurrentNativePins:
    """A compact current-work sidecar; retired bindings are deleted, not archived.

    ``db`` is the caller's already-fenced provider BEGIN IMMEDIATE transaction.
    Keep that transaction/fence through commit.  This object neither commits it
    nor calls ReplyTickets.transaction (which may prune before pin installation).
    The retirement cursor is process-local; restart safely begins a new bounded
    pass.  Unknown inventory never clears a pin, advances that pass or writes.
    """
    def __init__(self, runtime, home, thread, repo, *, clock=None):
        self.runtime = Path(runtime)
        self.scope = _scope(home, thread, repo)
        if not self.runtime.is_absolute() or self.runtime.resolve() != self.runtime:
            raise NativeInflightError("native_inflight_sidecar_path_invalid")
        self.path = self.runtime / "native-inflight" / (thread + ".sqlite3")
        self.lock = self.path.with_suffix(".lock")
        self.clock = _now if clock is None else clock
        self._cursor = {}

    def _validate(self, inventory):
        return validate_inventory(inventory, self.scope["codex_home"], self.scope["thread_id"],
                                  self.scope["repo_path"], clock=self.clock)

    def _meta(self):
        return dict(schema_version=1, kind="current_native_pins", **self.scope)

    @staticmethod
    def _schema(db, alias=_ALIAS):
        # Version alone is not a proof that LIMIT uses a current-state index.
        # Validate only fixed schema metadata, before reading any binding row.
        if alias not in (_ALIAS, "main"):
            raise NativeInflightError("native_inflight_sidecar_schema_invalid")
        expected = {
            "metadata": [("slot",1),("value",0)],
            "bindings": [("provider",1),("identity",2),("value",0)],
            "pin_refs": [("provider",1),("identity",2),("namespace",3),("kind",4),("key",5)],
        }
        for table, shape in expected.items():
            rows = db.execute("PRAGMA " + alias + ".table_info(" + table + ")").fetchmany(65)
            if [(row[1],row[5]) for row in rows] != shape:
                raise NativeInflightError("native_inflight_sidecar_schema_invalid")
        index = db.execute("PRAGMA " + alias + ".index_info(pin_reference)").fetchmany(6)
        if [row[2] for row in index] != ["provider","namespace","kind","key"]:
            raise NativeInflightError("native_inflight_sidecar_schema_invalid")

    @contextmanager
    def _attached(self, db, fence):
        # Best-effort metadata callers may catch our refusal and still commit
        # unrelated ingress.  A savepoint prevents a half-applied pin mutation.
        if not callable(fence) or not db.in_transaction:
            raise NativeInflightError("native_inflight_atomic_transaction_required")
        with fence():
            db.execute("SAVEPOINT native_pin_action")
            try:
                with self._attached_raw(db, nullcontext) as provider:
                    yield provider
            except BaseException:
                db.execute("ROLLBACK TO native_pin_action")
                db.execute("RELEASE native_pin_action")
                raise
            else:
                db.execute("RELEASE native_pin_action")

    @contextmanager
    def _attached_raw(self, db, fence):
        if not callable(fence):
            raise NativeInflightError("native_inflight_fence_required")
        with fence(), FileLock(self.lock):
            files = {name: path for _, name, path in db.execute("PRAGMA database_list")}
            main = Path(files.get("main", ""))
            provider = main.parent.name
            if (not db.in_transaction or provider not in ("slack", "lark", "onebot")
                    or main != self.runtime / provider / "reply-tickets.sqlite3"
                    or main.is_symlink() or main.resolve() != main
                    or db.execute("PRAGMA main.user_version").fetchone()[0] != 3
                    or db.execute("PRAGMA main.journal_mode").fetchone()[0] not in ("delete", "truncate", "persist")
                    or db.execute("PRAGMA main.synchronous").fetchone()[0] < 2):
                raise NativeInflightError("native_inflight_atomic_transaction_required")
            if self.path.is_symlink() or self.path.parent.resolve() != self.path.parent:
                raise NativeInflightError("native_inflight_sidecar_path_invalid")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if _ALIAS in files:
                if files[_ALIAS] != str(self.path):
                    raise NativeInflightError("native_inflight_attached_scope_mismatch")
            else:
                if not self.path.exists():
                    try:
                        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        os.close(fd)
                    except FileExistsError:
                        pass
                db.execute("ATTACH DATABASE ? AS " + _ALIAS, (str(self.path),))
            if (db.execute("PRAGMA " + _ALIAS + ".journal_mode").fetchone()[0] not in ("delete", "truncate", "persist")
                    or db.execute("PRAGMA " + _ALIAS + ".synchronous").fetchone()[0] < 2):
                raise NativeInflightError("native_inflight_atomic_transaction_required")
            version = db.execute("PRAGMA " + _ALIAS + ".user_version").fetchone()[0]
            if version == 0:
                existing = db.execute("SELECT name FROM " + _ALIAS + ".sqlite_master WHERE type='table' LIMIT 1").fetchone()
                if existing is not None:
                    raise NativeInflightError("native_inflight_sidecar_schema_invalid")
                db.execute("CREATE TABLE " + _ALIAS + ".metadata(slot INTEGER PRIMARY KEY,value TEXT NOT NULL)")
                db.execute("INSERT INTO " + _ALIAS + ".metadata VALUES(1,?)", (_json(self._meta()),))
                db.execute("CREATE TABLE " + _ALIAS + ".bindings(provider TEXT NOT NULL,identity TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(provider,identity))")
                db.execute("CREATE TABLE " + _ALIAS + ".pin_refs(provider TEXT NOT NULL,identity TEXT NOT NULL,namespace TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,PRIMARY KEY(provider,identity,namespace,kind,key))")
                db.execute("CREATE INDEX " + _ALIAS + ".pin_reference ON pin_refs(provider,namespace,kind,key)")
                db.execute("PRAGMA " + _ALIAS + ".user_version=1")
            elif version != 1:
                raise NativeInflightError("native_inflight_sidecar_schema_invalid")
            self._schema(db)
            meta_row = db.execute("SELECT CASE WHEN length(CAST(value AS BLOB))<=16384 THEN value ELSE NULL END FROM " + _ALIAS + ".metadata WHERE slot=1").fetchone()
            meta = _load(meta_row[0]) if meta_row else None
            if not isinstance(meta, dict) or any(meta.get(k) != v for k, v in self._meta().items()):
                raise NativeInflightError("native_inflight_attached_scope_mismatch")
            yield provider

    def _read_refs(self, db, refs):
        rows = []
        for namespace, kind, key in refs:
            row = db.execute("SELECT namespace,kind,key,CASE WHEN length(CAST(value AS BLOB))<=65536 THEN value ELSE NULL END,thread_id "
                "FROM main.records WHERE namespace=? AND kind=? AND key=?", (namespace, kind, key)).fetchone()
            if row is None or not isinstance(row[3], str):
                raise NativeInflightError("native_inflight_pin_record_unavailable")
            rows.append(dict(zip(("namespace", "kind", "key", "value", "thread_id"), row)))
        return rows

    def register(self, association, inventory, *, db, fence):
        """Pin a positively current admission's known PKs, atomically with binding."""
        validate_queue_inventory(inventory, self.scope["codex_home"], self.scope["thread_id"],
                                 self.scope["repo_path"], clock=self.clock)
        value = _association(association)
        if _work(value) not in _live_work(inventory):
            raise NativeInflightError("native_inflight_work_not_current")
        queue = {item["queue_id"]: _work(item) for item in inventory["queue"]["items"]}
        if value["queue_id"] in queue and queue[value["queue_id"]] != _work(value):
            raise NativeInflightError("native_inflight_queue_identity_mismatch")
        with self._attached(db, fence) as provider:
            validate_queue_inventory(inventory, self.scope["codex_home"], self.scope["thread_id"],
                                     self.scope["repo_path"], clock=self.clock)
            refs = {_reference(ref) for ref in value["refs"]}
            if any(ref[0].split("/", 1)[0] != provider for ref in refs):
                raise NativeInflightError("native_inflight_provider_mismatch")
            resolved = resolve_snapshot_pins(self._read_refs(db, refs), inventory, queue_only=True, work=_work(value))
            if refs != resolved:
                raise NativeInflightError("native_inflight_pin_reference_invalid")
            identity = _identity(value)
            old = db.execute("SELECT CASE WHEN length(CAST(value AS BLOB))<=65536 THEN value ELSE NULL END FROM " + _ALIAS + ".bindings WHERE provider=? AND identity=?", (provider, identity)).fetchone()
            if old is not None:
                previous = _association(_load(old[0]))
                if any(previous.get(k) != value.get(k) for k in ("instruction_id", "prompt_sha256", "queue_id")):
                    raise NativeInflightError("native_inflight_association_collision")
                # Later primary ACK/native metadata may add proven references;
                # existing identity and unknown nonconflicting fields survive.
                for key, item in value.items():
                    if key != "refs" and key in previous and previous[key] != item:
                        raise NativeInflightError("native_inflight_association_collision")
                value = dict(previous, **value)
                value["refs"] = [list(ref) for ref in sorted(refs | {_reference(ref) for ref in previous["refs"]})]
                value = _association(value)
                if resolve_snapshot_pins(self._read_refs(db, value["refs"]), inventory, queue_only=True, work=_work(value)) != {_reference(ref) for ref in value["refs"]}:
                    raise NativeInflightError("native_inflight_pin_reference_invalid")
            db.execute("INSERT INTO " + _ALIAS + ".bindings VALUES(?,?,?) ON CONFLICT(provider,identity) DO UPDATE SET value=excluded.value", (provider, identity, _json(value)))
            for ref in value["refs"]:
                row = db.execute("SELECT session FROM main.recent_records WHERE namespace=? AND kind=? AND key=?", ref).fetchone()
                if row is None or row[0] not in ("", self.scope["thread_id"]):
                    raise NativeInflightError("native_inflight_recent_metadata_unavailable")
                db.execute("UPDATE main.recent_records SET protected=1 WHERE namespace=? AND kind=? AND key=?", ref)
                db.execute("INSERT OR IGNORE INTO " + _ALIAS + ".pin_refs VALUES(?,?,?,?,?)", (provider, identity, *ref))
            return dict(status="registered", protected=len(value["refs"]), identity=identity)

    def reconcile(self, inventory, *, db, fence, budget=32):
        """Retire only absent current work; no receipt completion is manufactured."""
        if isinstance(inventory, dict) and inventory.get("status") == "unknown":
            return dict(status="unknown", examined=0, retired=0)
        self._validate(inventory)
        if type(budget) is not int or not 1 <= budget <= 64:
            raise NativeInflightError("native_inflight_retirement_budget_invalid")
        with self._attached(db, fence) as provider:
            self._validate(inventory)
            live = _live_work(inventory)
            queued = {item["queue_id"]: _work(item) for item in inventory["queue"]["items"]}
            rows = db.execute("SELECT identity,CASE WHEN length(CAST(value AS BLOB))<=65536 THEN value ELSE NULL END FROM " + _ALIAS + ".bindings WHERE provider=? AND identity>? ORDER BY identity LIMIT ?",
                              (provider, self._cursor.get(provider, ""), budget)).fetchall()
            retired = 0
            for identity, raw in rows:
                value = _association(_load(raw))
                if _identity(value) != identity:
                    raise NativeInflightError("native_inflight_association_collision")
                refs = db.execute("SELECT namespace,kind,key FROM " + _ALIAS + ".pin_refs WHERE provider=? AND identity=? ORDER BY namespace,kind,key LIMIT ?",
                                  (provider, identity, MAX_REFERENCES + 1)).fetchall()
                if refs != sorted(_reference(ref) for ref in value["refs"]):
                    raise NativeInflightError("native_inflight_pin_reference_invalid")
                if value["queue_id"] in queued and queued[value["queue_id"]] != _work(value):
                    raise NativeInflightError("native_inflight_queue_identity_mismatch")
                if _work(value) not in live and value["queue_id"] not in queued:
                    db.execute("DELETE FROM " + _ALIAS + ".bindings WHERE provider=? AND identity=?", (provider, identity))
                    db.execute("DELETE FROM " + _ALIAS + ".pin_refs WHERE provider=? AND identity=?", (provider, identity))
                    for ref in value["refs"]:
                        remains = db.execute("SELECT 1 FROM " + _ALIAS + ".pin_refs INDEXED BY pin_reference WHERE provider=? AND namespace=? AND kind=? AND key=? LIMIT 1", (provider, *ref)).fetchone()
                        if remains is None:
                            db.execute("UPDATE main.recent_records SET protected=0 WHERE namespace=? AND kind=? AND key=? AND session IN ('',?)", (*ref, self.scope["thread_id"]))
                    retired += 1
                self._cursor[provider] = identity
            if len(rows) < budget:
                self._cursor.pop(provider, None)
            return dict(status="reconciled", examined=len(rows), retired=retired)

    def export(self, *, limit=16384):
        """Explicit recovery snapshot of current bindings only, never providers."""
        if type(limit) is not int or not 1 <= limit <= 1048576:
            raise NativeInflightError("native_inflight_export_budget_invalid")
        if not self.path.exists():
            value = dict(**self._meta(), bindings=[])
        else:
            if self.path.is_symlink() or self.path.resolve() != self.path:
                raise NativeInflightError("native_inflight_sidecar_path_invalid")
            with FileLock(self.lock), closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise NativeInflightError("native_inflight_sidecar_schema_invalid")
                self._schema(db, "main")
                meta_row = db.execute("SELECT CASE WHEN length(CAST(value AS BLOB))<=16384 THEN value ELSE NULL END FROM metadata WHERE slot=1").fetchone()
                meta = _load(meta_row[0]) if meta_row else None
                if not isinstance(meta, dict) or any(meta.get(k) != v for k, v in self._meta().items()):
                    raise NativeInflightError("native_inflight_attached_scope_mismatch")
                rows = db.execute("SELECT provider,identity,CASE WHEN length(CAST(value AS BLOB))<=65536 THEN value ELSE NULL END FROM bindings ORDER BY provider,identity LIMIT ?", (limit + 1,)).fetchall()
                if len(rows) > limit:
                    raise NativeInflightError("native_inflight_export_budget")
                bindings = []
                for provider, identity, raw in rows:
                    binding = _association(_load(raw))
                    if _identity(binding) != identity or any(ref[0].split("/", 1)[0] != provider for ref in binding["refs"]):
                        raise NativeInflightError("native_inflight_association_collision")
                    bindings.append(dict(provider=provider, **binding))
                value = dict(meta, bindings=bindings)
        value["sha256"] = _sha(_json(value))
        return value

    def reinstall(self, snapshot, inventory, *, db, fence):
        """Recover bindings/pins in the same new-provider transaction, idempotently.

        Old protected flags are not proof.  Only associations still joined to
        actual current work are reinstalled.  No old sender starts here.
        """
        self._validate(inventory)
        if (not isinstance(snapshot, dict) or any(snapshot.get(k) != v for k, v in self._meta().items())
                or not isinstance(snapshot.get("bindings"), list) or len(snapshot["bindings"]) > 1048576
                or snapshot.get("sha256") != _sha(_json({k: v for k, v in snapshot.items() if k != "sha256"}))):
            raise NativeInflightError("native_inflight_recovery_invalid")
        main = Path(next(path for _, name, path in db.execute("PRAGMA database_list") if name == "main"))
        provider = main.parent.name
        registered = 0
        for raw in snapshot["bindings"]:
            value = _association(raw)
            if raw.get("provider") != provider:
                continue
            if _work(value) not in _live_work(inventory):
                continue
            value.pop("provider", None)
            self.register(value, inventory, db=db, fence=fence)
            registered += 1
        return dict(status="reinstalled", registered=registered)


def prepare_migration_pins(runtime, provider, snapshot, inventory, *, fence, clock=None):
    """Hydrate only staged CURRENT associations before manifest publication.

    The migration caller owns the stage and already holds its exact native and
    sender fences.  The source snapshot was exported in the explicit cold phase.
    Returns metadata-only recovery data/digest; no original shared DB is opened.
    """
    if provider not in ("slack", "lark", "onebot"):
        raise NativeInflightError("native_inflight_provider_mismatch")
    store = CurrentNativePins(Path(runtime), inventory["codex_home"], inventory["thread_id"],
                              inventory["repo_path"], clock=clock)
    store._validate(inventory)
    path = Path(runtime) / provider / "reply-tickets.sqlite3"
    if not path.is_file() or path.is_symlink() or path.resolve() != path:
        raise NativeInflightError("native_inflight_provider_unavailable")
    associations = snapshot_associations(snapshot, inventory)
    with fence(), closing(sqlite3.connect(path, timeout=0.25)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            for association in associations:
                if association["refs"][0][0].split("/", 1)[0] == provider:
                    store.register(association, inventory, db=db, fence=fence)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return store.export()
