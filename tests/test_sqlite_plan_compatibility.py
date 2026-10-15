"""Real indexed API fixtures with narrowly projected EXPLAIN spelling only.

These grammar/fault projections are not an old SQLite engine. Root's actual
version-pinned engine reproduction remains separate native-library evidence.
"""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import pytest

from codex_watchdog import current_work_retention as current
from codex_watchdog import native_rollout, native_stream_work
from codex_watchdog.native_inflight import NativeInflightError
from test_legacy_reply_tickets import SESSION, HOUR, stamp, thread_row
from test_legacy_reply_tickets_scaling import journal_at
from test_native_inflight import (
    rig, THREAD, OTHER, QUEUE, enqueue, provider_db, rows_for, sha,
)
from test_native_rollout import native_state


SURFACES = ("rollout", "stream", "legacy", "current")
SITES = ("rollout", "stream", "legacy-point", "legacy-namespace", "legacy-session",
         "native_event_work", "native_request_event", "native_ack_event")
FAULTS = ("full-scan", "index-scan", "covering-scan", "temporary-sort",
          "multiple-seeks", "wrong-table", "wrong-key")


def query_site(sql):
    sql = " ".join(sql.split())
    if "FROM threads WHERE id" in sql:
        return "threads"
    for name in ("native_event_work", "native_request_event", "native_ack_event"):
        if "INDEXED BY " + name in sql:
            return name
    if "INDEXED BY namespace_active_tickets" in sql:
        return "legacy-namespace"
    if "INDEXED BY session_active_tickets" in sql:
        return "legacy-session"
    if "FROM records WHERE namespace=? AND kind=? AND key=?" in sql:
        return "legacy-point"
    return None


def spelling(detail, style):
    # Only these literal, known table-name prefixes are modeled. All remaining
    # actual planner text is kept; estimates/new spellings are not guessed.
    for table in ("threads", "records"):
        old, new = "SEARCH TABLE " + table + " ", "SEARCH " + table + " "
        if detail.startswith(old):
            detail = new + detail[len(old):]
        if style == "table" and detail.startswith(new):
            return old + detail[len(new):]
    return detail


def corrupt_plan(rows, fault):
    assert len(rows) == 1, "fixture needs one genuine compiled equality seek"
    row = list(rows[0])
    detail = row[3]
    assert detail.startswith("SEARCH ")
    if fault == "full-scan":
        row[3] = "SCAN " + ("threads" if "SEARCH threads " in detail else "records")
    elif fault in ("index-scan", "covering-scan"):
        row[3] = detail.replace("SEARCH ", "SCAN ", 1)
        row[3] = re.sub(r" \([^()]*\)$", "", row[3])
        if fault == "covering-scan":
            row[3] = row[3].replace("USING INDEX ", "USING COVERING INDEX ")
    elif fault == "wrong-table":
        row[3] = detail.replace("SEARCH threads ", "SEARCH foreign_threads ", 1).replace(
            "SEARCH records ", "SEARCH foreign_records ", 1)
    elif fault == "wrong-key":
        row[3] = re.sub(r" \([^()]*\)$", " (unrelated=?)", detail)
    elif fault == "wrong-index":
        row[3] = detail.replace("USING INDEX ", "USING INDEX wrong_", 1).replace(
            "USING COVERING INDEX ", "USING COVERING INDEX wrong_", 1)
    elif fault == "temporary-sort":
        return [tuple(row), (999, 0, 0, "USE TEMP B-TREE FOR ORDER BY")]
    elif fault == "multiple-seeks":
        return [tuple(row), (999, row[1], row[2], detail)]
    else:
        raise AssertionError("unknown fixture fault")
    assert row[3] != detail
    return [tuple(row)]


class PlanCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchmany(self, size):
        result, self.rows = self.rows[:size], self.rows[size:]
        return result

    def fetchall(self):
        return self.fetchmany(len(self.rows))


