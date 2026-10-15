"""Retention preflight: caller pins precede pruning; unknown proof defers deletion.

Disposable SQLite journals and fake callbacks only; no providers or native clients.
"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid


from codex_watchdog import reply_tickets, recent_state

T0 = 1_700_000_000.0
EXPIRED = recent_state.TTL_SECONDS + 1


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


class Runtime:
    """One disposable journal runtime with a controllable clock."""

    def __init__(self, root):
        self.runtime = Path(root) / "runtime"
        self.runtime.mkdir()
        self.path = self.runtime / "slack" / "reply-tickets.json"
        self.lock = Path(root) / "reply-tickets.lock"
        self.database = self.runtime / "slack" / "reply-tickets.sqlite3"
        self.time = [T0]

    def journal(self, **options):
        return reply_tickets.ReplyTickets(self.path, self.lock, "slack", self.runtime, lambda: {},
                                          clock=lambda: self.time[0], **options)

    def advance(self, seconds):
        self.time[0] += seconds

    def put(self, *keys):
        journal = self.journal()
        with journal.transaction() as db:
            for key in keys:
                journal.put(db, "events", key, {"key": key})

    def keys(self, table, kind="events"):
        with closing(sqlite3.connect(str(self.database))) as db:
            return {row[0] for row in db.execute("SELECT key FROM " + table + " WHERE kind=?", (kind,))}

    def protected(self, kind="events"):
        with closing(sqlite3.connect(str(self.database))) as db:
            return dict(db.execute("SELECT key,protected FROM recent_records WHERE kind=?", (kind,)))


def fail(journal, db):
    raise RuntimeError("current_proof_unavailable")


class RetentionPreflightTests(unittest.TestCase):
    def runtime(self):
        root = tempfile.mkdtemp(prefix="retention-preflight-")
        self.addCleanup(shutil.rmtree, root, True)
        return Runtime(root)

    def test_callback_runs_once_in_immediate_transaction_after_namespace_checks(self):
        env = self.runtime()
        seen = []

        def callback(journal, db):
            other = sqlite3.connect(str(env.database), timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                locked = False
            except sqlite3.OperationalError:
                locked = True
            finally:
                other.close()
            seen.append(dict(journal=journal, active=db.in_transaction, locked=locked,
                             version=db.execute("PRAGMA user_version").fetchone()[0],
                             source=db.execute("SELECT 1 FROM sources WHERE namespace=?",
                                               (journal.namespace,)).fetchone()))
            return True

        journal = env.journal(before_prune=callback)
        with journal.transaction() as db:
            self.assertEqual(len(seen), 1)
            journal.put(db, "events", "new", {"key": "new"})
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0]["journal"], journal)
        self.assertEqual((seen[0]["active"], seen[0]["locked"], seen[0]["version"], seen[0]["source"]),
                         (True, True, 3, (1,)))
        with journal.transaction():
            pass
        self.assertEqual(len(seen), 2)
        self.assertEqual(env.keys("records"), {"new"})

    def test_callback_precedes_prune_and_can_pin_expired_row(self):
        env = self.runtime()
        env.put("old", "other")
        env.advance(EXPIRED)
        before = []

        def callback(journal, db):
            before.append(sorted(row[0] for row in db.execute("SELECT key FROM records WHERE kind='events'")))
            journal.protect(db, "events", "old")
            return True

        with env.journal(before_prune=callback).transaction():
            pass
        self.assertEqual(before, [["old", "other"]])
        self.assertEqual(env.keys("records"), {"old"})
        self.assertEqual(env.protected(), {"old": 1})

    def test_unknown_proof_keeps_dedup_rows_while_independent_row_commits(self):
        outcomes = dict(false=lambda journal, db: False, none=lambda journal, db: None,
                        integer=lambda journal, db: 1, text=lambda journal, db: "True", error=fail)
        for name, callback in outcomes.items():
            with self.subTest(name):
                env = self.runtime()
                env.put("old")
                env.advance(EXPIRED)
                journal = env.journal(before_prune=callback)
                with journal.transaction() as db:
                    self.assertEqual(journal.get(db, "events", "old"), {"key": "old"})
                    journal.put(db, "events", "new", {"key": "new"})
                    self.assertEqual(journal.get(db, "events", "new"), {"key": "new"})
                self.assertEqual(env.keys("records"), {"old", "new"})
                self.assertEqual(env.keys("recent_records"), {"old", "new"})
                with env.journal().transaction():
                    pass
                self.assertEqual(env.keys("records"), {"new"})

    def test_failed_callback_rolls_back_partial_pin_while_body_commits(self):
        for result in (RuntimeError("partial"), False):
            with self.subTest(repr(result)):
                env = self.runtime()
                env.put("old")
                env.advance(EXPIRED)

                def callback(journal, db, result=result):
                    journal.protect(db, "events", "old")
                    if isinstance(result, BaseException):
                        raise result
                    return result

                journal = env.journal(before_prune=callback)
                with journal.transaction() as db:
                    pin = db.execute("SELECT protected FROM recent_records WHERE kind='events' AND key='old'")
                    self.assertEqual(pin.fetchone(), (0,))
                    journal.put(db, "events", "new", {"key": "new"})
                self.assertEqual(env.protected(), {"old": 0, "new": 0})
                self.assertEqual(env.keys("records"), {"old", "new"})
                with env.journal().transaction():
                    pass
                self.assertEqual(env.keys("records"), {"new"})

    def test_expired_parent_stays_invalid_during_retention_deferral(self):
        env = self.runtime()
        entry = dict(target=dict(thread_id=str(uuid.uuid4())), event_fingerprint="fingerprint",
                     created_at=stamp(T0), ticket_schema=1)
        journal = env.journal()
        with journal.transaction() as db:
            journal.record(db, "parent", entry)
        env.advance(EXPIRED)
        journal = env.journal(before_prune=lambda journal, db: False)
        with journal.transaction() as db:
            self.assertIsNone(journal.get(db, "threads", "parent"))
            self.assertFalse(journal.claim(db, "parent"))
            journal.put(db, "events", "new", {"key": "new"})
        self.assertEqual(env.keys("records", "threads"), {"parent"})
        self.assertEqual(env.keys("records"), {"new"})

    def test_cap_deletion_is_deferred_but_new_recent_metadata_is_tracked(self):
        env = self.runtime()
        with mock.patch.object(recent_state, "RECENT_LIMIT", 2):
            for key in ("a", "b"):
                env.put(key)
                env.advance(1)
            journal = env.journal(before_prune=lambda journal, db: False)
            with journal.transaction() as db:
                journal.put(db, "events", "c", {"key": "c"})
            self.assertEqual(env.keys("records"), {"a", "b", "c"})
            self.assertEqual(env.keys("recent_records"), {"a", "b", "c"})
            env.advance(1)
            env.put("d")
            self.assertEqual(env.keys("records"), {"c", "d"})

    def test_without_callback_or_with_proof_retention_is_unchanged(self):
        options = dict(default={}, explicit_none=dict(before_prune=None),
                       proven=dict(before_prune=lambda journal, db: True))
        for name, option in options.items():
            with self.subTest(name):
                env = self.runtime()
                env.put("old", "expiring")
                env.advance(EXPIRED)
                with mock.patch.object(recent_state, "PRUNE_BUDGET", 1):
                    journal = env.journal(**option)
                    with journal.transaction() as db:
                        remaining = {row[0] for row in db.execute("SELECT key FROM records WHERE kind='events'")}
                        self.assertEqual(len(remaining), 1)
                        self.assertIsNone(journal.get(db, "events", remaining.pop()))
                        self.assertFalse(journal._retention_deferred)
                self.assertEqual(env.keys("records"), set())
                self.assertEqual(env.keys("recent_records"), set())

    def test_deferral_state_resets_after_commit_and_rollback(self):
        env = self.runtime()
        env.put("old")
        env.advance(EXPIRED)
        results, entered = [False, False, True], []

        def callback(journal, db):
            entered.append(journal._retention_deferred)
            return results.pop(0)

        journal = env.journal(before_prune=callback)
        self.assertFalse(journal._retention_deferred)
        with journal.transaction():
            self.assertTrue(journal._retention_deferred)
        self.assertFalse(journal._retention_deferred)
        with self.assertRaises(RuntimeError):
            with journal.transaction() as db:
                journal.put(db, "events", "rolled-back", {"key": "rolled-back"})
                self.assertTrue(journal._retention_deferred)
                raise RuntimeError("body_failed")
        self.assertFalse(journal._retention_deferred)
        self.assertEqual(env.keys("records"), {"old"})
        with journal.transaction():
            self.assertFalse(journal._retention_deferred)
        self.assertEqual(entered, [False, False, False])
        self.assertEqual(env.keys("records"), set())

    def test_interrupt_propagates_and_discards_partial_pin(self):
        env = self.runtime()
        env.put("old")
        env.advance(EXPIRED)
        body = []

        def callback(journal, db):
            journal.protect(db, "events", "old")
            raise KeyboardInterrupt()

        journal = env.journal(before_prune=callback)
        with self.assertRaises(KeyboardInterrupt):
            with journal.transaction():
                body.append(True)
        self.assertEqual(body, [])
        self.assertFalse(journal._retention_deferred)
        self.assertEqual(env.protected(), {"old": 0})

    def test_callback_ending_provider_transaction_is_refused(self):
        env = self.runtime()
        env.put("old")
        journal = env.journal(before_prune=lambda journal, db: db.commit() or True)
        with self.assertRaises(ValueError):
            with journal.transaction():
                self.fail("body must not run outside the provider transaction")
        self.assertFalse(journal._retention_deferred)

    def test_non_callable_callback_is_rejected(self):
        env = self.runtime()
        with self.assertRaises(TypeError):
            env.journal(before_prune=True)


if __name__ == "__main__":
    unittest.main()
