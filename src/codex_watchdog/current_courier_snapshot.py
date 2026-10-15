"""Current native queue -> exact node courier receipt points, without history scans.

The source is embedded in the dependency-free SSH helper; Python 3.6 is retained.
Association publication follows the existing durable native enqueue receipt.
"""

CURRENT_COURIER_SOURCE = r'''
import sqlite3
import itertools


def control_courier_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def control_courier_path(home, thread, queue_id):
    if not control_courier_uuid(thread) or not control_courier_uuid(queue_id):
        raise ControlError("control_courier_identity_invalid")
    root = control_state_home(home) / "courier-current" / thread
    path = root / (queue_id + ".json")
    if root.is_symlink() or path.is_symlink() or path.resolve() != path:
        raise ControlError("control_courier_path_invalid")
    return path


def control_courier_receipt(home, instruction):
    if (not isinstance(instruction, str) or not 1 <= len(instruction) <= 512
            or re.fullmatch(r"[A-Za-z0-9._:/+-]+", instruction) is None):
        raise ControlError("control_courier_instruction_invalid")
    path = control_state_home(home) / "remote-wake" / (
        hashlib.sha256(instruction.encode()).hexdigest() + ".json")
    if path.is_symlink() or path.resolve() != path:
        raise ControlError("control_courier_path_invalid")
    try:
        with path.open("rb") as handle:
            raw = handle.read(65537)
        value = json.loads(raw) if len(raw) <= 65536 else None
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        raise ControlError("control_courier_receipt_invalid")


def control_courier_lock(home):
    path = control_state_home(home) / "courier-current" / "associations.lock"
    if path.is_symlink() or path.resolve() != path:
        raise ControlError("control_courier_path_invalid")
    return control_file_lock(path)


def control_courier_identity(record):
    if (not isinstance(record, dict) or not control_courier_uuid(record.get("thread_id"))
            or not control_courier_uuid(record.get("queue_message_id"))
            or not isinstance(record.get("instruction_id"), str)
            or re.fullmatch(r"[A-Za-z0-9._:/+-]{1,512}", record["instruction_id"]) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(record.get("prompt_sha256", ""))) is None
            or record.get("state") not in ("enqueued", "consumed_or_started", "started")):
        raise ControlError("control_courier_receipt_invalid")
    return {key: record[key] for key in (
        "thread_id", "queue_message_id", "instruction_id", "prompt_sha256")}


def control_publish_courier_association(home, receipt):
    identity = control_courier_identity(receipt)
    original = control_courier_receipt(home, identity["instruction_id"])
    if control_courier_identity(original) != identity:
        raise ControlError("control_courier_receipt_mismatch")
    path = control_courier_path(home, identity["thread_id"], identity["queue_message_id"])
    entry = dict(schema_version=1, kind="current_courier", node=control_node_name(), **identity)
    with control_courier_lock(home):
        if path.exists():
            previous = control_read_json(path)
            if (type(previous.get("schema_version")) is not int
                    or any(previous.get(key) != value for key, value in entry.items())):
                raise ControlError("control_courier_association_collision")
            return dict(status="duplicate")
        control_atomic_json(path, entry)
        return dict(status="published")


def control_courier_queue(home, thread, with_payload=False):
    """Current Q is legitimate work; no native row is modified or skipped."""
    if not control_courier_uuid(thread):
        raise ControlError("control_courier_identity_invalid")
    home = Path(home).resolve()
    paths = list(itertools.islice(home.glob("queue_*.sqlite"), 2))
    if len(paths) != 1:
        raise ControlError("linux_queue_unavailable_or_ambiguous")
    path = paths[0]
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ControlError("linux_queue_unavailable_or_ambiguous")
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            projection = "id"
            if with_payload:
                projection += ",CASE WHEN length(CAST(payload_json AS BLOB))<=131072 THEN payload_json ELSE NULL END"
            query = "SELECT " + projection + " FROM queued_items WHERE thread_id=?"
            plan = db.execute("EXPLAIN QUERY PLAN " + query, (thread,)).fetchall()
            if not any("SEARCH" in row[3] and "thread_id" in row[3] for row in plan):
                raise ControlError("control_native_queue_index_required")
            rows = db.execute(query, (thread,)).fetchall()
            if any(not control_courier_uuid(row[0]) for row in rows):
                raise ControlError("control_courier_identity_invalid")
            return rows
    except (sqlite3.Error, OSError):
        raise ControlError("linux_queue_unavailable_or_ambiguous")


def control_courier_payload(raw):
    try:
        payload = json.loads(raw) if isinstance(raw, str) else None
        user = payload.get("UserInput") if isinstance(payload, dict) else None
        parts = user.get("content") if isinstance(user, dict) else None
        if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
                or parts[0].get("type") != "text" or not isinstance(parts[0].get("text"), str)):
            return None
        match = re.fullmatch(r"\[CODEX_WATCHDOG_WAKE id=([A-Za-z0-9._:/+-]{1,512}) sha256=([0-9a-f]{64})\]\n([\s\S]*)",
                             parts[0]["text"])
        if match is None or hashlib.sha256(match.group(3).encode()).hexdigest() != match.group(2):
            return None
        return match.group(1), match.group(2)
    except (ValueError, TypeError, RecursionError):
        return None


def control_has_current_courier(home, thread):
    rows = control_courier_queue(home, thread, with_payload=True)
    for row in rows:
        try:
            native = control_courier_payload(row[1])
            if native is None:
                continue
            path = control_courier_path(home, thread, row[0])
            if not path.exists():
                continue
            entry = control_read_json(path)
            if (type(entry.get("schema_version")) is not int
                    or entry.get("kind") != "current_courier" or entry.get("node") != control_node_name()
                    or entry.get("thread_id") != thread or entry.get("queue_message_id") != row[0]):
                continue
            original = control_courier_receipt(home, entry.get("instruction_id"))
            identity = control_courier_identity(original)
            if (native == (identity["instruction_id"], identity["prompt_sha256"])
                    and all(entry.get(key) == value for key, value in identity.items())):
                return True
        except ControlError:
            continue  # This point cannot authorize resumption; later Q remains independent.
    return False


def control_import_current_couriers(home, thread):
    """Explicit v2.2.8 compatibility import, bounded by actual Q, never by H.

    Derive original receipt point only from a validated native UserInput header
    and matching body digest. It does not enqueue/start anything or scan history.
    """
    imported = 0
    for queue_id, raw in control_courier_queue(home, thread, with_payload=True):
        try:
            native = control_courier_payload(raw)
            if native is None:
                continue
            receipt = control_courier_receipt(home, native[0])
            if (receipt.get("thread_id") != thread or receipt.get("queue_message_id") != queue_id
                    or receipt.get("instruction_id") != native[0]
                    or receipt.get("prompt_sha256") != native[1]):
                continue
            result = control_publish_courier_association(home, receipt)
            imported += result["status"] == "published"
        except (ControlError, ValueError, TypeError, RecursionError):
            continue
    return dict(status="imported_current_queue", imported=imported)


class ControlCourierCleanup:
    """Foreground maintenance cursor, not a persistent archive/index.

    Keep this object in the active scheduler. Recreating it in an SSH RPC or
    after restart does not promise cross-restart enumeration progress. The
    caller supplies actual current in-flight queue IDs; unknown work defers
    cleanup only. Original courier receipts and native rows are never deleted.
    """
    def __init__(self, home, thread):
        if not control_courier_uuid(thread):
            raise ControlError("control_courier_identity_invalid")
        self.home, self.thread, self.iterator = Path(home).resolve(), thread, None

    def step(self, budget=16, protected_queue_ids=None):
        if type(budget) is not int or not 1 <= budget <= 64:
            raise ControlError("control_cleanup_budget_invalid")
        if protected_queue_ids is None:
            return dict(status="current_work_inventory_required", visited=0, removed=0)
        if not isinstance(protected_queue_ids, (list, tuple, set)) or any(
                not control_courier_uuid(qid) for qid in protected_queue_ids):
            raise ControlError("control_courier_identity_invalid")
        # Publisher takes this same lock after enqueue, before association.
        # Snapshot Q only once it is held; a concurrent publication cannot
        # appear in scandir after an earlier, stale Q snapshot.
        with control_courier_lock(self.home):
            return self._locked_step(budget, protected_queue_ids)

    def _locked_step(self, budget, protected_queue_ids):
        current = {row[0] for row in control_courier_queue(self.home, self.thread)}
        current.update(protected_queue_ids)
        directory = control_courier_path(self.home, self.thread,
            "00000000-0000-0000-0000-000000000000").parent
        if self.iterator is None:
            try:
                self.iterator = os.scandir(str(directory))
            except FileNotFoundError:
                return dict(status="cleaned_bounded", visited=0, removed=0)
        visited = removed = 0
        while visited < budget:
            try:
                item = next(self.iterator)
            except StopIteration:
                self.iterator.close()
                self.iterator = None
                break
            visited += 1
            if not item.name.endswith(".json") or not item.is_file(follow_symlinks=False):
                continue
            qid = Path(item.name).stem
            if not control_courier_uuid(qid) or qid in current:
                continue
            path = control_courier_path(self.home, self.thread, qid)
            try:
                value = control_read_json(path)
                if (type(value.get("schema_version")) is not int
                        or value.get("kind") != "current_courier" or value.get("thread_id") != self.thread
                        or value.get("queue_message_id") != qid or value.get("node") != control_node_name()):
                    continue
                path.unlink()
                removed += 1
            except (ControlError, OSError):
                continue
        return dict(status="cleaned_bounded", visited=visited, removed=removed)
'''