class PlanConnection:
    def __init__(self, db, state):
        self.db, self.state = db, state
        self.set_progress_handler(lambda: 0, 1)

    def __getattr__(self, key):
        return getattr(self.db, key)

    def set_progress_handler(self, callback, interval):
        count = [0]

        def step():
            self.state["vm"] += 1
            count[0] += 1
            return callback() if callback is not None and count[0] % interval == 0 else 0

        return self.db.set_progress_handler(step, 1)

    def execute(self, sql, *args, **kwargs):
        self.state["sql"].append(sql)
        result = self.db.execute(sql, *args, **kwargs)
        site = query_site(sql)
        if not sql.lstrip().upper().startswith("EXPLAIN QUERY PLAN "):
            if site is not None:
                self.state["payload_sites"].append(site)
            return result
        rows = result.fetchall()
        original = [tuple(row) for row in rows]
        rows = [tuple(row[:3]) + (spelling(row[3], self.state["style"]),) for row in rows]
        if self.state["fault"] and site == self.state["target"]:
            rows = corrupt_plan(rows, self.state["fault"])
            self.state["fault_reached"] = True
        self.state["plans"].append(dict(site=site, raw=original, projected=rows))
        return PlanCursor(rows)


@contextmanager
def projected_plans(monkeypatch, *, style="current", target=None, fault=None):
    connect = sqlite3.connect
    state = dict(style=style, target=target, fault=fault, fault_reached=False,
                 sql=[], vm=0, connections=0, plans=[], payload_sites=[])

    def wrapped(*args, **kwargs):
        state["connections"] += 1
        return PlanConnection(connect(*args, **kwargs), state)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", wrapped)
        yield state


def fixture_case(surface, base, native_rig, monkeypatch, history=0):
    base = base.resolve()
    if surface in ("rollout", "stream"):
        home, repo = base / "profile", base / "repo"
        path = native_state(home, repo, thread=THREAD)
        with sqlite3.connect(home / "state_5.sqlite") as db:
            db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (
                ("history-" + str(i), str(repo), "cli", "user", 1,
                 str(home / "sessions" / (str(i) + ".jsonl")), "preserve") for i in range(history)))

        def invoke(missing=False):
            thread = OTHER if missing else THREAD
            if surface == "rollout":
                result = native_rollout.find_native_rollout(home, thread, repo)
                assert result == (None if missing else path)
            else:
                assert native_stream_work.stream_exact_thread(home, thread, repo) is (not missing)

        def refuse():
            if surface == "rollout":
                assert native_rollout.find_native_rollout(home, THREAD, repo) is None
            else:
                assert not native_stream_work.stream_exact_thread(home, THREAD, repo)

        return dict(invoke=invoke, refuse=refuse, files=[home / "state_5.sqlite", path])

    if surface == "legacy":
        journal = journal_at(base / "legacy", history,
            [thread_row("current", SESSION, stamp(-HOUR))], monkeypatch)

        def invoke(missing=False):
            with journal.transaction() as db:
                result = journal.get(db, "threads", "missing" if missing else "current")
                assert (result is None) is missing
                assert set(journal.active(db)) == {"current"}

        def refuse():
            with pytest.raises(ValueError, match="legacy_schema_invalid"):
                with journal.transaction():
                    pytest.fail("unsafe legacy plan reached provider payload body")

        return dict(invoke=invoke, refuse=refuse, files=[journal.database, journal.path])

    identifier, prompt = "slackbot:" + sha("sqlite-compatibility")[:40], "current original work"
    rows = rows_for(identifier, prompt)
    db = provider_db(native_rig, rows)
    db.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", (
        (rows[0]["namespace"], "events", "historic-" + str(i), json.dumps(dict(
            instruction_id="slackbot:old-" + str(i), text_sha256=sha("old"))),
         None, THREAD, 0, None) for i in range(history)))
    db.commit()
    db.close()
    enqueue(native_rig, identifier, prompt)
    # Real source-native fixture collector; the plan model is installed only
    # afterward. This is not a fabricated live native/RPC or owner claim.
    inventory = native_rig.inventory()
    path = native_rig.runtime / "slack/reply-tickets.sqlite3"

    def invoke(missing=False):
        value = inventory
        if missing:
            value = dict(inventory, native=dict(inventory["native"], instruction_id=None,
                prompt_sha256=None), queue=dict(inventory["queue"], items=[dict(
                    queue_id=QUEUE, instruction_id="slackbot:" + sha("missing")[:40],
                    prompt_sha256=sha("missing"))]))
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            result = current.current_associations(connection, value)
        if missing:
            assert result == []
        else:
            assert len(result) == 1 and len(result[0]["refs"]) == 6
            assert result[0]["instruction_id"] == identifier and result[0]["queue_id"] == QUEUE

    def refuse():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            with pytest.raises(NativeInflightError, match="index_unavailable"):
                current.current_associations(connection, inventory)

    return dict(invoke=invoke, refuse=refuse, files=[path])


