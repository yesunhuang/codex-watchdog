"""Compact current suppression and bounded recent claims, one point file/key.

The current fingerprint prevents a stable observed state from notifying again
on restart. Past fingerprints are best-effort claims within a bounded horizon,
not a perpetual event archive. All callers hold the existing notification lock.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import time
import os

from .storage import InstructionStore

TTL_SECONDS = 72 * 60 * 60
RECENT_LIMIT = 256
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_BYTES = 128 * 1024


def _finite_time(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


class CurrentNotificationState:
    def __init__(self, runtime, *, clock=time.time, writer=InstructionStore._atomic_json):
        runtime = Path(runtime).expanduser().resolve()
        self.root = runtime / "notifications" / "current"
        self.database = runtime / "notifications" / "recent-notification-claims.sqlite3"
        self.clock, self.writer = clock, writer

    def path(self, key):
        if not isinstance(key, str) or _DIGEST.fullmatch(key) is None:
            raise ValueError("notification_current_key_invalid")
        path = self.root / (key + ".json")
        if path.is_symlink() or self.root.resolve() != self.root.absolute():
            raise ValueError("notification_current_path_invalid")
        return path

    def _now(self, previous=0):
        value = self.clock()
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value < 0):
            raise ValueError("notification_current_clock_invalid")
        return max(float(value), previous)

    def read(self, key):
        path = self.path(key)
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            return dict(schema_version=1, key=key, checked_at=0, current=None, recent=[])
        if len(raw) > _MAX_BYTES:
            raise ValueError("notification_current_state_oversized")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("key") != key or not isinstance(value.get("recent"), list)
                or len(value["recent"]) > RECENT_LIMIT
                or type(value.get("checked_at")) not in (int, float)
                or not math.isfinite(value["checked_at"]) or value["checked_at"] < 0):
            raise ValueError("notification_current_state_invalid")
        entries = value["recent"] + ([value["current"]] if value.get("current") is not None else [])
        for entry in entries:
            if (not isinstance(entry, dict) or _DIGEST.fullmatch(str(entry.get("fingerprint", ""))) is None
                    or type(entry.get("claimed_at")) not in (int, float)
                    or not math.isfinite(entry["claimed_at"]) or entry["claimed_at"] < 0
                    or entry["claimed_at"] > value["checked_at"]
                    or entry.get("status") not in ("claimed", "sent", "uncertain")):
                raise ValueError("notification_current_claim_invalid")
        return value

    def claim(self, key, fingerprint, *, scope=None):
        if not isinstance(fingerprint, str) or _DIGEST.fullmatch(fingerprint) is None:
            raise ValueError("notification_current_fingerprint_invalid")
        try:
            value = self.read(key)
        except (ValueError, OSError):
            # This point cache is suppression convenience, not authorization or
            # the external-attempt claim. Preserve its malformed bytes and let
            # a different fingerprint use the independent recent claim ledger.
            value = dict(schema_version=1, key=key, checked_at=0, current=None, recent=[])
        now = self._now(value["checked_at"])
        current = value.get("current")
        recent = [entry for entry in value["recent"] if now - entry["claimed_at"] < TTL_SECONDS]
        if current is not None and current["fingerprint"] == fingerprint:
            return False
        if any(entry["fingerprint"] == fingerprint for entry in recent):
            return False
        claimed_at = self._claim_effect(key, fingerprint, now)
        if claimed_at is None:
            return False
        now = claimed_at
        claim = dict(fingerprint=fingerprint, claimed_at=now, status="claimed")
        if current is not None and now - current["claimed_at"] < TTL_SECONDS:
            recent = [entry for entry in recent if entry["fingerprint"] != current["fingerprint"]]
            recent.append(current)
        value.update(current=claim, recent=recent[-(RECENT_LIMIT - 1):], checked_at=now)
        if scope is not None:
            if not isinstance(scope, str) or not scope or len(scope) > 4096:
                raise ValueError("notification_current_scope_invalid")
            value["scope_sha256"] = hashlib.sha256(scope.encode()).hexdigest()
        # The ledger above already ended eligibility, even if this optional
        # cache publication fails or a malformed old cache cannot be replaced.
        try:
            self.read(key)
            self.writer(self.path(key), value)
        except (ValueError, OSError):
            pass
        return True

    def _claim_effect(self, key, fingerprint, now):
        if self.database.is_symlink() or self.database.parent.resolve() != self.database.parent:
            raise ValueError("notification_recent_claim_path_invalid")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database, timeout=1)) as db:
            db.execute("PRAGMA synchronous=FULL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("notification_recent_claim_schema_invalid")
                db.execute("CREATE TABLE claims (scope TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                           "born REAL NOT NULL, expires REAL NOT NULL, PRIMARY KEY(scope,fingerprint))")
                db.execute("CREATE INDEX claim_expiry ON claims(expires)")
                db.execute("CREATE INDEX scope_recent_claims ON claims(scope,born)")
                db.execute("CREATE TABLE boundary (id INTEGER PRIMARY KEY CHECK(id=1),checked_at REAL NOT NULL)")
                db.execute("INSERT INTO boundary VALUES(1,?)", (now,))
                db.execute("PRAGMA user_version=1")
                db.commit()
            elif version != 1:
                raise ValueError("notification_recent_claim_schema_invalid")
            db.execute("BEGIN IMMEDIATE")
            high = db.execute("SELECT checked_at FROM boundary WHERE id=1").fetchone()
            if high is None or type(high[0]) not in (float,int) or not math.isfinite(high[0]):
                raise ValueError("notification_recent_claim_boundary_invalid")
            now = max(now, high[0])
            db.execute("UPDATE boundary SET checked_at=? WHERE id=1", (now,))
            # Finite recent horizon, bounded indexed cleanup; no whole history
            # materialization or archive. Real senders hold notifications.lock.
            expired = db.execute("SELECT rowid FROM claims INDEXED BY claim_expiry WHERE expires<=? LIMIT 32", (now,)).fetchall()
            db.executemany("DELETE FROM claims WHERE rowid=?", expired)
            previous = db.execute("SELECT expires FROM claims WHERE scope=? AND fingerprint=?", (key, fingerprint)).fetchone()
            if previous is not None and previous[0] <= now:
                db.execute("DELETE FROM claims WHERE scope=? AND fingerprint=?",(key,fingerprint))
                previous = None
            if previous is not None:
                db.commit()
                return None
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (key, fingerprint, now, now+TTL_SECONDS))
            excess = db.execute("SELECT rowid FROM claims INDEXED BY scope_recent_claims WHERE scope=? "
                                "ORDER BY born DESC,rowid DESC LIMIT 32 OFFSET ?", (key,RECENT_LIMIT)).fetchall()
            db.executemany("DELETE FROM claims WHERE rowid=?", excess)
            db.commit()  # The one permissible external attempt starts afterward.
        return now

    def cleanup(self, *, current_scopes=None, budget=16):
        """Foreground maintenance; keep this object across ticks for progress.

        The SQLite expiry seek is bounded independently of current activity.
        A retired point file needs a complete current configuration scope set;
        uncertain scope evidence leaves that file alone.
        """
        if type(budget) is not int or not 1 <= budget <= 64:
            raise ValueError("notification_current_cleanup_budget_invalid")
        removed = 0
        if self.database.exists():
            if self.database.is_symlink() or self.database.parent.resolve() != self.database.parent:
                raise ValueError("notification_recent_claim_path_invalid")
            with closing(sqlite3.connect(self.database, timeout=1)) as db:
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("notification_recent_claim_schema_invalid")
                db.execute("BEGIN IMMEDIATE")
                high = db.execute("SELECT checked_at FROM boundary WHERE id=1").fetchone()
                if high is None or not _finite_time(high[0]):
                    raise ValueError("notification_recent_claim_boundary_invalid")
                now = self._now(high[0])
                db.execute("UPDATE boundary SET checked_at=? WHERE id=1", (now,))
                rows = db.execute("SELECT rowid FROM claims INDEXED BY claim_expiry WHERE expires<=? LIMIT ?", (now,budget)).fetchall()
                db.executemany("DELETE FROM claims WHERE rowid=?",rows)
                removed += len(rows)
                db.commit()
        else:
            now = self._now()
        visited = 0
        if current_scopes is not None and self.root.is_dir():
            if self.root.resolve() != self.root.absolute():
                raise ValueError("notification_current_path_invalid")
            scopes = {hashlib.sha256(scope.encode()).hexdigest() for scope in current_scopes}
            iterator = getattr(self,"_cleanup_iterator",None)
            info=self.root.stat()
            identity=(info.st_dev,info.st_ino)
            if iterator is not None and getattr(self,'_cleanup_identity',None)!=identity:
                iterator.close()
                iterator=None
            if iterator is None:
                iterator = self._cleanup_iterator = os.scandir(self.root)
                self._cleanup_identity=identity
            for _ in range(budget):
                try: entry=next(iterator)
                except StopIteration:
                    iterator.close()
                    self._cleanup_iterator=None
                    break
                visited += 1
                if not entry.is_file(follow_symlinks=False):continue
                path=Path(entry.path)
                if path.suffix != '.json' or _DIGEST.fullmatch(path.stem) is None:
                    continue
                try:
                    value=self.read(path.stem)
                    if (value.get('scope_sha256') is not None and value['scope_sha256'] not in scopes
                            and now-value['checked_at']>=TTL_SECONDS):
                        path.unlink()
                        removed += 1
                except (ValueError,OSError):
                    continue
        return dict(visited=visited,removed=removed)

    def finish(self, key, fingerprint, *, sent):
        value = self.read(key)
        current = value.get("current")
        if current is None or current["fingerprint"] != fingerprint:
            raise ValueError("notification_current_finish_mismatch")
        current["status"] = "sent" if sent else "uncertain"
        self.writer(self.path(key), value)

    def claim_time(self, key, fingerprint):
        """Immutable ticket creation anchor from the committed pre-send claim."""
        with closing(sqlite3.connect(self.database.resolve().as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
            row=db.execute("SELECT born FROM claims WHERE scope=? AND fingerprint=?",(key,fingerprint)).fetchone()
        if row is None:
            raise ValueError("notification_recent_claim_missing")
        return row[0]

    def import_current(self, key, entry):
        """Explicit upgrade only: preserve a known current suppression claim.

        Caller selects keys from current configured workspaces, not every old
        historical workspace, and holds the migration/notification lock.
        """
        if self.path(key).exists():
            return False
        stamp = datetime.fromisoformat(entry["recorded_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or _DIGEST.fullmatch(entry["fingerprint"]) is None:
            raise ValueError("notification_current_import_invalid")
        claimed = stamp.astimezone(timezone.utc).timestamp()
        now = self._now(claimed)
        value = dict(schema_version=1, key=key, checked_at=now,
                     current=dict(fingerprint=entry["fingerprint"], claimed_at=claimed, status="uncertain"), recent=[])
        self.writer(self.path(key), value)
        return True