def contents(paths):
    return {str(path): path.read_bytes() for path in paths}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("style", ["current", "table"])
def test_actual_api_accepts_only_supported_search_table_spelling(rig, tmp_path, monkeypatch, surface, style):
    case = fixture_case(surface, tmp_path / "case", rig, monkeypatch)
    with projected_plans(monkeypatch, style=style) as state:
        case["invoke"]()
        case["invoke"](missing=True)
    assert state["plans"]
    assert all(len(plan["projected"]) == 1 for plan in state["plans"])
    expected = "SEARCH TABLE " if style == "table" else "SEARCH "
    assert all(plan["projected"][0][3].startswith(expected) for plan in state["plans"])
    if surface in ("legacy", "current"):
        sites = {plan["site"] for plan in state["plans"]}
        assert sites >= ({"legacy-point", "legacy-namespace", "legacy-session"} if surface == "legacy"
                         else {"native_event_work", "native_request_event", "native_ack_event"})


def site_surface(site):
    return "legacy" if site.startswith("legacy-") else site if site in ("rollout", "stream") else "current"


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("fault", FAULTS)
def test_unsafe_plan_refuses_before_its_payload_query_without_repair(rig, tmp_path, monkeypatch, site, fault):
    case = fixture_case(site_surface(site), tmp_path / "case", rig, monkeypatch)
    before = contents(case["files"])
    target = "threads" if site in ("rollout", "stream") else site
    with projected_plans(monkeypatch, target=target, fault=fault) as state:
        case["refuse"]()
    assert state["fault_reached"]
    assert target not in state["payload_sites"]
    assert contents(case["files"]) == before
    # Scoped bad evidence cannot poison another later valid query/transaction.
    with projected_plans(monkeypatch):
        case["invoke"]()


@pytest.mark.parametrize("site", ["legacy-namespace", "legacy-session",
                                   "native_event_work", "native_request_event", "native_ack_event"])
def test_wrong_required_index_name_is_not_spelling_compatibility(rig, tmp_path, monkeypatch, site):
    case = fixture_case(site_surface(site), tmp_path / "case", rig, monkeypatch)
    before = contents(case["files"])
    with projected_plans(monkeypatch, target=site, fault="wrong-index") as state:
        case["refuse"]()
    assert state["fault_reached"] and site not in state["payload_sites"]
    assert contents(case["files"]) == before


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("history", [0, 1000, 10000, 100000])
def test_modeled_old_spelling_still_uses_actual_indexed_history_independent_work(
        rig, tmp_path, monkeypatch, capsys, surface, history):
    case = fixture_case(surface, tmp_path / "case", rig, monkeypatch, history)

    def no_history(*args, **kwargs):
        pytest.fail("indexed point guard enumerated native/provider history")

    with projected_plans(monkeypatch, style="table") as state:
        with monkeypatch.context() as patch:
            for name in ("glob", "rglob", "iterdir"):
                patch.setattr(Path, name, no_history)
            case["invoke"]()
            case["invoke"](missing=True)
    assert state["sql"] and len(state["sql"]) < 110
    assert state["vm"] < 2500  # Fixed schema/point/rank work, never H rows.
    assert all(len(plan["projected"]) == 1 and "SCAN" not in plan["projected"][0][3]
               and "TEMP B-TREE" not in plan["projected"][0][3] for plan in state["plans"])
    with capsys.disabled():
        print("C354_SQLITE_PLAN_WORK", json.dumps(dict(surface=surface, history=history,
            sql=len(state["sql"]), vm=state["vm"], connections=state["connections"],
            plans=state["plans"], evidence="real local engine with EXPLAIN spelling projection only"), sort_keys=True))
